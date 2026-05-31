"""Fitness analyzer — periodic background job that computes and persists routing fitness signals.

Polls each Bodai component's MCP endpoint (via BodaiComponentMCPClient) for local OTel
traces, computes rolling failure_rate and p99 latency per (task_class, selector) pair,
and writes fitness signals to Dhara at ``routing_fitness/{task_class}/{selector}``.

Bounded in-memory buffer (deque maxlen=1000) holds signals pending Dhara write.
DLQ (dead-letter queue) after 3 consecutive write failures per signal.
Circuit breaker protects Dhara write operations.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from akosha.mcp.client import BodaiComponentMCPClient

if TYPE_CHECKING:
    from oneiric.core.resiliency import CircuitBreaker

logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL_SECONDS = 60
_MAX_BUFFER_SIZE = 1000
_DLQ_FAILURE_THRESHOLD = 3
_DHARA_DEFAULT_URL = "http://localhost:8683"
_KEY_COMPONENT_RE = re.compile(r"^[a-zA-Z0-9_]{1,50}$")
_INVALID_KEY_PLACEHOLDER = "unknown"


def _sanitize_key_component(value: str) -> str:
    """Sanitize a key path component to prevent path injection in Dhara keys."""
    if _KEY_COMPONENT_RE.match(value):
        return value
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", value)[:50]
    return sanitized or _INVALID_KEY_PLACEHOLDER


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


@dataclass
class _BufferEntry:
    """Entry in the bounded in-memory signal buffer."""

    task_class: str
    selector: str
    signal: FitnessSignal
    attempt: int = 0


class FitnessAnalyzer:
    """Periodic fitness signal analyzer.

    Periodically polls known Bodai component endpoints for traces,
    computes aggregated fitness signals, and writes them to Dhara.

    Parameters:
        poll_interval_seconds: Interval between analysis runs (default 60 s)
        dhara_url: Dhara MCP server URL for writing signals
        component_endpoints: List of (component_name, mcp_url) tuples to poll
        circuit_breaker: CircuitBreaker for Dhara write protection
    """

    def __init__(
        self,
        poll_interval_seconds: int = _DEFAULT_POLL_INTERVAL_SECONDS,
        dhara_url: str | None = None,
        component_endpoints: list[tuple[str, str]] | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        self._poll_interval = max(poll_interval_seconds, 1)
        self._dhara_url = dhara_url or os.getenv("DHARA_MCP_URL", _DHARA_DEFAULT_URL)
        self._component_endpoints = component_endpoints or []
        self._circuit_breaker = circuit_breaker

        self._buffer: deque[_BufferEntry] = deque(maxlen=_MAX_BUFFER_SIZE)
        self._dlq_failures: dict[str, int] = {}

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
        client = BodaiComponentMCPClient(base_url=mcp_url, timeout=15.0)
        try:
            return await client.query_local_traces(task_class, time_range_minutes)
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

    async def _write_to_dhara(self, key: str, value: dict[str, Any]) -> None:
        """Write a fitness signal to Dhara via MCP HTTP."""
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{self._dhara_url}/tools/call",
                json={"name": "put", "arguments": {"key": key, "value": value}},
            )
            response.raise_for_status()

    async def _flush_buffer(self) -> None:
        """Attempt to write all buffered signals to Dhara."""
        if not self._buffer:
            return

        while self._buffer:
            entry = self._buffer.popleft()
            safe_task_class = _sanitize_key_component(entry.task_class)
            safe_selector = _sanitize_key_component(entry.selector)
            key = f"routing_fitness/{safe_task_class}/{safe_selector}"
            value = {
                "score": entry.signal.score,
                "samples": entry.signal.samples,
                "failure_rate": entry.signal.failure_rate,
                "p99_latency_ms": entry.signal.p99_latency_ms,
                "updated_at": entry.signal.updated_at,
                "window_start": entry.signal.window_start,
                "component_count": entry.signal.component_count,
            }

            try:
                if self._circuit_breaker is not None:
                    await self._circuit_breaker.call(lambda: self._write_to_dhara(key, value))  # type: ignore[arg-type]
                else:
                    await self._write_to_dhara(key, value)
                self._dlq_failures.pop(key, None)
            except Exception as exc:
                dlq_count = self._dlq_failures.get(key, 0) + 1
                self._dlq_failures[key] = dlq_count
                if dlq_count >= _DLQ_FAILURE_THRESHOLD:
                    logger.error(
                        "DLQ: fitness signal for %s/%s dropped after %d failed writes",
                        entry.task_class,
                        entry.selector,
                        dlq_count,
                    )
                    if key in self._dlq_failures:
                        del self._dlq_failures[key]
                else:
                    self._buffer.append(entry)
                logger.debug(
                    "Failed to write fitness signal %s: %s (attempt %d)",
                    key,
                    exc,
                    dlq_count,
                )

    async def _analyze_and_persist(self) -> None:
        """Run one analysis cycle: collect traces and write signals to Dhara."""
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

        for task_class, selector_signals in all_signals.items():
            for selector, signal in selector_signals.items():
                self._buffer.append(_BufferEntry(task_class, selector, signal))

        if self._buffer:
            await self._flush_buffer()

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

        signals: dict[str, dict[str, FitnessSignal]] = {}
        for entry in self._buffer:
            signals.setdefault(entry.task_class, {})[entry.selector] = entry.signal
        return signals
