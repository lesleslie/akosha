"""The synthetic canary: the receiver proving it can still write.

§11.3. The heartbeat watcher proves the substrate is queryable. The canary
proves the receiver WRITES — which is the failure mode that lets /health
stay green while every producer's spans silently drop.

Two things are pinned here that are easy to get subtly wrong:

1. **The canary goes through the full production path.** A real
   ``ExportTraceServiceRequest`` is built and handed to ``_handle_batch``,
   so the marker crosses protobuf decode, the span→row conversion, the
   validator and the INSERT. A canary that shortcut straight to the
   INSERT would have stayed green through the ``ArrayValue`` serializer
   outage — which is the exact bug class it exists to catch.

2. **Success, rejection and failure are three different outcomes.** A
   multi-pod deployment racing the same cycle dedups on the
   ``(trace_id, span_id)`` UNIQUE constraint. That is the write path
   WORKING (another pod got there first), so it is a rejection — not a
   success, and not a failure. Collapsing the three is how a canary
   reports a healthy fleet as broken, or a broken fleet as healthy.
"""

from __future__ import annotations

import time

import pytest

from akosha.observability import otlp_metrics
from akosha.observability.canary_watcher import (
    CANARY_EMIT_INTERVAL_SECONDS,
    CANARY_NAMESPACE,
    CANARY_SERVICE_NAME,
    CANARY_SPAN_NAME,
    CANARY_TRACE_ID,
    CanaryEmitter,
    build_canary_request,
    canary_span_id,
)
from akosha.observability.otlp_receiver import OTLPReceiver
from akosha.storage.hot_store import HotStore


@pytest.fixture
async def store() -> HotStore:
    hs = HotStore(database_path=":memory:", embedding_dim=384)
    await hs.initialize()
    return hs


@pytest.fixture
def receiver(store: HotStore) -> OTLPReceiver:
    return OTLPReceiver(store, grpc_port=0, http_port=0)


def _rows(receiver: OTLPReceiver) -> list[tuple[str, str]]:
    return receiver._hot_store.conn.execute(  # type: ignore[union-attr]
        "SELECT service_name, span_name FROM spans_substrate"
    ).fetchall()


# ── the marker's identity ──────────────────────────────────────────────


def test_the_canary_request_carries_the_spec_identity() -> None:
    """service_name, namespace, span_name and the two marker attributes."""
    request = build_canary_request(1_700_000_000_000_000_000)

    resource_attrs = {
        a.key: a.value.string_value for a in request.resource_spans[0].resource.attributes
    }
    assert resource_attrs["service.name"] == CANARY_SERVICE_NAME
    assert resource_attrs["service.namespace"] == CANARY_NAMESPACE

    span = request.resource_spans[0].scope_spans[0].spans[0]
    assert span.name == CANARY_SPAN_NAME
    assert span.trace_id == CANARY_TRACE_ID

    attrs = {a.key: a.value for a in span.attributes}
    assert attrs["_canary"].bool_value is True
    assert attrs["_canary_version"].string_value == "1"


def test_the_canary_namespace_is_one_the_fleet_actually_uses() -> None:
    """The canary must not be rejected by the receiver's own validator.

    ``bodai`` is the fleet default. A canary on a namespace that is NOT
    on the allowlist would fail before it ever reached the INSERT, and a
    canary that fails on a namespace the fleet legitimately uses is a
    false alarm, not a signal.
    """
    from akosha.observability.resource_validator import ALLOWED_NAMESPACES

    assert CANARY_NAMESPACE in ALLOWED_NAMESPACES


def test_the_span_id_is_derived_from_the_timestamp() -> None:
    """Derived, not random — so the multi-pod dedup path is reproducible."""
    assert canary_span_id(1_700_000_000_000_000_000) == canary_span_id(
        1_700_000_000_000_000_000
    )
    assert canary_span_id(1_700_000_000_000_000_000) != canary_span_id(
        1_700_000_000_000_000_001
    )
    assert len(canary_span_id(1)) == 8


# ── the write path ──────────────────────────────────────────────────────


async def test_a_canary_write_lands_in_the_substrate(
    receiver: OTLPReceiver,
) -> None:
    """The whole point: the marker actually reaches the table."""
    emitter = CanaryEmitter(receiver)

    outcome = await emitter.emit_once()

    assert outcome == "succeeded"
    assert _rows(receiver) == [(CANARY_SERVICE_NAME, CANARY_SPAN_NAME)]
    assert emitter.state.canary_writes_succeeded_total == 1
    assert emitter.state.last_canary_success_unix_nano is not None


async def test_a_successful_write_advances_the_gauge(receiver: OTLPReceiver) -> None:
    """The P0-C alert measures staleness against this gauge."""
    emitter = CanaryEmitter(receiver)
    before = otlp_metrics.canary_last_success_unix_nano._value.get()

    await emitter.emit_once()

    after = otlp_metrics.canary_last_success_unix_nano._value.get()
    assert after > before


async def test_the_canary_goes_through_the_conversion_path(
    receiver: OTLPReceiver,
) -> None:
    """It must not shortcut into the INSERT.

    The serializer is where the ``ArrayValue`` defect lived. A canary
    that skipped it would have stayed green through that outage.
    """
    import akosha.observability.otlp_receiver as mod

    converted: list[str] = []
    real = mod._proto_to_dict

    def _spy(span: object, resource_attrs: dict[str, object]) -> dict[str, object]:
        converted.append(getattr(span, "name", ""))
        return real(span, resource_attrs)  # type: ignore[arg-type]

    mod._proto_to_dict = _spy  # type: ignore[assignment]
    try:
        await CanaryEmitter(receiver).emit_once()
    finally:
        mod._proto_to_dict = real  # type: ignore[assignment]

    assert converted == [CANARY_SPAN_NAME], (
        "the canary must be converted exactly like a producer's span"
    )


# ── the three outcomes stay distinct ────────────────────────────────────


async def test_a_deduplicated_write_is_a_rejection_not_a_success(
    receiver: OTLPReceiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A multi-pod race is the write path WORKING.

    Two pods emitting the same cycle collide on the UNIQUE constraint.
    The loser must not be counted as a success (it inserted nothing) and
    must not be counted as a failure (nothing broke).
    """
    import akosha.observability.canary_watcher as cw

    fixed = 1_700_000_000_000_000_000
    monkeypatch.setattr(cw.time, "time_ns", lambda: fixed)
    emitter = CanaryEmitter(receiver)

    first = await emitter.emit_once()
    second = await emitter.emit_once()

    assert first == "succeeded"
    assert second == "rejected"
    assert emitter.state.canary_writes_succeeded_total == 1
    assert emitter.state.canary_writes_rejected_total == 1
    assert emitter.state.canary_writes_failed_total == 0
    # And only ONE row exists — the constraint did its job.
    assert len(_rows(receiver)) == 1


async def test_a_quarantined_write_is_a_failure(receiver: OTLPReceiver) -> None:
    """A row the validator rejects is a failed write, not a rejection.

    A canary that quarantines its own marker is telling you the write
    path is broken, which is exactly the signal it exists to raise.
    """
    import akosha.observability.otlp_receiver as mod

    real = mod._proto_to_dict

    def _break_it(span: object, resource_attrs: dict[str, object]) -> dict[str, object]:
        row = real(span, resource_attrs)  # type: ignore[arg-type]
        row["service_namespace"] = "not-a-real-namespace"
        return row

    mod._proto_to_dict = _break_it  # type: ignore[assignment]
    try:
        outcome = await CanaryEmitter(receiver).emit_once()
    finally:
        mod._proto_to_dict = real  # type: ignore[assignment]

    assert outcome == "failed"
    assert _rows(receiver) == []


# ── the task must survive a failure ─────────────────────────────────────


async def test_a_failing_emit_does_not_kill_the_task(
    receiver: OTLPReceiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task that died on the first transient error stops testing anything.

    The staleness of ``canary_last_success_unix_nano`` is what the P0-C
    alert reads, so the task has to keep running — otherwise a dead
    emitter and a broken write path look identical.
    """
    import asyncio

    import akosha.observability.canary_watcher as cw

    calls = {"n": 0}

    async def _boom(self: object) -> str:
        calls["n"] += 1
        raise RuntimeError("transient")

    monkeypatch.setattr(cw.CanaryEmitter, "emit_once", _boom)
    # The receiver clamps the interval to a minimum of one second, so the
    # fastest two emits need a little over two seconds.
    monkeypatch.setattr(cw, "CANARY_EMIT_INTERVAL_SECONDS", 1)

    stop = asyncio.Event()
    task = asyncio.create_task(receiver.run_canary_emitter(stop))
    await asyncio.sleep(2.5)
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    assert calls["n"] >= 2, "the emitter must have retried, not died"


# ── retention: the canary cannot be allowed to fill the table ──────────


def test_canary_rows_are_swept_at_24h(receiver: OTLPReceiver) -> None:
    """A canary row is liveness evidence, not history.

    At one row per 30 seconds that is 86,400 rows a day, and the
    substrate is an IN-MEMORY DuckDB. On the substrate's 30-day horizon
    the canary alone would fill it with millions of rows inside a month.
    """
    from akosha.observability.canary_watcher import CANARY_SERVICE_NAME

    now_ns = time.time_ns()
    old_ns = now_ns - 25 * 3600 * 1_000_000_000
    recent_ns = now_ns - 3600 * 1_000_000_000

    receiver._hot_store.conn.execute(  # type: ignore[union-attr]
        "INSERT INTO spans_substrate "
        "(span_id, trace_id, service_name, service_namespace, span_name, "
        " start_time_unix_nano, received_at_unix_nano, attributes, resource) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, '{}', '{}')",
        [
            "a" * 16,
            "b" * 32,
            CANARY_SERVICE_NAME,
            CANARY_NAMESPACE,
            CANARY_SPAN_NAME,
            old_ns,
            old_ns,
        ],
    )
    receiver._hot_store.conn.execute(  # type: ignore[union-attr]
        "INSERT INTO spans_substrate "
        "(span_id, trace_id, service_name, service_namespace, span_name, "
        " start_time_unix_nano, received_at_unix_nano, attributes, resource) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, '{}', '{}')",
        [
            "c" * 16,
            "d" * 32,
            CANARY_SERVICE_NAME,
            CANARY_NAMESPACE,
            CANARY_SPAN_NAME,
            recent_ns,
            recent_ns,
        ],
    )

    deleted = receiver._sweep_table(
        receiver._hot_store.spans_substrate_table,
        "WHERE service_name = ? AND received_at_unix_nano < ?",
        [CANARY_SERVICE_NAME, now_ns - 24 * 3600 * 1_000_000_000],
        label="spans_substrate_canary",
    )

    assert deleted == 1, "the 25h-old canary row must go, the 1h-old one must stay"
    remaining = receiver._hot_store.conn.execute(  # type: ignore[union-attr]
        "SELECT COUNT(*) FROM spans_substrate"
    ).fetchone()
    assert remaining[0] == 1


def test_the_canary_sweep_failure_is_labelled_separately(
    receiver: OTLPReceiver,
) -> None:
    """Two sweeps on one table must be distinguishable in an alert."""
    receiver._sweep_table(
        "table_that_does_not_exist",
        "WHERE 1=1",
        label="spans_substrate_canary",
    )
    samples = [
        s
        for metric in otlp_metrics.retention_sweep_failed_total.collect()
        for s in metric.samples
    ]
    tables = {s.labels.get("table") for s in samples}
    assert "spans_substrate_canary" in tables


def test_the_emit_interval_is_thirty_seconds() -> None:
    """Pinned: 3 missed emits is the 90s P0-C window."""
    assert CANARY_EMIT_INTERVAL_SECONDS == 30
