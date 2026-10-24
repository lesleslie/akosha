"""PR-A5: the heartbeat watcher — P0-A detection over the span substrate.

The spec's §14 matrix pins four cases: zero rows seeded (``query_empty``),
the database closed (``query_threw``, P0-A suppressed), two consecutive
absences (P0-A), and an empty roster (P0). All four are here, plus both
directions of the "two consecutive absences" rule — a watcher that fires on
the FIRST missed heartbeat is just as broken as one that never fires, and
only one of the two is a typo away.

Rows are seeded through the real OTLP receiver, not by hand-rolled INSERTs.
The watcher matches on the ``service_name`` column the receiver populates
from the OTLP resource attributes; a hand-written INSERT that set the column
differently would let a real mapping bug pass unnoticed.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import pytest
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.resource.v1 import resource_pb2
from opentelemetry.proto.trace.v1 import trace_pb2
from prometheus_client import Counter, Gauge, generate_latest

from akosha.observability.heartbeat_watcher import (
    HeartbeatReport,
    HeartbeatStatus,
    HeartbeatWatcher,
    heartbeat_empty_roster_total,
    heartbeat_last_check_unix_nano,
    heartbeat_p0_a_total,
    heartbeat_query_failed_total,
    heartbeat_service_absent_total,
)
from akosha.observability.otlp_receiver import OTLPReceiver
from akosha.observability.prometheus_metrics import get_metrics_registry
from akosha.storage.hot_store import HotStore
from akosha.storage.span_query import SpanHit, SpanQuery

if TYPE_CHECKING:
    from prometheus_client.samples import Sample

NS_PER_SECOND = 1_000_000_000

# Captured at import time. The watcher module registered its metrics
# against whatever registry was current when it was imported, which is
# this object. ``reset_all_metrics()`` zeroes metrics in place and does
# NOT replace the shared registry, so this object stays the live one for
# the life of the process.
_SHARED_REGISTRY = get_metrics_registry()


@pytest.fixture
async def store() -> HotStore:
    hs = HotStore(database_path=":memory:", embedding_dim=384)
    await hs.initialize()
    return hs


@pytest.fixture
def query(store: HotStore) -> SpanQuery:
    return SpanQuery(store)


@pytest.fixture
def receiver(store: HotStore) -> OTLPReceiver:
    return OTLPReceiver(store, grpc_port=0, http_port=0)


@pytest.fixture
def watcher(query: SpanQuery) -> HeartbeatWatcher:
    return HeartbeatWatcher(query, known_mcp_servers=["mahavishnu", "akosha"])


# ── helpers ──────────────────────────────────────────────────────────────


def _span_request(span_id: bytes, *, service_name: str) -> Any:
    """A real OTLP export carrying one span for ``service_name``."""

    def kv(pairs: list[tuple[str, str]]) -> list[Any]:
        return [
            common_pb2.KeyValue(key=k, value=common_pb2.AnyValue(string_value=v)) for k, v in pairs
        ]

    return trace_service_pb2.ExportTraceServiceRequest(
        resource_spans=[
            trace_pb2.ResourceSpans(
                resource=resource_pb2.Resource(
                    attributes=kv(
                        [
                            ("service.name", service_name),
                            ("service.namespace", "bodai"),
                        ]
                    )
                ),
                scope_spans=[
                    trace_pb2.ScopeSpans(
                        spans=[
                            trace_pb2.Span(
                                trace_id=b"\x0a" * 16,
                                span_id=span_id,
                                name="heartbeat-probe-span",
                                start_time_unix_nano=1_000_000_000,
                                end_time_unix_nano=2_000_000_000,
                            )
                        ]
                    )
                ],
            )
        ]
    )


async def _seed(receiver: OTLPReceiver, service_name: str, *, span_id: bytes) -> None:
    """Write one span through the real ingest path."""
    result = await receiver._handle_batch(_span_request(span_id, service_name=service_name))
    assert result["inserted"] == 1, f"seed failed for {service_name}: {result}"


def _samples(counter: Counter, sample_name: str) -> list[Sample]:
    """The samples a counter currently exposes, by sample name.

    Read off the metric object rather than off the exposition blob: the
    blob mixes every metric in the shared registry, and the object is the
    one counter under test.
    """
    found: list[Sample] = []
    for metric in counter.collect():
        for sample in metric.samples:
            if sample.name == sample_name:
                found.append(sample)
    return found


def _counter_value(counter: Counter, sample_name: str, **labels: str) -> float:
    """One counter sample's value, matched on its labels. 0.0 if absent."""
    for sample in _samples(counter, sample_name):
        if all(sample.labels.get(key) == value for key, value in labels.items()):
            return sample.value
    return 0.0


def _label_values(counter: Counter, sample_name: str, label_key: str) -> dict[str, float]:
    """Every value of one counter, keyed by one label's value."""
    return {
        sample.labels.get(label_key, ""): sample.value for sample in _samples(counter, sample_name)
    }


def _gauge_value(gauge: Gauge, sample_name: str) -> float:
    """A gauge's current value, read off the metric object itself."""
    for metric in gauge.collect():
        for sample in metric.samples:
            if sample.name == sample_name:
                return sample.value
    return 0.0


class _FlakyQuery:
    """A reader whose read fails for ONE service on ONE chosen call.

    Two reads against one shared store cannot fail independently in
    production — the store dies as a whole — so the all-or-nothing cycle
    semantics ("if any service's read fails, no service's streak moves")
    are unreachable without a stub. Without this, an implementation that
    advanced the first service's streak and then discovered the second
    one's failure would pass every other test in this file.

    ``fail_on_call`` is 1-indexed per service, so the stub can succeed for
    a cycle (establishing the streaks) and then fail on the next one.
    """

    def __init__(self, inner: SpanQuery, *, failing: str, fail_on_call: int = 1) -> None:
        self._inner = inner
        self._failing = failing
        self._fail_on_call = fail_on_call
        self._calls: dict[str, int] = {}

    async def search(
        self,
        *,
        service_name: str | None = None,
        since_unix_nano: int | None = None,
        limit: int = 50,
    ) -> list[SpanHit]:
        if service_name is not None and service_name == self._failing:
            seen = self._calls.get(service_name, 0) + 1
            self._calls[service_name] = seen
            if seen == self._fail_on_call:
                raise RuntimeError("synthetic read failure")
        return await self._inner.search(
            service_name=service_name,
            since_unix_nano=since_unix_nano,
            limit=limit,
        )


# ── query_empty vs query_threw ───────────────────────────────────────────


async def test_an_unseeded_substrate_reports_query_empty(watcher: HeartbeatWatcher) -> None:
    """The query RAN and found nothing. That is a fact about the producers.

    Not an error: the substrate is healthy, the rostered producers have
    simply not written a span yet.
    """
    report = await watcher.check()

    assert report.status is HeartbeatStatus.QUERY_EMPTY
    assert report.query_error is None
    assert report.query_error_code is None
    assert report.p0_a is False
    assert report.is_p0 is False
    assert {s.service_name for s in report.services} == {"mahavishnu", "akosha"}
    assert all(s.status is HeartbeatStatus.QUERY_EMPTY for s in report.services)
    assert all(s.consecutive_absences == 1 for s in report.services)


async def test_a_closed_database_reports_query_threw(
    store: HotStore, watcher: HeartbeatWatcher
) -> None:
    """The query RAISED. That is a fact about the substrate, not the producers.

    The distinction is the whole of §11.1: an empty result and a failed
    query are different events with different consequences, and reporting
    them with one value is how a dead database gets diagnosed as a dead
    producer.
    """
    await store.close()

    report = await watcher.check()

    assert report.status is HeartbeatStatus.QUERY_THREW
    assert report.status is not HeartbeatStatus.QUERY_EMPTY
    assert report.query_error is not None
    assert "ConnectionException" in report.query_error
    assert report.query_error_code == "connection"
    assert report.p0_a is False
    assert report.is_p0 is False
    assert all(s.status is HeartbeatStatus.QUERY_THREW for s in report.services)


async def test_a_query_threw_suppresses_p0_a(store: HotStore, query: SpanQuery) -> None:
    """P0-A is held off while the substrate cannot be read.

    An unreadable substrate is not evidence that a producer died. Paging
    on it sends the operator to the wrong system — and the P0 it would
    raise is the P0 that means "the telemetry store is broken", which the
    ``query_failed`` counter already reports.
    """
    watcher = HeartbeatWatcher(query, known_mcp_servers=["mahavishnu"])
    await watcher.check()
    second = await watcher.check()
    assert second.p0_a is True, "precondition: P0-A fires on the second absence"

    await store.close()
    report = await watcher.check()

    assert report.status is HeartbeatStatus.QUERY_THREW
    assert report.p0_a is False
    assert report.is_p0 is False
    assert report.p0_services == ()


async def test_a_failed_cycle_leaves_every_streak_untouched(
    store: HotStore, query: SpanQuery
) -> None:
    """A cycle that could not read the substrate moves no streaks at all.

    Two failures here are silent. If a failed read advanced the counter,
    a long substrate outage would manufacture a P0-A for every service in
    the roster the moment the database came back — the outage would look
    like a fleet-wide producer death. If it RESET the counter, a genuine
    producer death that started during the outage would be forgiven.
    """
    flaky = _FlakyQuery(query, failing="akosha", fail_on_call=2)
    watcher = HeartbeatWatcher(flaky, known_mcp_servers=["mahavishnu", "akosha"])

    await watcher.check()
    assert watcher.consecutive_absences == {"mahavishnu": 1, "akosha": 1}

    report = await watcher.check()

    assert report.status is HeartbeatStatus.QUERY_THREW
    # "mahavishnu" was read successfully and was absent, so a
    # per-service implementation would have advanced it to 2 here.
    assert watcher.consecutive_absences == {"mahavishnu": 1, "akosha": 1}
    assert report.p0_a is False


# ── two consecutive absences ─────────────────────────────────────────────


async def test_a_single_absence_does_not_raise_p0_a(query: SpanQuery) -> None:
    """One missed heartbeat is a slow producer, not a dead one.

    A producer that flushes on a 90s cadence misses a 60s window roughly
    one time in three. Firing on the first miss would page for that.
    """
    watcher = HeartbeatWatcher(query, known_mcp_servers=["mahavishnu"])

    report = await watcher.check()

    assert report.status is HeartbeatStatus.QUERY_EMPTY
    assert report.services[0].consecutive_absences == 1
    assert report.services[0].p0_a is False
    assert report.p0_a is False
    assert report.is_p0 is False


async def test_two_consecutive_absences_raise_p0_a(query: SpanQuery) -> None:
    """Two in a row is the pattern §11 asks for (2 x 60s = 120s)."""
    watcher = HeartbeatWatcher(query, known_mcp_servers=["mahavishnu"])

    await watcher.check()
    report = await watcher.check()

    assert report.status is HeartbeatStatus.QUERY_EMPTY
    assert report.services[0].consecutive_absences == 2
    assert report.services[0].p0_a is True
    assert report.p0_a is True
    assert report.is_p0 is True
    assert report.p0_services == ("mahavishnu",)


async def test_a_service_seen_again_resets_its_streak(
    store: HotStore, query: SpanQuery, receiver: OTLPReceiver
) -> None:
    """A producer that comes back is forgiven.

    Without the reset, a producer that flaps — quiet for one window, loud
    for one, quiet for one — would eventually cross the threshold and page
    for a service that is demonstrably alive.
    """
    watcher = HeartbeatWatcher(query, known_mcp_servers=["mahavishnu"])
    await watcher.check()
    assert watcher.consecutive_absences == {"mahavishnu": 1}

    await _seed(receiver, "mahavishnu", span_id=b"\x21" * 8)
    present = await watcher.check()
    assert present.status is HeartbeatStatus.OK
    assert watcher.consecutive_absences == {"mahavishnu": 0}

    store.conn.execute("DELETE FROM spans_substrate")  # type: ignore[union-attr]
    absent = await watcher.check()
    assert absent.services[0].consecutive_absences == 1
    assert absent.p0_a is False


# ── presence ─────────────────────────────────────────────────────────────


async def test_a_service_that_emitted_a_span_is_present(
    query: SpanQuery, receiver: OTLPReceiver
) -> None:
    """The happy path: the rostered service is writing, the watcher says so."""
    await _seed(receiver, "mahavishnu", span_id=b"\x22" * 8)
    watcher = HeartbeatWatcher(query, known_mcp_servers=["mahavishnu"])

    report = await watcher.check()

    assert report.status is HeartbeatStatus.OK
    assert report.services[0].status is HeartbeatStatus.OK
    assert report.services[0].consecutive_absences == 0
    assert report.p0_a is False
    assert report.is_p0 is False


async def test_only_the_absent_service_is_reported_absent(
    query: SpanQuery, receiver: OTLPReceiver
) -> None:
    """One quiet producer in a noisy fleet is still a quiet producer."""
    await _seed(receiver, "mahavishnu", span_id=b"\x23" * 8)
    watcher = HeartbeatWatcher(query, known_mcp_servers=["mahavishnu", "akosha"])

    report = await watcher.check()

    assert report.status is HeartbeatStatus.QUERY_EMPTY
    assert report.absent_services == ("akosha",)
    by_name = {s.service_name: s for s in report.services}
    assert by_name["mahavishnu"].status is HeartbeatStatus.OK
    assert by_name["akosha"].status is HeartbeatStatus.QUERY_EMPTY


async def test_a_row_older_than_the_window_does_not_count(
    store: HotStore, query: SpanQuery, receiver: OTLPReceiver
) -> None:
    """The window is a window, not "since the beginning of time".

    A span written two minutes ago says the producer was alive two minutes
    ago. Counting it would make a dead producer look healthy forever —
    the substrate's retention is 30 days, so the row outlives any outage
    it might be evidence of.
    """
    await _seed(receiver, "mahavishnu", span_id=b"\x24" * 8)
    now = time.time_ns()
    store.conn.execute(  # type: ignore[union-attr]
        "UPDATE spans_substrate SET received_at_unix_nano = ?",
        [now - 120 * NS_PER_SECOND],
    )
    watcher = HeartbeatWatcher(query, known_mcp_servers=["mahavishnu"])

    report = await watcher.check(now_unix_nano=now)

    assert report.status is HeartbeatStatus.QUERY_EMPTY
    assert report.services[0].consecutive_absences == 1


# ── the empty roster is a P0 ─────────────────────────────────────────────


async def test_empty_known_servers_roster_emits_p0(query: SpanQuery) -> None:
    """An empty roster is a misconfiguration, not "everything is fine".

    The watcher has nothing to watch. Reporting healthy would be the one
    answer it cannot be allowed to give: a registry wipe, or a substrate
    lift that dropped every entry, looks exactly like a healthy system
    until somebody looks.
    """
    watcher = HeartbeatWatcher(query, known_mcp_servers=[])

    report = await watcher.check()

    assert report.p0_empty_roster is True
    assert report.is_p0 is True
    assert report.status is HeartbeatStatus.EMPTY_ROSTER
    assert report.services == ()
    assert report.p0_a is False


async def test_the_empty_roster_p0_fires_on_every_cycle(query: SpanQuery) -> None:
    """Not once at boot — every cycle, until the roster is populated."""
    watcher = HeartbeatWatcher(query, known_mcp_servers=[])

    first = await watcher.check()
    second = await watcher.check()

    assert first.p0_empty_roster is True
    assert second.p0_empty_roster is True
    assert second.is_p0 is True


async def test_an_empty_roster_does_not_read_the_substrate(
    store: HotStore, query: SpanQuery
) -> None:
    """With nothing to watch there is nothing to ask.

    A closed database and an empty roster both deserve a P0, but they are
    different P0s with different runbooks, and the report must name which
    one it is.
    """
    watcher = HeartbeatWatcher(query, known_mcp_servers=[])
    await store.close()

    report = await watcher.check()

    assert report.status is HeartbeatStatus.EMPTY_ROSTER
    assert report.p0_empty_roster is True
    assert report.query_error is None


# ── roster handling ──────────────────────────────────────────────────────


def test_the_roster_drops_blank_and_duplicate_entries(query: SpanQuery) -> None:
    """A blank entry names a service no producer emits.

    Left in, it would read as permanently absent and page on its own after
    two windows — a P0 with no cause. A duplicate would list the same
    server twice in every report.
    """
    watcher = HeartbeatWatcher(
        query,
        known_mcp_servers=["mahavishnu", "", "  ", "akosha", "mahavishnu"],
    )

    assert watcher.known_mcp_servers == ("mahavishnu", "akosha")


async def test_setting_a_new_roster_clears_departed_streaks(query: SpanQuery) -> None:
    """A server that leaves the roster is no longer watched.

    Its streak has to go with it, or the count survives the server and
    reappears as a P0-A for a service that is not even being watched any
    more.
    """
    watcher = HeartbeatWatcher(query, known_mcp_servers=["mahavishnu", "akosha"])
    await watcher.check()
    assert watcher.consecutive_absences == {"mahavishnu": 1, "akosha": 1}

    watcher.set_known_mcp_servers(["mahavishnu"])

    assert watcher.known_mcp_servers == ("mahavishnu",)
    assert watcher.consecutive_absences == {"mahavishnu": 1}


async def test_a_service_added_to_the_roster_starts_from_zero(query: SpanQuery) -> None:
    """A new roster entry has not been absent for two windows.

    Starting it at 1 — or worse, counting the cycle that added it as an
    absence — would page for a service that was never watched.
    """
    watcher = HeartbeatWatcher(query, known_mcp_servers=["mahavishnu"])
    await watcher.check()
    await watcher.check()

    watcher.set_known_mcp_servers(["mahavishnu", "akosha"])
    report = await watcher.check()

    by_name = {s.service_name: s for s in report.services}
    assert by_name["akosha"].consecutive_absences == 1
    assert by_name["mahavishnu"].consecutive_absences == 3


# ── metrics ──────────────────────────────────────────────────────────────


async def test_the_query_failed_metric_is_incremented(
    store: HotStore, watcher: HeartbeatWatcher
) -> None:
    """The failure is counted separately from the absence.

    Without its own counter, "the database is closed" and "the producers
    are quiet" both look like "the watcher found nothing", and the
    operator has no way to tell a broken read path from a dead fleet.
    """
    sample_name = "akosha_otlp_heartbeat_query_failed_total"
    before = _counter_value(heartbeat_query_failed_total, sample_name, code="connection")

    await store.close()
    await watcher.check()

    after = _counter_value(heartbeat_query_failed_total, sample_name, code="connection")
    assert after == before + 1
    assert "connection" in _label_values(heartbeat_query_failed_total, sample_name, "code")


async def test_the_absent_and_p0_metrics_are_incremented(query: SpanQuery) -> None:
    """One counter for "a window went by with nothing", one for the P0.

    The first says a producer is quiet; the second says it has been quiet
    long enough to matter. Collapsing them loses the difference between
    "starting to look wrong" and "look away now".
    """
    absent_name = "akosha_otlp_heartbeat_service_absent_total"
    p0_name = "akosha_otlp_heartbeat_p0_a_total"
    absent_before = _counter_value(
        heartbeat_service_absent_total, absent_name, service="mahavishnu"
    )
    p0_before = _counter_value(heartbeat_p0_a_total, p0_name, service="mahavishnu")

    watcher = HeartbeatWatcher(query, known_mcp_servers=["mahavishnu"])
    await watcher.check()
    assert (
        _counter_value(heartbeat_service_absent_total, absent_name, service="mahavishnu")
        == absent_before + 1
    )
    assert _counter_value(heartbeat_p0_a_total, p0_name, service="mahavishnu") == p0_before

    await watcher.check()
    assert (
        _counter_value(heartbeat_service_absent_total, absent_name, service="mahavishnu")
        == absent_before + 2
    )
    assert _counter_value(heartbeat_p0_a_total, p0_name, service="mahavishnu") == p0_before + 1


async def test_the_empty_roster_metric_is_incremented(query: SpanQuery) -> None:
    """The misconfiguration is counted, so it can be graphed over time.

    A P0 that fires on every cycle and then stops being mentioned is
    how a roster that was never populated becomes permanent background
    noise nobody is looking at any more.
    """
    sample_name = "akosha_otlp_heartbeat_empty_roster_total"
    before = _counter_value(heartbeat_empty_roster_total, sample_name)

    watcher = HeartbeatWatcher(query, known_mcp_servers=[])
    await watcher.check()

    assert _counter_value(heartbeat_empty_roster_total, sample_name) == before + 1


def test_the_watcher_metrics_land_in_the_shared_registry() -> None:
    """They must ride the /metrics route already on :8682, not a new port.

    A second CollectorRegistry would mean a second scrape endpoint, and
    the substrate's metrics would be invisible to whatever already scrapes
    akosha's /metrics route.

    The families are matched on their ``# TYPE`` lines rather than on
    sample names: a counter with no labelled child yet still appears in
    the exposition, but with no sample line to match on.
    """
    blob = generate_latest(_SHARED_REGISTRY).decode()
    for family in (
        "akosha_otlp_heartbeat_query_failed_total",
        "akosha_otlp_heartbeat_service_absent_total",
        "akosha_otlp_heartbeat_p0_a_total",
        "akosha_otlp_heartbeat_empty_roster_total",
        "akosha_otlp_heartbeat_last_check_unix_nano",
    ):
        assert f"# TYPE {family} " in blob, f"{family} missing from the shared registry"


async def test_the_last_check_gauge_advances(watcher: HeartbeatWatcher) -> None:
    """A gauge that never moves is a gauge nobody is looking at.

    0 means the watcher has never run; anything else means the last cycle
    happened at that instant. It is the difference between "the watcher
    is not running" and "the watcher ran and found nothing".
    """
    sample_name = "akosha_otlp_heartbeat_last_check_unix_nano"
    before = _gauge_value(heartbeat_last_check_unix_nano, sample_name)

    await watcher.check(now_unix_nano=1_700_000_000_000_000_000)

    after = _gauge_value(heartbeat_last_check_unix_nano, sample_name)
    assert after == 1_700_000_000_000_000_000
    assert after != before


# ── the lifespan task ────────────────────────────────────────────────────


async def test_run_returns_promptly_when_stopped(query: SpanQuery) -> None:
    """A sweep task that ignores stop_event hangs the lifespan teardown."""
    watcher = HeartbeatWatcher(query, known_mcp_servers=["mahavishnu"])
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(watcher.run(stop), timeout=5)


async def test_run_checks_on_every_interval(query: SpanQuery) -> None:
    """The loop is what makes the watcher a watcher.

    Without it, the two-consecutive-absence rule can never be satisfied
    in production: the counter only advances when a cycle runs.
    """
    watcher = HeartbeatWatcher(query, known_mcp_servers=["mahavishnu"], window_seconds=1)
    stop = asyncio.Event()
    task = asyncio.create_task(watcher.run(stop, interval_seconds=0.05))
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    assert watcher.consecutive_absences["mahavishnu"] >= 1


# ── shape of the report ──────────────────────────────────────────────────


def test_the_window_and_threshold_match_the_spec() -> None:
    """§11: a 60s window, P0-A after two consecutive windows."""
    assert HeartbeatWatcher.DEFAULT_WINDOW_SECONDS == 60
    assert HeartbeatWatcher.DEFAULT_ABSENCE_THRESHOLD == 2


async def test_the_report_carries_the_window_it_used(query: SpanQuery) -> None:
    """The window is part of the answer, not a hidden constant.

    A report that says "your producer is quiet" without saying "quiet for
    how long" cannot be acted on.
    """
    watcher = HeartbeatWatcher(query, known_mcp_servers=["mahavishnu"], window_seconds=30)
    report = await watcher.check()
    assert report.window_seconds == 30
    assert isinstance(report, HeartbeatReport)


async def test_a_watcher_with_no_roster_at_all_still_reports(
    query: SpanQuery,
) -> None:
    """The default (no roster passed) is the empty-roster P0, not silence.

    A watcher constructed without a roster must not sit there quietly
    reporting nothing — that is the exact failure the empty-roster P0
    exists to catch.
    """
    watcher = HeartbeatWatcher(query)
    report = await watcher.check()
    assert report.p0_empty_roster is True
    assert report.is_p0 is True
