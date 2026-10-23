"""akosha's ``HotStore`` inherits the substrate's ingest writers — PR-A3.

The count semantics (dedup accounting, composite-vs-PK conflict resolution)
are DuckDB behaviour against substrate-owned DDL, so they are tested in
oneiric alongside that DDL. What belongs here is the thing only akosha can
assert: that the receiver reaches those writers through the subclass it
borrows from the lifespan.

That inheritance is load-bearing rather than incidental. The receiver
receives a ``HotStore`` from the lifespan, so if these methods ever stop
being inherited — moved back into the subclass, or the subclass stops
subclassing — the receiver fails with an AttributeError on the first span
that arrives in production, not in a test.
"""

from __future__ import annotations

import pytest

from akosha.observability.otlp_receiver import OTLPReceiver
from akosha.storage.hot_store import HotStore


@pytest.fixture
async def store() -> HotStore:
    hs = HotStore(database_path=":memory:", embedding_dim=384)
    await hs.initialize()
    return hs


def test_the_writers_are_inherited_not_redeclared() -> None:
    """They live on the substrate class, per ADR 017.

    akosha's module is capped at 500 LOC by
    ``tests/unit/test_hot_store_subclass.py`` precisely so it stays a thin
    subclass, and the tables these methods write are substrate-owned.
    """
    from oneiric.adapters.vector.duckdb_hot_store import DuckdbHotStore

    assert HotStore.insert_spans_substrate is DuckdbHotStore.insert_spans_substrate
    assert HotStore.insert_dlq_rows is DuckdbHotStore.insert_dlq_rows
    assert "insert_spans_substrate" not in vars(HotStore)


async def test_the_receiver_reaches_the_substrate_writer(
    store: HotStore,
) -> None:
    """End to end: a real protobuf batch lands in the real substrate.

    Exercises the exact call chain the lifespan produces — receiver holds a
    ``HotStore``, calls ``insert_spans_substrate`` on it, and the row reaches
    the table the read path queries.
    """
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
    from opentelemetry.proto.common.v1 import common_pb2
    from opentelemetry.proto.resource.v1 import resource_pb2
    from opentelemetry.proto.trace.v1 import trace_pb2

    request = trace_service_pb2.ExportTraceServiceRequest(
        resource_spans=[
            trace_pb2.ResourceSpans(
                resource=resource_pb2.Resource(
                    attributes=[
                        common_pb2.KeyValue(
                            key="service.name",
                            value=common_pb2.AnyValue(string_value="akosha"),
                        ),
                        common_pb2.KeyValue(
                            key="service.namespace",
                            value=common_pb2.AnyValue(string_value="bodai"),
                        ),
                    ]
                ),
                scope_spans=[
                    trace_pb2.ScopeSpans(
                        spans=[
                            trace_pb2.Span(
                                trace_id=b"\x01" * 16,
                                span_id=b"\x02" * 8,
                                name="integration-span",
                                start_time_unix_nano=1_000,
                                end_time_unix_nano=2_000,
                            )
                        ]
                    )
                ],
            )
        ]
    )

    receiver = OTLPReceiver(store, grpc_port=0, http_port=0)
    result = await receiver._handle_batch(request)

    assert result == {"inserted": 1, "deduped": 0, "rejected": 0}
    rows = store.conn.execute(  # type: ignore[union-attr]
        "SELECT span_id, service_name FROM spans_substrate"
    ).fetchall()
    assert rows == [("02" * 8, "akosha")]


async def test_a_rejected_span_reaches_the_substrate_dlq(
    store: HotStore,
) -> None:
    """The quarantine half of the same chain, through inheritance too."""
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
    from opentelemetry.proto.trace.v1 import trace_pb2

    # No resource attributes at all -> no service_name -> rejected.
    request = trace_service_pb2.ExportTraceServiceRequest(
        resource_spans=[
            trace_pb2.ResourceSpans(
                scope_spans=[
                    trace_pb2.ScopeSpans(
                        spans=[
                            trace_pb2.Span(
                                trace_id=b"\x03" * 16,
                                span_id=b"\x04" * 8,
                                name="nameless",
                                start_time_unix_nano=1,
                                end_time_unix_nano=2,
                            )
                        ]
                    )
                ]
            )
        ]
    )

    receiver = OTLPReceiver(store, grpc_port=0, http_port=0)
    result = await receiver._handle_batch(request)

    assert result["rejected"] == 1
    quarantined = store.conn.execute(  # type: ignore[union-attr]
        "SELECT reason, field, span_id FROM _dlq_rows"
    ).fetchall()
    assert quarantined == [("not_null", "service_name", "04" * 8)]