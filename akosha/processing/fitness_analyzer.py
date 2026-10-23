"""Fitness analyzer — periodic background job that computes routing fitness signals.

Reads spans from the ``spans_substrate`` table via ``SpanQuery.summary`` and
computes rolling failure_rate and p99 latency per (task_class, selector) pair.

The pair set is a **declared inventory** (``declared_pairs``), not a set derived
from the rows that happen to be present — see
``docs/decisions/2026-10-21-fitness-analyzer-pair-inventory.md``. A declared pair
that emitted nothing is reported with ``samples == 0`` and ``score == 0.0``
rather than being skipped, so "idle" and "broken" stay distinguishable.

This replaced a per-component HTTP fan-out whose endpoint list had no writers in
production. The declared inventory keeps the property that made that approach
sound in principle: the report's shape comes from configuration, not from
traffic.

Signals are returned in-memory (via :meth:`run_fitness_analysis`); the
prior Dhara-backed persistence layer was removed when Dhara was
decommissioned. Callers that need durable storage should subscribe to
``run_fitness_analysis`` results or pipe the analyzer's output into
their own storage layer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from akosha.storage.span_query import SpanQuery, SpanSummary

logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL_SECONDS = 60


@dataclass
class FitnessSignal:
    """Routing fitness signal for a (task_class, selector) pair."""

    score: float = 0.0
    samples: int = 0
    failure_rate: float = 0.0
    p99_latency_ms: float = 0.0
    updated_at: str | None = None
    window_start: str | None = None
    component_count: int = 0


class FitnessAnalyzer:
    """Periodic fitness signal analyzer.

    Periodically polls known Bodai component endpoints for traces and
    computes aggregated fitness signals. Signals are kept in memory
    only — the historical Dhara persistence layer was removed when
    Dhara was decommissioned.

    Parameters:
        poll_interval_seconds: Interval between analysis runs (default 60 s)
        component_endpoints: List of (component_name, mcp_url) tuples to poll
    """

    def __init__(
        self,
        poll_interval_seconds: int = _DEFAULT_POLL_INTERVAL_SECONDS,
        component_endpoints: list[tuple[str, str]] | None = None,
        *,
        declared_pairs: list[tuple[str, str]] | None = None,
        span_query: SpanQuery | None = None,
        window_start_unix_nano: int | None = None,
    ) -> None:
        self._poll_interval = max(poll_interval_seconds, 1)
        self._component_endpoints = component_endpoints or []
        # R-6: the (task_class, selector) pairs this analyzer reports on. This is
        # the *declared* set from
        # docs/decisions/2026-10-21-fitness-analyzer-pair-inventory.md — it must
        # never be derived from the rows it analyses, because a pair that
        # migrated and emitted nothing would then vanish rather than report
        # zero, making "idle" indistinguishable from "broken".
        self._declared_pairs = list(declared_pairs or [])
        self._span_query = span_query
        self._window_start_unix_nano = window_start_unix_nano
        # In-memory signal cache, keyed by ``task_class -> selector -> FitnessSignal``.
        self._signals: dict[str, dict[str, FitnessSignal]] = {}

        self._running = False
        self._task: asyncio.Task[None] | None = None

    def _signal_from_summary(
        self,
        summary: SpanSummary,
        cutoff_unix_nano: int,
    ) -> FitnessSignal:
        """Build a signal from a substrate aggregate.

        ``failure_rate`` keeps its original denominator: ``failure_count /
        span_count`` over *every* row in the window. The substrate's
        ``span_count`` is the same population the old ``len(traces)`` counted,
        so the reported number does not silently change meaning. Rows with a
        NULL ``latency_ms`` stay in that denominator even though
        ``PERCENTILE_CONT`` excludes them from ``latency_p99_ms`` — the two
        aggregates cover different populations on purpose (read-path spec §4.2).

        ``component_count`` is 0: it came from counting distinct
        ``component_name`` across per-component HTTP responses, and the
        substrate query returns one aggregate rather than the rows behind it.
        """
        span_count = summary.span_count
        failure_rate = summary.failure_count / span_count if span_count else 0.0
        # Score is NOT ``1.0 - failure_rate`` when there are no rows: that would
        # score silence as 1.0 and report a broken producer as perfect. The old
        # ``_compute_signal`` returned the default (score 0.0) for empty input,
        # pinned by test_fitness_analyzer_baseline.py::test_empty_traces_yields
        # _the_default_signal. Silence is unknown, not healthy.
        score = 1.0 - failure_rate if span_count else 0.0
        window_start = datetime.fromtimestamp(cutoff_unix_nano / 1_000_000_000, UTC)
        return FitnessSignal(
            score=score,
            samples=span_count,
            failure_rate=failure_rate,
            # None means "no rows carried a latency", not "zero latency" — but
            # FitnessSignal.p99_latency_ms is a plain float, so ``samples == 0``
            # is what carries the no-data distinction at this layer.
            p99_latency_ms=summary.latency_p99_ms if summary.latency_p99_ms is not None else 0.0,
            updated_at=datetime.now(UTC).isoformat(),
            window_start=window_start.isoformat(),
            component_count=0,
        )

    async def _analyze_and_persist(self) -> None:
        """Run one analysis cycle: query the substrate and update the signal cache.

        R-6 replaced the per-component HTTP fan-out. The old cycle could not
        distinguish a declared pair that went quiet from a pair that did not
        exist, because it derived both the pair set and the signals from the
        rows it happened to get back — and ``if not traces: continue`` dropped
        silent pairs entirely. Here every declared pair produces a row, so
        ``samples == 0`` is a reportable state rather than an absence.

        The historical Dhara write step was removed when Dhara was
        decommissioned; signals are stored in ``self._signals`` and returned
        in-memory via :meth:`run_fitness_analysis`.
        """
        if self._span_query is None:
            logger.debug(
                "FitnessAnalyzer: no SpanQuery configured; nothing to analyze"
            )
            return

        if not self._declared_pairs:
            logger.debug("FitnessAnalyzer: no declared (task_class, selector) pairs")
            return

        cutoff = self._window_start_unix_nano
        if cutoff is None:
            cutoff = int((datetime.now(UTC) - timedelta(minutes=60)).timestamp() * 1e9)

        all_signals: dict[str, dict[str, FitnessSignal]] = {}
        for task_class, selector in self._declared_pairs:
            summary = await self._span_query.summary(
                selector=selector,
                task_class=task_class,
                cutoff_unix_nano=cutoff,
                # All rows, not just success/error: this analyzer's historical
                # denominator was every returned trace, with only the literal
                # 'error' counted as a failure. See
                # SpanQuery.summary's docstring for why the two callers differ.
                scored_outcomes_only=False,
            )
            all_signals.setdefault(task_class, {})[selector] = self._signal_from_summary(
                summary, cutoff
            )

        self._signals = all_signals

    async def _run_loop(self) -> None:
        """Main analysis loop — runs until stop() is called."""
        while self._running:
            try:
                await self._analyze_and_persist()
            except Exception as exc:
                logger.debug("FitnessAnalyzer cycle failed: %s", exc)
            await asyncio.sleep(self._poll_interval)

    async def start(self) -> None:
        """Start the periodic analysis background task."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "FitnessAnalyzer started (poll_interval=%ds, components=%d)",
            self._poll_interval,
            len(self._component_endpoints),
        )

    async def stop(self) -> None:
        """Stop the background task gracefully."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("FitnessAnalyzer stopped")

    async def run_fitness_analysis(self) -> dict[str, dict[str, FitnessSignal]]:
        """Manual trigger — run one analysis cycle and return the signals.

        Returns:
            Dict mapping task_class → selector → FitnessSignal
        """
        await self._analyze_and_persist()
        return self._signals
