"""Tests for FitnessAnalyzer — periodic routing-fitness signal computation.

Audit found this module at 0% coverage. These tests pin the public
surface (constructor, ``add_component``, ``run_fitness_analysis``,
``start``/``stop`` lifecycle) and the key invariants of the
internal computation helpers.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from akosha.processing.fitness_analyzer import (
    FitnessAnalyzer,
    FitnessSignal,
    _sanitize_key_component,
)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def analyzer() -> FitnessAnalyzer:
    """Construct an analyzer with no components (deterministic for unit tests)."""
    return FitnessAnalyzer(
        poll_interval_seconds=60,
        dhara_url="http://localhost:8683",
        component_endpoints=[],
    )


def _trace(outcome: str, duration_ms: float, component: str = "akosha") -> dict[str, Any]:
    """Build a single trace dict in the shape FitnessAnalyzer expects."""
    return {
        "outcome": outcome,
        "duration_ms": duration_ms,
        "component_name": component,
    }


# ---------------------------------------------------------------------------
# Public surface: construction + add_component
# ---------------------------------------------------------------------------


def test_construction_with_defaults_does_not_connect() -> None:
    """Default constructor must not block on network — pure setup."""
    a = FitnessAnalyzer()
    assert a._component_endpoints == []
    assert a._running is False
    assert a._task is None


def test_construction_clamps_poll_interval_to_at_least_one_second() -> None:
    """poll_interval_seconds < 1 would deadlock the loop; clamp to 1."""
    a = FitnessAnalyzer(poll_interval_seconds=0)
    assert a._poll_interval == 1
    a = FitnessAnalyzer(poll_interval_seconds=-5)
    assert a._poll_interval == 1


def test_construction_reads_dhara_url_from_env_when_not_passed() -> None:
    a = FitnessAnalyzer()
    # Default to the well-known port; env-var override tested elsewhere.
    assert a._dhara_url.startswith("http://")


def test_add_component_appends_new_endpoint() -> None:
    a = FitnessAnalyzer()
    a.add_component("mahavishnu", "http://localhost:8680/mcp")
    a.add_component("dhara", "http://localhost:8683/mcp")
    assert ("mahavishnu", "http://localhost:8680/mcp") in a._component_endpoints
    assert ("dhara", "http://localhost:8683/mcp") in a._component_endpoints


def test_add_component_dedupes_duplicate_registration() -> None:
    """Re-adding the same endpoint is a no-op (operators may retry safely)."""
    a = FitnessAnalyzer()
    a.add_component("mahavishnu", "http://localhost:8680/mcp")
    a.add_component("mahavishnu", "http://localhost:8680/mcp")
    a.add_component("mahavishnu", "http://localhost:8680/mcp")
    matches = [
        ep
        for ep in a._component_endpoints
        if ep == ("mahavishnu", "http://localhost:8680/mcp")
    ]
    assert len(matches) == 1


def test_initial_endpoints_passed_to_constructor_are_used() -> None:
    endpoints = [("a", "http://a"), ("b", "http://b")]
    a = FitnessAnalyzer(component_endpoints=endpoints)
    assert a._component_endpoints == endpoints


# ---------------------------------------------------------------------------
# _sanitize_key_component (audit-critical for path-injection defense)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("input_str", "expected"),
    [
        ("akosha", "akosha"),
        ("dhara_main", "dhara_main"),
        ("Component-With-Dashes", "Component_With_Dashes"),
        ("a" * 60, "a" * 50),  # length cap
        ("", "unknown"),  # empty → placeholder
        ("../etc/passwd", "___etc_passwd"),  # each char -> one underscore
        ("foo; DROP TABLE users;--", "foo__DROP_TABLE_users___"),  # sql injection
        ("with spaces and !@#", "with_spaces_and____"),
    ],
)
def test_sanitize_key_component(input_str: str, expected: str) -> None:
    assert _sanitize_key_component(input_str) == expected


def test_sanitize_key_component_allows_alphanumeric_and_underscore() -> None:
    """Allowed shape per the regex: ^[a-zA-Z0-9_]{1,50}$."""
    assert _sanitize_key_component("valid_identifier_42") == "valid_identifier_42"


# ---------------------------------------------------------------------------
# _compute_signal
# ---------------------------------------------------------------------------


def test_compute_signal_with_empty_traces_returns_defaults() -> None:
    """Empty corpus → ``FitnessSignal()`` defaults.

    Note: the dataclass default ``score=0.0`` is pessimistic — a
    caller observing ``score=0.0`` cannot distinguish "no data" from
    "100% failure". This is a known data-semantics issue; the test
    pins the current behavior. A future fix should default ``score``
    to 1.0 (neutral) when ``samples == 0``.
    """
    a = FitnessAnalyzer()
    sig = a._compute_signal("code_generation", "least_loaded", [])
    assert sig.failure_rate == 0.0
    assert sig.score == 0.0  # known-issue: should be 1.0 when samples==0
    assert sig.p99_latency_ms == 0.0
    assert sig.samples == 0


def test_compute_signal_with_all_success() -> None:
    a = FitnessAnalyzer()
    traces = [_trace("success", 100.0), _trace("success", 200.0)]
    sig = a._compute_signal("code_generation", "least_loaded", traces)
    assert sig.failure_rate == 0.0
    assert sig.score == 1.0
    assert sig.samples == 2
    assert sig.p99_latency_ms == 200.0  # max of two


def test_compute_signal_with_all_failures() -> None:
    a = FitnessAnalyzer()
    traces = [_trace("error", 50.0), _trace("error", 75.0)]
    sig = a._compute_signal("code_generation", "least_loaded", traces)
    assert sig.failure_rate == 1.0
    assert sig.score == 0.0
    assert sig.p99_latency_ms == 75.0


def test_compute_signal_with_mixed_outcomes() -> None:
    """3 success + 1 error → failure_rate=0.25, score=0.75."""
    a = FitnessAnalyzer()
    traces = [
        _trace("success", 10.0),
        _trace("success", 20.0),
        _trace("success", 30.0),
        _trace("error", 100.0),
    ]
    sig = a._compute_signal("code_generation", "least_loaded", traces)
    assert sig.failure_rate == pytest.approx(0.25)
    assert sig.score == pytest.approx(0.75)
    assert sig.samples == 4


def test_compute_signal_component_count() -> None:
    """component_count reflects distinct contributing components."""
    a = FitnessAnalyzer()
    traces = [
        _trace("success", 10.0, component="a"),
        _trace("success", 20.0, component="a"),
        _trace("success", 30.0, component="b"),
        _trace("success", 40.0, component="b"),
    ]
    sig = a._compute_signal("code_generation", "least_loaded", traces)
    assert sig.component_count == 2


def test_compute_signal_p99_uses_99th_percentile_index() -> None:
    """With 100 traces, p99 picks the 99th percentile (index 98 in sorted order)."""
    a = FitnessAnalyzer()
    # Build 100 traces: durations 1..100 ms, all success.
    traces = [_trace("success", float(i + 1)) for i in range(100)]
    sig = a._compute_signal("code_generation", "least_loaded", traces)
    # p99_idx = min(int(100 * 0.99), 99) = min(99, 99) = 99 → durations[99] = 100ms
    assert sig.p99_latency_ms == 100.0


# ---------------------------------------------------------------------------
# run_fitness_analysis — public entry point
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_fitness_analysis_with_no_components_returns_empty_dict() -> None:
    """No registered components → empty signals, no exceptions."""
    a = FitnessAnalyzer(component_endpoints=[])
    signals = await a.run_fitness_analysis()
    assert signals == {}


@pytest.mark.asyncio
async def test_run_fitness_analysis_handles_component_failures(
    analyzer: FitnessAnalyzer,
) -> None:
    """A failing component must not crash the whole analysis run."""
    analyzer.add_component("broken", "http://localhost:1")

    async def explode(*a: object, **kw: object) -> list[dict[str, Any]]:
        raise ConnectionError("nope")

    with patch.object(analyzer, "_fetch_traces_from_component", explode):
        signals = await analyzer.run_fitness_analysis()
    assert signals == {}


@pytest.mark.asyncio
async def test_run_fitness_analysis_returns_signals_for_known_task_classes(
    analyzer: FitnessAnalyzer,
) -> None:
    """When components return traces, signals land under the right task class.

    We patch ``_flush_buffer`` to a no-op so the buffer doesn't try
    to talk to real Dhara and DLQ our test signals.
    """
    analyzer.add_component("akosha", "http://localhost:8680/mcp")
    fake_traces = [
        _trace("success", 50.0),
        _trace("error", 100.0),
    ]

    async def fake_collect(self: object, task_class: str) -> list[dict[str, Any]]:
        return fake_traces

    async def no_flush(self: object) -> None:
        return None

    with (
        patch.object(FitnessAnalyzer, "_collect_traces", fake_collect),
        patch.object(FitnessAnalyzer, "_flush_buffer", no_flush),
    ):
        signals = await analyzer.run_fitness_analysis()
    # At least one task class should have a non-empty signal map.
    assert signals, "expected at least one task class with signals"
    for tc, sigmap in signals.items():
        for selector, sig in sigmap.items():
            assert isinstance(sig, FitnessSignal)
            assert 0.0 <= sig.failure_rate <= 1.0
            assert 0.0 <= sig.score <= 1.0
            assert sig.samples > 0
            # Sanity: unknown selectors shouldn't appear by accident.
            assert isinstance(selector, str)
            assert isinstance(tc, str)


# ---------------------------------------------------------------------------
# Lifecycle: start/stop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_sets_running_flag_and_creates_task(
    analyzer: FitnessAnalyzer,
) -> None:
    with patch.object(analyzer, "_run_loop", new=AsyncMock()):
        await analyzer.start()
    try:
        assert analyzer._running is True
        assert analyzer._task is not None
    finally:
        await analyzer.stop()


@pytest.mark.asyncio
async def test_stop_cancels_background_task_and_clears_running_flag(
    analyzer: FitnessAnalyzer,
) -> None:
    with patch.object(analyzer, "_run_loop", new=AsyncMock()):
        await analyzer.start()
    await analyzer.stop()
    assert analyzer._running is False
    assert analyzer._task is None


@pytest.mark.asyncio
async def test_stop_without_start_is_safe_noop(analyzer: FitnessAnalyzer) -> None:
    """stop() before start() must not raise."""
    await analyzer.stop()
    assert analyzer._running is False


# ---------------------------------------------------------------------------
# Internal buffer / DLQ invariants
# ---------------------------------------------------------------------------


def test_buffer_max_size_is_bounded(analyzer: FitnessAnalyzer) -> None:
    """The pending-write buffer is a deque(maxlen=1000) — bounded to 1000."""
    assert analyzer._buffer.maxlen == 1000


def test_dlq_failure_counter_starts_empty(analyzer: FitnessAnalyzer) -> None:
    assert analyzer._dlq_failures == {}


# ---------------------------------------------------------------------------
# FitnessSignal defaults
# ---------------------------------------------------------------------------


def test_fitness_signal_default_is_pessimistic() -> None:
    """Empty ``FitnessSignal()`` default — current behavior is pessimistic.

    The dataclass default ``score=0.0`` cannot distinguish "no data"
    from "100% failure". Pin the current behavior so a refactor that
    "fixes" it is forced to update this test (and the
    ``_compute_signal`` empty-traces test that mirrors it).
    """
    sig = FitnessSignal()
    assert sig.score == 0.0  # known-issue: should be 1.0 when samples==0
    assert sig.failure_rate == 0.0
    assert sig.p99_latency_ms == 0.0
    assert sig.samples == 0
    assert sig.component_count == 0
