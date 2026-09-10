"""Unit tests for ``akosha.mcp.tools.ecosystem_skills_circuit`` (Phase 4).

Covers the per-server circuit breaker that protects the federation tool
against repeated failures (plan §11 H-1): "skip server with ≥3 failures
in last 30s for 60s".

The breaker is consumed from a single asyncio loop, so the threading
lock is defense-in-depth rather than critical. Tests use a synthetic
``clock`` callable (mutable int) to advance "time" deterministically and
avoid ``time.sleep``.
"""

from __future__ import annotations

from typing import Any

import pytest

from akosha.mcp.tools.ecosystem_skills_circuit import (
    CircuitBreakerOpen,
    EcosystemCircuitRegistry,
    EcosystemServerCircuit,
)


class _Clock:
    """Manual clock. ``advance`` pushes the wall forward by ``seconds``."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestEcosystemServerCircuit:
    def test_starts_closed(self) -> None:
        clock = _Clock()
        breaker = EcosystemServerCircuit("akosha", clock=clock)
        assert breaker.is_open() is False
        breaker.allow()  # no exception

    def test_does_not_open_below_threshold(self) -> None:
        clock = _Clock()
        breaker = EcosystemServerCircuit(
            "akosha", max_failures=3, window_seconds=30.0, clock=clock
        )
        # Two failures < threshold 3; breaker remains closed.
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.is_open() is False
        breaker.allow()

    def test_opens_on_threshold(self) -> None:
        clock = _Clock()
        breaker = EcosystemServerCircuit(
            "akosha", max_failures=3, window_seconds=30.0, clock=clock
        )
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.is_open() is True
        with pytest.raises(CircuitBreakerOpen) as exc_info:
            breaker.allow()
        assert exc_info.value.server_key == "akosha"
        assert exc_info.value.retry_after > 0

    def test_sliding_window_drops_old_failures(self) -> None:
        clock = _Clock(now=1000.0)
        breaker = EcosystemServerCircuit(
            "akosha", max_failures=3, window_seconds=30.0, clock=clock
        )
        # Three failures spaced across 35s; at the third, the first
        # is older than the 30s sliding window so only 2 failures are
        # in-window and the breaker MUST NOT open.
        breaker.record_failure()  # t = 1000
        clock.advance(15)
        breaker.record_failure()  # t = 1015
        clock.advance(20)
        breaker.record_failure()  # t = 1035 (> 30s after first)
        assert breaker.is_open() is False

    def test_cooldown_releases_after_window(self) -> None:
        clock = _Clock(now=1000.0)
        breaker = EcosystemServerCircuit(
            "akosha",
            max_failures=3,
            window_seconds=30.0,
            cooldown_seconds=60.0,
            clock=clock,
        )
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.is_open() is True
        # After 60s of cooldown, allow() returns True (half-open)
        clock.advance(60.0)
        assert breaker.allow() is True

    def test_record_success_closes_breaker(self) -> None:
        clock = _Clock(now=1000.0)
        breaker = EcosystemServerCircuit(
            "akosha", max_failures=3, window_seconds=30.0, clock=clock
        )
        # Half-open state
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_failure()
        clock.advance(60.0)
        breaker.allow()  # half-open (returns True)
        # Successful call: closes entirely
        breaker.record_success()
        assert breaker.is_open() is False
        breaker.allow()  # closed → no exception

    def test_reopen_after_half_open_failure(self) -> None:
        clock = _Clock(now=1000.0)
        breaker = EcosystemServerCircuit(
            "akosha", max_failures=3, window_seconds=30.0, cooldown_seconds=60.0, clock=clock
        )
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_failure()
        clock.advance(60.0)
        breaker.allow()  # half-open
        breaker.record_failure()  # reopens immediately
        assert breaker.is_open() is True
        with pytest.raises(CircuitBreakerOpen):
            breaker.allow()

    def test_retry_after_diminishes_with_time(self) -> None:
        clock = _Clock(now=1000.0)
        breaker = EcosystemServerCircuit(
            "akosha", max_failures=2, window_seconds=30.0, cooldown_seconds=60.0, clock=clock
        )
        breaker.record_failure()
        breaker.record_failure()
        # Wait 30s; retry_after should be ~30s.
        clock.advance(30.0)
        with pytest.raises(CircuitBreakerOpen) as exc_info:
            breaker.allow()
        assert exc_info.value.retry_after == pytest.approx(30.0, abs=0.1)

    def test_idempotent_record_success_when_closed(self) -> None:
        clock = _Clock()
        breaker = EcosystemServerCircuit("akosha", clock=clock)
        breaker.record_success()
        breaker.record_success()
        assert breaker.is_open() is False
        breaker.allow()


class TestEcosystemCircuitRegistry:
    def test_returns_same_breaker_on_repeat_calls(self) -> None:
        registry = EcosystemCircuitRegistry()
        b1 = registry.for_server("akosha")
        b2 = registry.for_server("akosha")
        assert b1 is b2

    def test_distinct_breakers_per_server(self) -> None:
        registry = EcosystemCircuitRegistry()
        b1 = registry.for_server("akosha")
        b2 = registry.for_server("mahavishnu")
        assert b1 is not b2

    def test_reset_all_clears_state(self) -> None:
        registry = EcosystemCircuitRegistry()
        clock = _Clock()
        breaker = registry.for_server("akosha")
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.is_open() is True
        # Force the breaker closed via reset_all
        registry.reset_all()
        new_breaker = registry.for_server("akosha")
        assert new_breaker.is_open() is False


def test_module_all_exports() -> None:
    """Sanity: ``__all__`` contains only the public surface."""
    from akosha.mcp.tools import ecosystem_skills_circuit

    assert sorted(ecosystem_skills_circuit.__all__) == [
        "CircuitBreakerOpen",
        "EcosystemCircuitRegistry",
        "EcosystemServerCircuit",
    ]
