"""Heartbeat watcher — P0-A detection over the OTel span substrate.

Spec v7 §11 (``docs/specs/2026-10-19-bodai-substrate-otel-v7.md``), PR-A5.

The watcher answers one question on every cycle: *is every service we expect
to be talking still talking?* It reads the substrate through the existing
read surface (:class:`~akosha.storage.span_query.SpanQuery` — the same one
the fitness analyzer uses) and compares what it finds against a static
roster of known MCP servers.

Three behaviours are load-bearing. Conflating any two of them produces an
answer that looks right and is wrong:

1. **query_empty is not query_threw.** A query that ran and found no rows is
   *evidence*: the producer went quiet. A query that RAISED is a *broken
   substrate*: the database is closed, locked, or corrupt. The second emits
   ``akosha_otlp_heartbeat_query_failed_total{code}`` and SUPPRESSES P0-A —
   an unreadable substrate is not evidence that any producer died, and
   paging on it sends the operator to the wrong system at 3am.

2. **P0-A needs TWO consecutive absences.** One missed heartbeat is a slow
   producer or a race with the write path. Two in a row is a pattern. The
   streak is tracked per service, reset the moment the service is seen
   again, and is NOT advanced by a cycle whose query threw: a cycle that
   could not read the substrate produced no evidence either way. A cycle is
   atomic — if any service's read fails, no service's streak moves.

3. **An empty roster is itself a P0.** If the roster is empty the watcher
   has nothing to watch, and "everything is fine" is the one answer it must
   not give: a registry wipe, or a substrate lift that dropped every entry,
   is indistinguishable from a healthy system until somebody looks. The
   watcher therefore raises the empty-roster P0 on every cycle.

The roster is INJECTED, never imported. This module does not read
``akosha.config``, so it stays independently testable and the roster's home
stays a config concern. The caller populates it at lifespan startup from
``otel.receiver.known_mcp_servers`` (spec §11.4) — a ``list[str]`` of
service names, matched against the substrate's ``service_name`` column.

The watcher reports; it does not page. :meth:`HeartbeatWatcher.check`
returns a :class:`HeartbeatReport` whose ``is_p0`` says whether the cycle
warrants an operator waking up, and the lifespan decides what to do about
it. Metrics and log lines are emitted here so the decision has evidence
attached to it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from prometheus_client import Counter, Gauge

from akosha.observability.prometheus_metrics import get_metrics_registry

if TYPE_CHECKING:
    from collections.abc import Sequence

    from akosha.storage.span_query import SpanHit

logger = logging.getLogger(__name__)

__all__ = [
    "HeartbeatReport",
    "HeartbeatStatus",
    "HeartbeatWatcher",
    "ServiceHeartbeat",
    "ServiceSpanReader",
]

_NS_PER_SECOND = 1_000_000_000


# ── metrics ──────────────────────────────────────────────────────────────
#
# Registered against akosha's shared CollectorRegistry, so they ride the
# ``/metrics`` route already live on the MCP port (:8682) rather than a
# second port the substrate would have to own.


_REGISTRY = get_metrics_registry()

heartbeat_query_failed_total: Counter = Counter(
    name="akosha_otlp_heartbeat_query_failed_total",
    documentation=(
        "Heartbeat queries that RAISED (closed / locked / corrupt substrate). "
        "Distinct from 'the query ran and found no rows' — a failed query is "
        "a broken read path, not a quiet producer, and it suppresses P0-A "
        "while it lasts. The spec names this telemetry.heartbeat_watcher."
        "query_failed{code}; the akosha_otlp_ prefix matches the rest of the "
        "substrate's metrics."
    ),
    labelnames=["code"],
    registry=_REGISTRY,
)

heartbeat_service_absent_total: Counter = Counter(
    name="akosha_otlp_heartbeat_service_absent_total",
    documentation=(
        "Cycles in which a rostered service had no span inside the window. "
        "Counted per service: the first miss is noise, the second is P0-A."
    ),
    labelnames=["service"],
    registry=_REGISTRY,
)

heartbeat_p0_a_total: Counter = Counter(
    name="akosha_otlp_heartbeat_p0_a_total",
    documentation=(
        "P0-A raised: a service missed two consecutive heartbeat windows. "
        "Labelled by service so the alert names the producer that went quiet."
    ),
    labelnames=["service"],
    registry=_REGISTRY,
)

heartbeat_empty_roster_total: Counter = Counter(
    name="akosha_otlp_heartbeat_empty_roster_total",
    documentation=(
        "Cycles in which the known-server roster was empty. That is a P0 "
        "misconfiguration, not a healthy state — there is nothing to watch, "
        "so the watcher cannot detect a dead producer at all."
    ),
    registry=_REGISTRY,
)

heartbeat_last_check_unix_nano: Gauge = Gauge(
    name="akosha_otlp_heartbeat_last_check_unix_nano",
    documentation=(
        "Unix nanos of the last heartbeat cycle. 0 means the watcher has "
        "never run — pair it with the absent counter to tell 'never ran' "
        "from 'ran and found nothing'."
    ),
    registry=_REGISTRY,
)


# ── result types ─────────────────────────────────────────────────────────


class HeartbeatStatus(StrEnum):
    """What one heartbeat cycle concluded.

    ``QUERY_EMPTY`` and ``QUERY_THREW`` are deliberately distinct members
    rather than a boolean: the whole point of §11.1 is that "the query ran
    and found nothing" and "the query could not run" are different facts
    with different consequences.
    """

    OK = "ok"
    QUERY_EMPTY = "query_empty"
    QUERY_THREW = "query_threw"
    EMPTY_ROSTER = "empty_roster"


@dataclass(frozen=True)
class ServiceHeartbeat:
    """One roster service's verdict for one cycle."""

    service_name: str
    status: HeartbeatStatus
    consecutive_absences: int
    p0_a: bool


@dataclass(frozen=True)
class HeartbeatReport:
    """The result of one watcher cycle.

    ``p0_a`` and ``p0_empty_roster`` are kept apart because the operator
    runbooks differ: one is "a producer went quiet" and the other is "the
    roster was never populated". ``is_p0`` is the single question the
    caller asks.
    """

    status: HeartbeatStatus
    services: tuple[ServiceHeartbeat, ...]
    p0_a: bool
    p0_empty_roster: bool
    query_error: str | None
    query_error_code: str | None
    window_seconds: int
    checked_at_unix_nano: int

    @property
    def is_p0(self) -> bool:
        """Whether this cycle must reach an operator."""
        return self.p0_a or self.p0_empty_roster

    @property
    def absent_services(self) -> tuple[str, ...]:
        """Roster services with no span inside the window."""
        return tuple(
            s.service_name for s in self.services if s.status is HeartbeatStatus.QUERY_EMPTY
        )

    @property
    def p0_services(self) -> tuple[str, ...]:
        """Services that crossed the consecutive-absence threshold."""
        return tuple(s.service_name for s in self.services if s.p0_a)


class ServiceSpanReader(Protocol):
    """The slice of the read surface the watcher needs.

    Narrower than :class:`~akosha.storage.span_query.SpanQuery` on purpose:
    the watcher only ever asks "has this service emitted a span since T?".
    Declaring that as a protocol keeps the watcher independent of the
    concrete read surface — and lets a test make ONE service's read fail
    while another's succeeds, which is the only way to reach the
    all-or-nothing cycle semantics from a single shared store.
    """

    async def search(
        self,
        *,
        service_name: str | None = ...,
        since_unix_nano: int | None = ...,
        limit: int = ...,
    ) -> list[SpanHit]: ...


# ── watcher ──────────────────────────────────────────────────────────────


class HeartbeatWatcher:
    """Detects MCP servers that stopped emitting spans (spec §11, P0-A).

    Args:
        reader: The read surface over ``spans_substrate``. The watcher
            never opens its own connection and never writes. In production
            this is ``SpanQuery(hot_store)``.
        known_mcp_servers: The roster of service names to watch. Matched
            against the substrate's ``service_name`` column. Blank entries
            are dropped and duplicates collapsed — a roster entry of ``""``
            would name a service no producer ever emits, so it would read as
            permanently absent and page on its own.
        window_seconds: The lookback window. §11 pins it at 60s.
        absence_threshold: Consecutive empty windows before P0-A. §11 pins
            it at 2 (i.e. 120s of silence).
    """

    DEFAULT_WINDOW_SECONDS: int = 60
    DEFAULT_ABSENCE_THRESHOLD: int = 2

    def __init__(
        self,
        reader: ServiceSpanReader,
        *,
        known_mcp_servers: Sequence[str] = (),
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        absence_threshold: int = DEFAULT_ABSENCE_THRESHOLD,
    ) -> None:
        if window_seconds < 1:
            raise ValueError(f"window_seconds must be >= 1, got {window_seconds}")
        if absence_threshold < 1:
            raise ValueError(f"absence_threshold must be >= 1, got {absence_threshold}")
        self._reader = reader
        self._window_seconds = window_seconds
        self._absence_threshold = absence_threshold
        self._roster: tuple[str, ...] = _normalize_roster(known_mcp_servers)
        self._absences: dict[str, int] = {}

    # ── roster ───────────────────────────────────────────────────────────

    @property
    def known_mcp_servers(self) -> tuple[str, ...]:
        """The roster currently being watched."""
        return self._roster

    def set_known_mcp_servers(self, servers: Sequence[str]) -> None:
        """Replace the roster.

        The lifespan calls this once the layered config is loaded. A server
        that leaves the roster loses its streak (it is no longer watched); a
        server that joins starts at zero, because a service added to the
        roster has not yet been absent for two windows.
        """
        self._roster = _normalize_roster(servers)
        self._absences = {k: v for k, v in self._absences.items() if k in self._roster}

    @property
    def consecutive_absences(self) -> dict[str, int]:
        """Current per-service absence streaks (a copy; mutating it does nothing)."""
        return dict(self._absences)

    def reset(self) -> None:
        """Forget every absence streak."""
        self._absences.clear()

    # ── the cycle ────────────────────────────────────────────────────────

    async def check(self, *, now_unix_nano: int | None = None) -> HeartbeatReport:
        """Run one heartbeat cycle and report what it found.

        Args:
            now_unix_nano: The instant the window is measured back from.
                Defaults to the wall clock. Injecting it makes the window
                boundary explicit in tests.

        Returns:
            A :class:`HeartbeatReport`. This method does not raise for a
            failed query — a broken substrate is a reportable fact, not an
            exception for the caller to handle.
        """
        now = time.time_ns() if now_unix_nano is None else int(now_unix_nano)
        since = now - self._window_seconds * _NS_PER_SECOND
        heartbeat_last_check_unix_nano.set(now)

        if not self._roster:
            return self._empty_roster_report(now=now)

        # Phase 1 — read. No state is mutated here, so a read that raises
        # leaves every streak exactly where it was.
        present: list[bool] = []
        for name in self._roster:
            try:
                hits = await self._reader.search(
                    service_name=name,
                    since_unix_nano=since,
                    limit=1,
                )
            except Exception as exc:
                # §11.1: a query that RAISED is a broken substrate, not a
                # quiet producer. Emit the failure, suppress P0-A, and hand
                # back a report that says so.
                code = _error_code(exc)
                heartbeat_query_failed_total.labels(code=code).inc()
                logger.exception(
                    "akosha.heartbeat_watcher.query_failed code=%s service=%s",
                    code,
                    name,
                )
                return self._query_threw_report(now=now, exc=exc, code=code)
            present.append(bool(hits))

        # Phase 2 — apply. The cycle is atomic: every roster service's
        # streak moves together, and a service seen again resets to zero.
        services: list[ServiceHeartbeat] = []
        p0_a = False
        for name, is_present in zip(self._roster, present, strict=True):
            if is_present:
                self._absences[name] = 0
                services.append(
                    ServiceHeartbeat(
                        service_name=name,
                        status=HeartbeatStatus.OK,
                        consecutive_absences=0,
                        p0_a=False,
                    )
                )
                continue

            streak = self._absences.get(name, 0) + 1
            self._absences[name] = streak
            raised = streak >= self._absence_threshold
            heartbeat_service_absent_total.labels(service=name).inc()
            if raised:
                p0_a = True
                heartbeat_p0_a_total.labels(service=name).inc()
                logger.error(
                    "akosha.heartbeat_watcher.p0_a service=%s consecutive_absences=%s "
                    "window_seconds=%s — no span for %ss",
                    name,
                    streak,
                    self._window_seconds,
                    streak * self._window_seconds,
                )
            services.append(
                ServiceHeartbeat(
                    service_name=name,
                    status=HeartbeatStatus.QUERY_EMPTY,
                    consecutive_absences=streak,
                    p0_a=raised,
                )
            )

        status = (
            HeartbeatStatus.QUERY_EMPTY
            if any(s.status is HeartbeatStatus.QUERY_EMPTY for s in services)
            else HeartbeatStatus.OK
        )
        return HeartbeatReport(
            status=status,
            services=tuple(services),
            p0_a=p0_a,
            p0_empty_roster=False,
            query_error=None,
            query_error_code=None,
            window_seconds=self._window_seconds,
            checked_at_unix_nano=now,
        )

    def _empty_roster_report(self, *, now: int) -> HeartbeatReport:
        """The empty-roster P0. Fires on every cycle, by design.

        An empty roster means the watcher is blind. Reporting "healthy"
        would be the one answer that cannot be acted on: there is no
        producer being watched, so there is no way for the watcher to notice
        that a producer died.
        """
        heartbeat_empty_roster_total.inc()
        logger.error(
            "akosha.heartbeat_watcher.p0_empty_roster — the known-server roster is "
            "empty, so no producer is being watched and a dead producer cannot be "
            "detected",
        )
        return HeartbeatReport(
            status=HeartbeatStatus.EMPTY_ROSTER,
            services=(),
            p0_a=False,
            p0_empty_roster=True,
            query_error=None,
            query_error_code=None,
            window_seconds=self._window_seconds,
            checked_at_unix_nano=now,
        )

    def _query_threw_report(self, *, now: int, exc: BaseException, code: str) -> HeartbeatReport:
        """The report for a cycle whose read failed.

        The per-service streaks are reported as they stand, unmoved: a
        cycle that could not read the substrate produced no evidence about
        any producer, in either direction.
        """
        return HeartbeatReport(
            status=HeartbeatStatus.QUERY_THREW,
            services=tuple(
                ServiceHeartbeat(
                    service_name=name,
                    status=HeartbeatStatus.QUERY_THREW,
                    consecutive_absences=self._absences.get(name, 0),
                    p0_a=False,
                )
                for name in self._roster
            ),
            p0_a=False,
            p0_empty_roster=False,
            query_error=f"{type(exc).__name__}: {exc}",
            query_error_code=code,
            window_seconds=self._window_seconds,
            checked_at_unix_nano=now,
        )

    # ── lifespan task ────────────────────────────────────────────────────

    async def run(
        self,
        stop_event: asyncio.Event,
        *,
        interval_seconds: float | None = None,
    ) -> None:
        """Run the watcher until ``stop_event`` is set.

        Mirrors ``OTLPReceiver.run_retention_sweep``: the first cycle runs
        one full interval after start, and the wait is a cancellable
        ``wait_for`` so the lifespan's cancel/gather sequence behaves the
        same as it does for the receiver's own tasks.

        A cycle that raises for an unforeseen reason is logged and the loop
        continues. A watchdog that dies on the first surprise stops
        watching, and a watcher that stops watching looks exactly like a
        fleet of healthy producers.
        """
        interval = self._window_seconds if interval_seconds is None else interval_seconds
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                report = await self.check()
            except Exception:
                logger.exception("akosha.heartbeat_watcher.cycle_failed")
                continue
            if report.is_p0:
                logger.error(
                    "akosha.heartbeat_watcher.p0 status=%s p0_a=%s empty_roster=%s",
                    report.status,
                    report.p0_a,
                    report.p0_empty_roster,
                )


# ── helpers ──────────────────────────────────────────────────────────────


def _normalize_roster(servers: Sequence[str]) -> tuple[str, ...]:
    """Strip, drop blanks, collapse duplicates — preserving order.

    A blank entry would name a service no producer ever emits, so it would
    read as permanently absent and page on its own. A duplicate would list
    the same server twice in every report.
    """
    seen: set[str] = set()
    normalized: list[str] = []
    for raw in servers:
        name = raw.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        normalized.append(name)
    return tuple(normalized)


#: Closed label set for ``akosha_otlp_heartbeat_query_failed_total{code}``.
#: A code derived from the exception MESSAGE would make the metric a
#: cardinality bomb, and the whole point of the label is that an operator
#: can group failures by kind.
_ERROR_CODES: dict[str, str] = {
    "CatalogException": "catalog",
    "ConnectionException": "connection",
    "IOException": "io",
    "OperationalError": "operational",
    "RuntimeError": "runtime",
    "TimeoutError": "timeout",
    "AttributeError": "not_initialized",
}


def _error_code(exc: BaseException) -> str:
    """A short, stable label for a query failure."""
    return _ERROR_CODES.get(type(exc).__name__, "unknown")
