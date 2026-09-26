"""Fitness analyzer — periodic background job that computes routing fitness signals.

Polls each Bodai component's MCP endpoint (via CommonMCPClient) for local OTel
traces and computes rolling failure_rate and p99 latency per
(task_class, selector) pair.

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
from typing import Any

from mcp_common.clients.common_mcp_client import (
    CommonMCPClient,
)

from akosha.mcp.client import query_local_traces

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
    ) -> None:
        self._poll_interval = max(poll_interval_seconds, 1)
        self._component_endpoints = component_endpoints or []
        # In-memory signal cache, keyed by ``task_class -> selector -> FitnessSignal``.
        self._signals: dict[str, dict[str, FitnessSignal]] = {}

        self._running = False
        self._task: asyncio.Task[None] | None = None

    def add_component(self, component_name: str, mcp_url: str) -> None:
        """Register a component endpoint to be polled.

        Args:
            component_name: Name of the component (e.g. "mahavishnu")
            mcp_url: MCP HTTP server URL (e.g. "http://localhost:8680/mcp")
        """
        entry = (component_name, mcp_url)
        if entry not in self._component_endpoints:
            self._component_endpoints.append(entry)

    async def _fetch_traces_from_component(
        self,
        component_name: str,
        mcp_url: str,
        task_class: str,
        time_range_minutes: int = 60,
    ) -> list[dict[str, Any]]:
        """Fetch traces from a single component via its MCP endpoint."""
        client = CommonMCPClient(base_url=mcp_url, timeout=15.0)
        try:
            return await query_local_traces(client, task_class, time_range_minutes)
        except Exception as exc:
            logger.debug(
                "Failed to fetch traces from %s (%s): %s",
                component_name,
                mcp_url,
                exc,
            )
            return []
        finally:
            await client.aclose()

    async def _collect_traces(self, task_class: str) -> list[dict[str, Any]]:
        """Poll all registered component endpoints for traces."""
        results = await asyncio.gather(
            *[
                self._fetch_traces_from_component(name, url, task_class)
                for name, url in self._component_endpoints
            ],
            return_exceptions=True,
        )
        traces: list[dict[str, Any]] = []
        for result in results:
            if isinstance(result, list):
                traces.extend(result)
        return traces

    def _compute_signal(
        self,
        _task_class: str,  # Intentionally unused; reserved for future per-task-class routing
        _selector: str,  # Intentionally unused; reserved for future per-selector routing
        traces: list[dict[str, Any]],
    ) -> FitnessSignal:
        """Compute a FitnessSignal from a list of traces."""
        if not traces:
            return FitnessSignal()

        outcomes = [t.get("outcome", "") for t in traces]
        durations = [float(t.get("duration_ms", 0.0)) for t in traces]

        error_count = sum(1 for o in outcomes if o == "error")
        failure_rate = error_count / len(traces) if traces else 0.0
        score = 1.0 - failure_rate

        sorted_durations = sorted(durations)
        p99_idx = min(int(len(sorted_durations) * 0.99), len(sorted_durations) - 1)
        p99_latency = sorted_durations[p99_idx] if sorted_durations else 0.0

        now_iso = datetime.now(UTC).isoformat()
        window_start_iso = (datetime.now(UTC) - timedelta(minutes=60)).isoformat()

        return FitnessSignal(
            score=score,
            samples=len(traces),
            failure_rate=failure_rate,
            p99_latency_ms=p99_latency,
            updated_at=now_iso,
            window_start=window_start_iso,
            component_count=len({t.get("component_name", "") for t in traces}),
        )

    async def _analyze_and_persist(self) -> None:
        """Run one analysis cycle: collect traces and update the signal cache.

        The historical Dhara write step was removed when Dhara was
        decommissioned; signals are now stored in ``self._signals``
        and returned in-memory via :meth:`run_fitness_analysis`.
        """
        if not self._component_endpoints:
            logger.debug("FitnessAnalyzer: no component endpoints registered")
            return

        task_classes = ["code_generation", "reasoning", "swarm", "quick", "documentation"]
        all_signals: dict[str, dict[str, FitnessSignal]] = {}

        for task_class in task_classes:
            traces = await self._collect_traces(task_class)
            if not traces:
                continue

            by_selector: dict[str, list[dict[str, Any]]] = {}
            for trace in traces:
                selector = trace.get("selector", "unknown")
                by_selector.setdefault(selector, []).append(trace)

            for selector, selector_traces in by_selector.items():
                signal = self._compute_signal(task_class, selector, selector_traces)
                all_signals.setdefault(task_class, {})[selector] = signal

        if not all_signals:
            logger.debug("FitnessAnalyzer: no traces collected in this cycle")
            return

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
