"""In-memory circuit breaker for the Phase 4 federation tool.

Per plan §11 H-1: a per-server circuit breaker that "skip server with
≥3 failures in last 30s for 60s". The breaker tracks the timestamps of
the last ``MAX_FAILURES`` failures (default 3); when the count in the
last ``WINDOW_SECONDS`` window (default 30s) reaches that threshold,
the breaker opens and skips the server for ``COOLDOWN_SECONDS`` (60s).

States:

* **closed** — healthy. ``allow()`` returns ``True`` unconditionally.
* **open** — tripped. ``allow()`` raises :class:`CircuitBreakerOpen`
  until ``cooldown_seconds`` have elapsed since the trip timestamp.
* **half-open** — cooldown elapsed, one trial call allowed. The next
  ``record_success()`` closes the breaker; the next
  ``record_failure()`` re-opens it (resets the trip timestamp).

This is a lightweight, in-memory implementation rather than a third-
party breaker library. Two reasons:

* Per-server state is independent of process state, so a server-side
  restart drops state and re-probes — matches the plan's "skip for
  60s" semantics (not "trip forever").
* The dependency footprint stays small (we already pull pybreaker /
  circuitbreaker but those are Crackerjack-quality-of-service patterns,
  not the right shape for this transport-level circuit).

Threading note: the breaker is consumed from a single asyncio event
loop (the MCP request handler), so the data structures do not need
locking. If a worker ever runs multiple loops concurrently, wrap the
methods in :func:`asyncio.Lock`.
"""

from __future__ import annotations

import time
from collections import deque
from threading import Lock
from typing import Any


class CircuitBreakerOpen(Exception):
    """Raised internally to short-circuit a call when the breaker is open.

    Callers should NOT raise this through the MCP boundary — the
    federation tool handles it by populating ``errors[server_key]`` with
    a "circuit open" message (per plan §5 task #3 partial-failure
    visibility).
    """

    def __init__(self, server_key: str, retry_after: float) -> None:
        self.server_key = server_key
        self.retry_after = retry_after
        super().__init__(f"circuit breaker open for {server_key!r}; retry in {retry_after:.1f}s")


class EcosystemServerCircuit:
    """Per-server circuit breaker for the federation tool.

    The breaker tracks the last ``max_failures`` (default 3) failure
    timestamps in a sliding window of ``window_seconds`` (default 30s).
    When the sliding-window count reaches the threshold the breaker
    "opens" and skips the server for ``cooldown_seconds`` (default 60s).

    After the cooldown expires the breaker half-opens on the next
    :meth:`allow` call: it returns ``True`` (let the call through) but a
    single new failure re-opens it. A successful call closes the breaker
    entirely (resetting the failure buffer).

    Args:
        server_key: identifier used in error messages.
        max_failures: failures within the window required to open.
        window_seconds: sliding window length.
        cooldown_seconds: how long to skip after tripping.
        clock: callable returning current ``time.time()`` — overridable
            for tests. Defaults to :func:`time.time`.
    """

    MAX_FAILURES = 3
    WINDOW_SECONDS = 30.0
    COOLDOWN_SECONDS = 60.0

    def __init__(
        self,
        server_key: str,
        *,
        max_failures: int = MAX_FAILURES,
        window_seconds: float = WINDOW_SECONDS,
        cooldown_seconds: float = COOLDOWN_SECONDS,
        clock: Any = None,
    ) -> None:
        self._server_key = server_key
        self._max_failures = max(1, max_failures)
        self._window_seconds = window_seconds
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock if clock is not None else time.time

        # ``deque`` of failure timestamps (most recent at the right).
        # Capped at ``max_failures`` so memory stays bounded.
        self._failures: deque[float] = deque(maxlen=self._max_failures)
        # The timestamp at which the breaker last tripped open. ``None``
        # means the breaker is closed. After cooldown elapses, the
        # breaker enters "half-open" implicitly (see :meth:`allow`);
        # this field stays set so :meth:`record_failure` knows the
        # breaker is in half-open and must reopen on a single failure.
        self._opened_at: float | None = None
        # Lock is held only across the small critical section that
        # reads / writes state. ``asyncio`` co-operatively yields
        # between awaits, but the breaker is consumed from a single
        # event loop in practice — the lock is defense-in-depth.
        self._lock = Lock()

    def allow(self) -> bool:
        """Return ``True`` if the next call should proceed.

        Raises :class:`CircuitBreakerOpen` when the breaker is still in
        its cooldown. Cooldown elapsed does NOT close the breaker —
        it transitions implicitly to "half-open"; the next
        :meth:`record_failure` reopens it, :meth:`record_success` closes.
        """
        with self._lock:
            now = self._now()
            if self._opened_at is None:
                return True
            elapsed = now - self._opened_at
            if elapsed >= self._cooldown_seconds:
                # Cooldown elapsed — half-open; the trial call goes through.
                return True
            raise CircuitBreakerOpen(self._server_key, self._cooldown_seconds - elapsed)

    def record_success(self) -> None:
        """Record a successful call: clear the failure buffer and close the breaker.

        Idempotent — safe to call when the breaker is already closed.
        Also closes a half-open breaker (one successful trial call
        closes it).
        """
        with self._lock:
            self._failures.clear()
            self._opened_at = None

    def record_failure(self) -> None:
        """Record a failed call; may trip the breaker.

        Trips when the failure count in the sliding window reaches
        ``max_failures``. The trip timestamp is recorded so subsequent
        :meth:`allow` calls skip the server for ``cooldown_seconds``.

        In half-open state (cooldown elapsed, no success yet), a single
        failure re-opens the breaker immediately and resets the
        cooldown clock.
        """
        with self._lock:
            now = self._now()
            # Half-open state: any failure re-opens immediately. We can
            # detect half-open by checking whether ``_opened_at`` is set
            # but the cooldown has already elapsed.
            if self._opened_at is not None and (now - self._opened_at) >= self._cooldown_seconds:
                # Re-open with a fresh cooldown window; clear the
                # rolling failure buffer so the next trip requires
                # ``max_failures`` again.
                self._failures.clear()
                self._opened_at = now
                return
            # Closed: count failures toward trip threshold. Drop
            # failures older than the sliding window before comparing.
            while self._failures and (now - self._failures[0]) > self._window_seconds:
                self._failures.popleft()
            self._failures.append(now)
            if len(self._failures) >= self._max_failures and self._opened_at is None:
                self._opened_at = now

    def is_open(self) -> bool:
        """Return ``True`` if the breaker is currently open and cooling down.

        Half-open state (cooldown elapsed, awaiting trial result) is
        reported as open so call sites can detect that the breaker is
        not in a "healthy closed" posture.
        """
        with self._lock:
            # ``_opened_at`` is set on the trip-to-open transition and
            # cleared on a successful trial; any non-``None`` value means
            # we're either still cooling down or in half-open.
            return self._opened_at is not None

    def _now(self) -> float:
        return float(self._clock())


class EcosystemCircuitRegistry:
    """Holds one :class:`EcosystemServerCircuit` per server key.

    The federation tool constructs one instance at module-import time
    and reuses it across requests. Server keys are added lazily on the
    first call — this matches the dynamic registration pattern (new
    Bodai components can join without code changes).
    """

    def __init__(
        self,
        *,
        max_failures: int = EcosystemServerCircuit.MAX_FAILURES,
        window_seconds: float = EcosystemServerCircuit.WINDOW_SECONDS,
        cooldown_seconds: float = EcosystemServerCircuit.COOLDOWN_SECONDS,
    ) -> None:
        self._breakers: dict[str, EcosystemServerCircuit] = {}
        self._max_failures = max_failures
        self._window_seconds = window_seconds
        self._cooldown_seconds = cooldown_seconds

    def for_server(self, server_key: str) -> EcosystemServerCircuit:
        """Return the breaker for ``server_key``, creating it on first access."""
        breaker = self._breakers.get(server_key)
        if breaker is None:
            breaker = EcosystemServerCircuit(
                server_key,
                max_failures=self._max_failures,
                window_seconds=self._window_seconds,
                cooldown_seconds=self._cooldown_seconds,
            )
            self._breakers[server_key] = breaker
        return breaker

    def reset_all(self) -> None:
        """Clear all breakers. Used in tests to start from a clean state."""
        self._breakers.clear()


__all__ = [
    "CircuitBreakerOpen",
    "EcosystemCircuitRegistry",
    "EcosystemServerCircuit",
]
