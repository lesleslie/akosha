"""Array, kvlist and bytes attributes must not kill an Export RPC.

This file pins a real production failure.

``AnyValue`` is a oneof over seven value shapes. A first cut at the
attribute unwrapper returned the raw protobuf for the three non-scalar
shapes::

    return getattr(value, which)   # array_value -> ArrayValue message

``json.dumps`` has no handler for a protobuf message, so rendering a span
with an array-valued attribute raised::

    TypeError: Object of type ArrayValue is not JSON serializable
    when serializing dict item 'ordered'

The exception escaped the gRPC servicer, gRPC answered
``StatusCode.UNKNOWN`` for the **whole Export RPC**, and the producer
dropped the entire batch — up to 512 spans per failed export from a
``BatchSpanProcessor``.

The trigger was ordinary, not exotic. oneiric's resolver emits a
``resolver.decision`` span whose ``ordered`` attribute is a list of dicts
(``oneiric/core/observability.py`` flattens ``DecisionEvent.details``
into the top-level attribute dict), and pydantic-ai emits
``gen_ai.response.finish_reasons`` as a list. Both are legal OTLP.

Two things are pinned here:

1. Every ``AnyValue`` shape renders to a JSON-serializable Python value.
2. A span that cannot be rendered is **quarantined, not fatal** — the rest
   of the batch still lands. The isolation is defence in depth: the first
   fix removes the known cause, and the isolation means the next unknown
   cause costs one span instead of one batch.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.resource.v1 import resource_pb2
from opentelemetry.proto.trace.v1 import trace_pb2

from akosha.observability import otlp_receiver as mod
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


def _sv(value: str) -> common_pb2.AnyValue:
    return common_pb2.AnyValue(string_value=value)


def _ordered_kv() -> common_pb2.KeyValue:
    """The exact shape oneiric's resolver emits.

    ``ResolutionExplanation.as_dict()`` returns ``{"domain", "key",
    "ordered": [{...}, {...}]}`` and ``DecisionEvent.as_attributes``
    flattens the whole dict into the span's attributes, so ``ordered``
    arrives on the wire as an array of kvlists.
    """
    return common_pb2.KeyValue(
        key="ordered",
        value=common_pb2.AnyValue(
            array_value=common_pb2.ArrayValue(
                values=[
                    common_pb2.AnyValue(
                        kvlist_value=common_pb2.KeyValueList(
                            values=[
                                common_pb2.KeyValue(key="domain", value=_sv("adapter")),
                                common_pb2.KeyValue(key="key", value=_sv("queue")),
                            ]
                        )
                    ),
                    common_pb2.AnyValue(
                        kvlist_value=common_pb2.KeyValueList(
                            values=[
                                common_pb2.KeyValue(key="domain", value=_sv("adapter")),
                            ]
                        )
                    ),
                ]
            )
        ),
    )


def _span(
    *,
    span_id: bytes = b"\x02" * 8,
    name: str = "unit-test-span",
    attrs: list[Any] | None = None,
) -> trace_pb2.Span:
    return trace_pb2.Span(
        trace_id=b"\x01" * 16,
        span_id=span_id,
        name=name,
        start_time_unix_nano=1_000_000_000,
        end_time_unix_nano=1_005_000_000,
        status=trace_pb2.Status(code=1),
        attributes=attrs or [],
    )


def _request(spans: list[trace_pb2.Span]) -> Any:
    """A real ``ExportTraceServiceRequest`` carrying ``spans``."""
    return trace_service_pb2.ExportTraceServiceRequest(
        resource_spans=[
            trace_pb2.ResourceSpans(
                resource=resource_pb2.Resource(
                    attributes=[
                        common_pb2.KeyValue(key="service.name", value=_sv("mahavishnu")),
                        common_pb2.KeyValue(key="service.namespace", value=_sv("bodai")),
                    ]
                ),
                scope_spans=[trace_pb2.ScopeSpans(spans=spans)],
            )
        ]
    )


def _stored(receiver: OTLPReceiver, sql: str) -> list[tuple[Any, ...]]:
    return receiver._hot_store.conn.execute(sql).fetchall()  # type: ignore[union-attr]


# ── the unwrapper ────────────────────────────────────────────────────────


def test_an_array_attribute_renders_as_a_json_list() -> None:
    """The oneiric resolver's ``ordered`` attribute must render."""
    row = mod._proto_to_dict(_span(attrs=[_ordered_kv()]), {})
    parsed = json.loads(row["attributes"])
    assert parsed["ordered"] == [
        {"domain": "adapter", "key": "queue"},
        {"domain": "adapter"},
    ]


def test_a_kvlist_attribute_renders_as_a_json_object() -> None:
    kv = common_pb2.KeyValue(
        key="details",
        value=common_pb2.AnyValue(
            kvlist_value=common_pb2.KeyValueList(
                values=[
                    common_pb2.KeyValue(key="a", value=_sv("x")),
                    common_pb2.KeyValue(key="b", value=common_pb2.AnyValue(int_value=7)),
                ]
            )
        ),
    )
    row = mod._proto_to_dict(_span(attrs=[kv]), {})
    assert json.loads(row["attributes"])["details"] == {"a": "x", "b": 7}


def test_a_bytes_attribute_renders_as_base64() -> None:
    """``bytes_value`` is Python ``bytes``, which ``json.dumps`` refuses."""
    kv = common_pb2.KeyValue(
        key="blob", value=common_pb2.AnyValue(bytes_value=b"\x00\xff")
    )
    row = mod._proto_to_dict(_span(attrs=[kv]), {})
    assert json.loads(row["attributes"])["blob"] == base64.b64encode(b"\x00\xff").decode()


def test_a_nested_array_renders_recursively() -> None:
    """An array of arrays of scalars must not leave a raw protobuf behind."""
    kv = common_pb2.KeyValue(
        key="matrix",
        value=common_pb2.AnyValue(
            array_value=common_pb2.ArrayValue(
                values=[
                    common_pb2.AnyValue(
                        array_value=common_pb2.ArrayValue(
                            values=[
                                common_pb2.AnyValue(int_value=1),
                                common_pb2.AnyValue(int_value=2),
                            ]
                        )
                    )
                ]
            )
        ),
    )
    row = mod._proto_to_dict(_span(attrs=[kv]), {})
    assert json.loads(row["attributes"])["matrix"] == [[1, 2]]


def test_a_resource_level_array_attribute_renders() -> None:
    """The same unwrap serves resource attributes.

    A resource-level array crashed identically to a span-level one, because
    both end up in a ``json.dumps`` call.
    """
    request = trace_service_pb2.ExportTraceServiceRequest(
        resource_spans=[
            trace_pb2.ResourceSpans(
                resource=resource_pb2.Resource(
                    attributes=[
                        common_pb2.KeyValue(key="service.name", value=_sv("mahavishnu")),
                        common_pb2.KeyValue(key="service.namespace", value=_sv("bodai")),
                        _ordered_kv(),
                    ]
                ),
                scope_spans=[trace_pb2.ScopeSpans(spans=[_span()])],
            )
        ]
    )
    rows = mod._extract_spans(request)
    assert json.loads(rows[0]["resource"])["ordered"] == [
        {"domain": "adapter", "key": "queue"},
        {"domain": "adapter"},
    ]


# ── isolation: one bad span must not take down the batch ────────────────


async def test_a_bad_span_does_not_take_down_the_batch(
    receiver: OTLPReceiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The good span must land even when another span cannot be rendered.

    The blast radius of the original bug was the whole batch: gRPC answered
    UNKNOWN, so the producer dropped every span it had queued. Here the
    first span's conversion is made to fail, and the second one must still
    reach the substrate.
    """
    real = mod._proto_to_dict

    def _boom(span: Any, resource_attrs: dict[str, Any]) -> dict[str, Any]:
        if getattr(span, "name", "") == "bad-span":
            raise TypeError("Object of type ArrayValue is not JSON serializable")
        return real(span, resource_attrs)

    monkeypatch.setattr(mod, "_proto_to_dict", _boom)

    request = _request(
        [
            _span(span_id=b"\x04" * 8, name="bad-span"),
            _span(span_id=b"\x03" * 8, name="good-span"),
        ]
    )
    result = await receiver._handle_batch(request)

    assert result["inserted"] == 1, "the good span must still land"
    assert result["rejected"] == 1, "the bad span must be quarantined"
    stored = _stored(receiver, "SELECT span_name FROM spans_substrate")
    assert [r[0] for r in stored] == ["good-span"]


async def test_an_unrenderable_span_is_reported_as_unrenderable(
    receiver: OTLPReceiver,
) -> None:
    """The DLQ reason must name the real failure.

    The stand-in row carries placeholder values for the required fields, so
    without the dedicated check the reason would be a misleading
    ``not_null`` on a field the span may well have had.
    """
    row = mod._unrenderable_row(
        _span(), TypeError("Object of type ArrayValue is not JSON serializable")
    )
    error = receiver.pre_validate(row)
    assert error is not None
    assert error.reason == "unrenderable"
    assert error.field == "span"
    assert "ArrayValue" in str(error.value)


async def test_a_batch_of_array_attributes_lands_whole(
    receiver: OTLPReceiver,
) -> None:
    """The end-to-end shape of the original failure.

    One span, one array attribute. Before the fix this raised a TypeError
    out of the servicer and the RPC failed; now the span lands with its
    attribute intact.
    """
    request = _request([_span(attrs=[_ordered_kv()])])
    result = await receiver._handle_batch(request)

    assert result["inserted"] == 1
    assert result["rejected"] == 0
    stored = _stored(receiver, "SELECT attributes FROM spans_substrate")
    assert json.loads(stored[0][0])["ordered"][0]["domain"] == "adapter"
