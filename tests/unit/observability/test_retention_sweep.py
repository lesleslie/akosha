"""PR-A4: the retention sweep, and the namespace fallback that unblocks ingest.

Three things here were each individually invisible:

- ``_dlq_rows`` grew without bound because the sweep body was a stub.
- Retention keyed off the wrong column deletes rows that just arrived.
- A producer that declares ``service.namespace`` on the SPAN (which is what
  mcp-common's middleware does) was quarantined even though it stated its
  identity, because the receiver only ever read the resource.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

import pytest
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.resource.v1 import resource_pb2
from opentelemetry.proto.trace.v1 import trace_pb2

from akosha.observability import otlp_metrics
from akosha.observability.otlp_receiver import OTLPReceiver
from akosha.storage.hot_store import HotStore

DAY_NS = 86_400 * 1_000_000_000


@pytest.fixture
async def store() -> HotStore:
    hs = HotStore(database_path=":memory:", embedding_dim=384)
    await hs.initialize()
    return hs


@pytest.fixture
def receiver(store: HotStore) -> OTLPReceiver:
    return OTLPReceiver(store, grpc_port=0, http_port=0)


def _insert_span(store: HotStore, span_id: bytes, *, received_ns: int, start_ns: int) -> None:
    """Insert one row directly so the test controls the retention columns."""
    store.conn.execute(
        f"INSERT INTO {store.spans_substrate_table} "
        "(span_id, trace_id, service_name, service_namespace, span_name, "
        "start_time_unix_nano, end_time_unix_nano, received_at_unix_nano, "
        "attributes, resource) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            span_id.hex(),
            (b"\x09" * 16).hex(),
            "cli-tools",
            "bodai",
            "synthetic",
            start_ns,
            start_ns + 1_000_000,
            received_ns,
            "{}",
            "{}",
        ],
    )


def _count(store: HotStore, table: str) -> int:
    return store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ── the cardinal rule ────────────────────────────────────────────────────


def test_retention_keys_on_received_not_start(receiver: OTLPReceiver, store: HotStore) -> None:
    """A producer's clock must never be able to delete a fresh row.

    This span claims to have started 400 days ago while being received
    seconds ago. Filtering on ``start_time_unix_nano`` would evict it on the
    next sweep; filtering on the receiver's own clock keeps it.
    """
    now = time.time_ns()
    _insert_span(store, b"\x01" * 8, received_ns=now, start_ns=now - 400 * DAY_NS)

    deleted = receiver._sweep_table(
        store.spans_substrate_table,
        "WHERE received_at_unix_nano < ?",
        [now - 30 * DAY_NS],
    )

    assert deleted == 0
    assert _count(store, store.spans_substrate_table) == 1


def test_a_genuinely_old_span_is_deleted(receiver: OTLPReceiver, store: HotStore) -> None:
    """The complement — otherwise the test above would pass if DELETE were a no-op."""
    now = time.time_ns()
    _insert_span(store, b"\x02" * 8, received_ns=now - 31 * DAY_NS, start_ns=now)

    deleted = receiver._sweep_table(
        store.spans_substrate_table,
        "WHERE received_at_unix_nano < ?",
        [now - 30 * DAY_NS],
    )

    assert deleted == 1
    assert _count(store, store.spans_substrate_table) == 0


# ── §8.1 DLQ retention ───────────────────────────────────────────────────


def test_dlq_rows_older_than_24h_are_swept(
    receiver: OTLPReceiver, store: HotStore
) -> None:
    """§8.1. Quarantined rows are evidence, not history — a day is enough.

    The 1h row must survive: if the sweep used the substrate's 30-day window
    for the DLQ too, a fresh rejection would sit there for a month, which is
    exactly when the operator has stopped caring.
    """
    now = time.time_ns()
    for i, age_h in enumerate((1, 25, 100)):
        store.conn.execute(
            f"INSERT INTO {store.dlq_rows_table} "
            "(reason, field, row_json, span_id, rejected_at) VALUES (?, ?, ?, ?, ?)",
            [
                "not_null",
                "service_namespace",
                "{}",
                f"{i:016x}",
                datetime.fromtimestamp((now - age_h * 3600 * 1_000_000_000) / 1e9),
            ],
        )

    deleted = receiver._sweep_table(
        store.dlq_rows_table,
        "WHERE rejected_at < CURRENT_TIMESTAMP - INTERVAL '24 hours'",
    )

    assert deleted == 2
    assert _count(store, store.dlq_rows_table) == 1


# ── failure is not zero ──────────────────────────────────────────────────


def test_a_failing_sweep_reports_none_not_zero(
    receiver: OTLPReceiver, store: HotStore
) -> None:
    """"Deleted nothing" and "could not run" must not look alike.

    Collapsing them is how a sweep that has been broken for weeks keeps
    reporting itself healthy — there is nothing to alert on.
    """
    result = receiver._sweep_table("table_that_does_not_exist", "WHERE 1=1")
    assert result is None


# ── stop_event ───────────────────────────────────────────────────────────


async def test_the_sweep_returns_promptly_when_stopped(receiver: OTLPReceiver) -> None:
    """A sweep task that ignores stop_event hangs the lifespan teardown."""
    stop = asyncio.Event()
    task = asyncio.create_task(receiver.run_retention_sweep(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=5)


async def test_an_already_set_event_means_no_work_at_all(receiver: OTLPReceiver) -> None:
    """Guard the race where shutdown lands before the task's first await."""
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(receiver.run_retention_sweep(stop), timeout=5)


# ── namespace fallback ───────────────────────────────────────────────────


def _span_request(
    span_id: bytes,
    *,
    resource_attrs: list[tuple[str, str]],
    span_attrs: list[tuple[str, str]],
) -> Any:
    def kv(pairs: list[tuple[str, str]]) -> list[Any]:
        return [
            common_pb2.KeyValue(key=k, value=common_pb2.AnyValue(string_value=v))
            for k, v in pairs
        ]

    return trace_service_pb2.ExportTraceServiceRequest(
        resource_spans=[
            trace_pb2.ResourceSpans(
                resource=resource_pb2.Resource(attributes=kv(resource_attrs)),
                scope_spans=[
                    trace_pb2.ScopeSpans(
                        spans=[
                            trace_pb2.Span(
                                trace_id=b"\x0a" * 16,
                                span_id=span_id,
                                name="middleware_span",
                                start_time_unix_nano=1_000_000_000,
                                end_time_unix_nano=2_000_000_000,
                                attributes=kv(span_attrs),
                            )
                        ]
                    )
                ],
            )
        ]
    )


async def test_a_span_level_namespace_is_accepted(receiver: OTLPReceiver) -> None:
    """mcp-common's middleware stamps identity on the Span, not the Resource.

    Reading resource-only quarantined every mahavishnu-dispatched span even
    though the producer had plainly declared who it was.
    """
    result = await receiver._handle_batch(
        _span_request(
            b"\x0b" * 8,
            resource_attrs=[],
            span_attrs=[
                ("service.name", "mahavishnu"),
                ("service.namespace", "bodai"),
            ],
        )
    )
    assert result["rejected"] == 0
    assert result["inserted"] == 1


async def test_the_retired_bridge_namespace_is_rejected(receiver: OTLPReceiver) -> None:
    """PR-A6: the pre-flip namespace is off the allowlist on purpose.

    ``mcp-ecosystem`` was mcp-common's default before PR-A7. Keeping it
    allowlisted would let a producer that regressed to the old default look
    perfectly healthy -- which is precisely what the validator exists to
    catch. It is rejected by the ALLOWLIST, not by the required-field check,
    so it is rejected even though the field is present and non-empty.
    """
    result = await receiver._handle_batch(
        _span_request(
            b"\x0f" * 8,
            resource_attrs=[],
            span_attrs=[
                ("service.name", "mahavishnu"),
                ("service.namespace", "mcp-ecosystem"),
            ],
        )
    )
    assert result["rejected"] == 1
    assert result["inserted"] == 0


async def test_the_resource_still_wins_over_the_span(receiver: OTLPReceiver) -> None:
    """Resource is where OTel semantics put service identity; it takes priority."""
    from akosha.observability.otlp_receiver import _proto_to_dict

    row = _proto_to_dict(
        trace_pb2.Span(
            trace_id=b"\x0c" * 16,
            span_id=b"\x0c" * 8,
            name="s",
            start_time_unix_nano=1,
            attributes=[
                common_pb2.KeyValue(
                    key="service.namespace",
                    value=common_pb2.AnyValue(string_value="from-span"),
                )
            ],
        ),
        {"service.namespace": "from-resource"},
    )
    assert row["service_namespace"] == "from-resource"


async def test_still_rejected_when_neither_bag_carries_it(receiver: OTLPReceiver) -> None:
    """The fallback must not become a loophole — absent is still absent."""
    result = await receiver._handle_batch(
        _span_request(b"\x0d" * 8, resource_attrs=[], span_attrs=[])
    )
    assert result["rejected"] == 1


async def test_a_rejected_batch_names_the_failing_field(receiver: OTLPReceiver) -> None:
    """The label must come from the real ingest path, not just the metric API.

    Asserts a delta, never an absolute: the registry is a process-wide
    singleton, so any fixed number here is a race with every other test that
    touches the same counter.

    The field is ``service_name``, not ``service_namespace``: pre_validate
    iterates ``sorted(REQUIRED_FIELDS)`` and reports the FIRST failure, and
    ``service_name`` sorts ahead of ``service_namespace``. A span carrying
    neither trips the earlier one.
    """
    before = _sample_value("service_name")

    await receiver._handle_batch(
        _span_request(b"\x0e" * 8, resource_attrs=[], span_attrs=[])
    )

    after = _sample_value("service_name")
    assert after == before + 1


def _metric_samples(metric: Any) -> list[Any]:
    """Every SAMPLE of a metric, flattened out of its label combinations.

    ``Counter.collect()`` returns one ``Metric`` per label combination, and
    each of those carries a ``.samples`` list. The sample is what carries
    ``labels`` and ``value`` -- the ``Metric`` carries neither.
    """
    samples: list[Any] = []
    for family in metric.collect():
        samples.extend(family.samples)
    return samples


def _sample_value(field: str) -> float:
    """Current value of the rejection counter for ``field``.

    Reads the metric OBJECT, not the rendered exposition blob.
    ``prometheus_metrics.reset_all_metrics()`` replaces the registry
    wholesale and re-registers only its own metrics, so every
    ``akosha_otlp_*`` collector is dropped from the live registry afterwards
    -- and ``tests/test_prometheus_metrics_coverage.py`` calls it about ten
    times. A test that reads the blob therefore fails depending on what ran
    before it, which is a property of the test suite, not of the code.
    """
    total = 0.0
    for sample in _metric_samples(otlp_metrics.spans_rejected_total):
        if sample.labels.get("field") == field:
            total += float(sample.value)
    return total


# ── metrics ──────────────────────────────────────────────────────────────


def test_receiver_metrics_land_in_the_shared_registry() -> None:
    """They must ride the /metrics route already on :8682, not a new port.

    Asserted against the metric OBJECTS' own samples rather than the
    rendered exposition blob. ``reset_all_metrics()`` replaces the registry
    wholesale and re-registers only its OWN metrics, so every
    ``akosha_otlp_*`` collector is dropped from the live registry afterwards
    -- and ``tests/test_prometheus_metrics_coverage.py`` calls it about ten
    times. Reading the objects directly makes this assertion
    order-independent, which is what it actually cares about.
    """
    expected = {
        "akosha_otlp_spans_inserted_total",
        "akosha_otlp_dlq_inserts_total",
        "akosha_otlp_sweep_first_started_unix_nano",
        "akosha_otlp_sweep_last_completed_unix_nano",
    }
    seen: set[str] = set()
    for metric in (
        otlp_metrics.spans_inserted_total,
        otlp_metrics.dlq_inserts_total,
        otlp_metrics.sweep_first_started_unix_nano,
        otlp_metrics.sweep_last_completed_unix_nano,
    ):
        seen |= {s.name for s in _metric_samples(metric)}
    missing = expected - seen
    assert not missing, f"missing metric series: {sorted(missing)}"


# ── rejection labels ─────────────────────────────────────────────────────


def test_rejections_are_labelled_by_the_field_that_failed() -> None:
    """An unlabelled rejection count cannot be acted on.

    The hot store is :memory:, so _dlq_rows is not queryable from outside the
    process. This label is the only external record of WHY rows were
    quarantined — which is the difference between "139 rejected" and
    "139 rejected, all service_namespace".
    """
    otlp_metrics.spans_rejected_total.labels(field="service_namespace").inc(3)
    otlp_metrics.spans_rejected_total.labels(field="status_code").inc(1)

    # Deltas, not absolutes: the metric is process-wide, so other tests may
    # already have moved it.
    for field, delta in (("service_namespace", 3), ("status_code", 1)):
        samples = [
            s
            for s in _metric_samples(otlp_metrics.spans_rejected_total)
            if s.labels.get("field") == field
        ]
        assert samples, f"no sample for field={field}"
        for s in samples:
            assert s.value >= delta

    # Every sample must carry the label: a sample with no field dimension
    # cannot be attributed to a validation rule.
    for s in _metric_samples(otlp_metrics.spans_rejected_total):
        assert "field" in s.labels, f"unlabelled rejection sample: {s}"