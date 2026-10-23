"""``OTLPReceiver`` — validation, ingest, DLQ, and the fail-loud boot.

Driven with real protobuf messages, not doubles. The mapping from
``ResourceSpans`` to a substrate row is where the two silent-wrongness bugs
this file pins actually live, and a hand-rolled fake would happily carry the
wrong field names through a test that passes.

The one thing deliberately NOT covered here is the port binding itself: that
needs a live listener and lands in the e2e module, which is marked ``slow``.
"""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.trace.v1 import trace_pb2
from opentelemetry.proto.resource.v1 import resource_pb2

from akosha.observability.otlp_receiver import OTLPReceiver, RowValidationError
from akosha.storage.hot_store import HotStore


@pytest.fixture
async def store() -> HotStore:
    hs = HotStore(database_path=":memory:", embedding_dim=384)
    await hs.initialize()
    return hs


@pytest.fixture
def receiver(store: HotStore) -> OTLPReceiver:
    return OTLPReceiver(store, grpc_port=0, http_port=0)


def _request(
    *,
    trace_id: bytes = b"\x01" * 16,
    span_id: bytes = b"\x02" * 8,
    service_name: str | None = "mahavishnu",
    service_namespace: str | None = "bodai",
    span_name: str = "unit-test-span",
    start: int = 1_000_000_000,
    status_code: int = 1,
    span_attrs: list[Any] | None = None,
) -> Any:
    """Build a real ``ExportTraceServiceRequest``."""
    resource_attrs = []
    if service_name is not None:
        resource_attrs.append(
            common_pb2.KeyValue(key="service.name", value=_sv(service_name))
        )
    if service_namespace is not None:
        resource_attrs.append(
            common_pb2.KeyValue(key="service.namespace", value=_sv(service_namespace))
        )
    return trace_service_pb2.ExportTraceServiceRequest(
        resource_spans=[
            trace_pb2.ResourceSpans(
                resource=resource_pb2.Resource(attributes=resource_attrs),
                scope_spans=[
                    trace_pb2.ScopeSpans(
                        spans=[
                            trace_pb2.Span(
                                trace_id=trace_id,
                                span_id=span_id,
                                name=span_name,
                                start_time_unix_nano=start,
                                end_time_unix_nano=start + 5_000_000,
                                status=trace_pb2.Status(code=status_code),
                                attributes=span_attrs or [],
                            )
                        ]
                    )
                ],
            )
        ]
    )


def _sv(value: str) -> common_pb2.AnyValue:
    return common_pb2.AnyValue(string_value=value)


def _stored(receiver: OTLPReceiver, sql: str) -> list[tuple[Any, ...]]:
    return receiver._hot_store.conn.execute(sql).fetchall()  # type: ignore[union-attr]


# ── pre_validate ─────────────────────────────────────────────────────────


def test_a_complete_row_passes(receiver: OTLPReceiver) -> None:
    assert receiver.pre_validate({"service_name": "m", "service_namespace": "b",
                                  "span_name": "s", "start_time_unix_nano": 1}) is None


@pytest.mark.parametrize("missing", ["service_name", "service_namespace",
                                     "span_name", "start_time_unix_nano"])
def test_each_required_field_is_required(receiver: OTLPReceiver, missing: str) -> None:
    row = {"service_name": "m", "service_namespace": "b",
           "span_name": "s", "start_time_unix_nano": 1}
    del row[missing]
    error = receiver.pre_validate(row)
    assert error is not None
    assert error.field == missing
    assert error.reason == "not_null"


def test_empty_string_counts_as_absent(receiver: OTLPReceiver) -> None:
    """``service_name=""`` is not a service.

    proto3 cannot distinguish an unset string from an empty one — both arrive
    as ``""`` — so treating empty as present would let every producer that
    omits the resource attribute write a span attributed to a nameless service,
    which is exactly what the "unknown" default in the proto mapping exists to
    prevent.
    """
    error = receiver.pre_validate({"service_name": "", "service_namespace": "b",
                                   "span_name": "s", "start_time_unix_nano": 1})
    assert error is not None
    assert error.field == "service_name"


def test_the_reported_field_is_deterministic(receiver: OTLPReceiver) -> None:
    """A row missing several required fields must name the same one every run.

    ``REQUIRED_FIELDS`` is a frozenset, and frozenset iteration order varies
    with the hash seed. A DLQ row whose ``field`` differs between two runs of
    the same batch cannot be diagnosed from its own evidence — and these are
    the rows that exist precisely because something went wrong.
    """
    empty: dict[str, Any] = {}
    reported = {receiver.pre_validate(empty).field for _ in range(200)}  # type: ignore[union-attr]
    assert reported == {"service_name"}


def test_an_unknown_status_code_is_rejected(receiver: OTLPReceiver) -> None:
    error = receiver.pre_validate({"service_name": "m", "service_namespace": "b",
                                   "span_name": "s", "start_time_unix_nano": 1,
                                   "status_code": "STATUS_CODE_WEIRD"})
    assert error is not None
    assert (error.field, error.reason) == ("status_code", "enum")


def test_all_three_status_codes_are_accepted(receiver: OTLPReceiver) -> None:
    for code in ("STATUS_CODE_UNSET", "STATUS_CODE_OK", "STATUS_CODE_ERROR"):
        row = {"service_name": "m", "service_namespace": "b", "span_name": "s",
               "start_time_unix_nano": 1, "status_code": code}
        assert receiver.pre_validate(row) is None


def test_a_null_status_code_is_allowed(receiver: OTLPReceiver) -> None:
    """Absent is not the same as invalid.

    ``status_code`` is nullable in the substrate, and a producer that sends no
    status at all should not have every one of its spans quarantined.
    """
    row = {"service_name": "m", "service_namespace": "b",
           "span_name": "s", "start_time_unix_nano": 1}
    assert receiver.pre_validate(row) is None


def test_unserializable_attributes_are_rejected(receiver: OTLPReceiver) -> None:
    error = receiver.pre_validate({"service_name": "m", "service_namespace": "b",
                                   "span_name": "s", "start_time_unix_nano": 1,
                                   "attributes": {"bad": object()}})
    assert error is not None
    assert (error.field, error.reason) == ("attributes", "json")


# ── _handle_batch ingest ─────────────────────────────────────────────────


async def test_a_valid_span_lands_in_the_substrate(receiver: OTLPReceiver) -> None:
    result = await receiver._handle_batch(_request())
    assert result == {"inserted": 1, "deduped": 0, "rejected": 0}
    rows = _stored(receiver, "SELECT span_id, service_name, span_name FROM spans_substrate")
    assert len(rows) == 1
    assert rows[0][1] == "mahavishnu"


async def test_service_identity_comes_from_the_resource(receiver: OTLPReceiver) -> None:
    """The regression a Span-level lookup would have caused.

    In OTLP the service identity is a ``ResourceSpans`` attribute, and a Span
    message has no ``resource`` field at all. Reading one off the span finds
    nothing and falls through to the "unknown" default — so every row in the
    system would be attributed to a service literally named "unknown", and the
    read path's ``service_name`` filter would match nothing. The test asserts
    the real name, which is the only thing that distinguishes this from the
    broken version.
    """
    await receiver._handle_batch(_request())
    names = {r[0] for r in _stored(receiver, "SELECT service_name FROM spans_substrate")}
    assert names == {"mahavishnu"}


async def test_replaying_the_same_request_is_a_dedup(receiver: OTLPReceiver) -> None:
    request = _request()
    assert (await receiver._handle_batch(request))["inserted"] == 1
    second = await receiver._handle_batch(request)
    assert second == {"inserted": 0, "deduped": 1, "rejected": 0}
    assert len(_stored(receiver, "SELECT 1 FROM spans_substrate")) == 1


async def test_span_ids_are_hex_encoded(receiver: OTLPReceiver) -> None:
    """OTLP carries span_id as bytes; the substrate stores hex.

    Bytes stored raw would render as b'\\x01\\x02...' and correlate with
    nothing. Hex is also what the read path's callers pass back as a filter.
    """
    await receiver._handle_batch(_request(trace_id=b"\xab" * 16, span_id=b"\xcd" * 8))
    rows = _stored(receiver, "SELECT span_id, trace_id FROM spans_substrate")
    assert rows[0][0] == "cd" * 8
    assert rows[0][1] == "ab" * 16


async def test_a_root_span_has_a_null_parent(receiver: OTLPReceiver) -> None:
    """proto3 leaves ``parent_span_id`` empty for a root span, not zero.

    Mapping that to the string ``""`` would make every root span look like it
    had a parent named empty-string, and any query filtering on parent would
    treat the roots as children of nothing.
    """
    await receiver._handle_batch(_request())
    assert _stored(receiver, "SELECT parent_span_id FROM spans_substrate")[0][0] is None


async def test_duration_is_derived_from_the_span_timestamps(
    receiver: OTLPReceiver,
) -> None:
    await receiver._handle_batch(_request(start=1_000_000_000))
    got = _stored(receiver, "SELECT duration_ms FROM spans_substrate")[0][0]
    assert got == pytest.approx(5.0)


async def test_read_path_columns_are_populated_not_left_in_the_json_blob(
    receiver: OTLPReceiver,
) -> None:
    """The columns ``SpanQuery.summary`` filters on must be real columns.

    Leaving these in the ``attributes`` JSON alone makes the whole read path
    return empty while ingest looks perfectly healthy: rows are landing, the
    table is populated, and every query misses because it filters on
    ``task_class``/``selector``/``outcome`` columns that are all NULL. That
    is the failure mode the fitness analyzer would have shipped as "every
    pair is idle".
    """
    request = _request(
        span_attrs=[
            common_pb2.KeyValue(key="task_class", value=_sv("mcp_tool_call")),
            common_pb2.KeyValue(key="selector", value=_sv("pool_route_execute")),
            common_pb2.KeyValue(key="outcome", value=_sv("success")),
            common_pb2.KeyValue(key="duration_ms", value=_sv("42.5")),
        ]
    )
    await receiver._handle_batch(request)
    row = _stored(
        receiver,
        "SELECT task_class, selector, outcome, latency_ms FROM spans_substrate",
    )[0]
    assert row == ("mcp_tool_call", "pool_route_execute", "success", 42.5)


async def test_the_semconv_dotted_task_class_is_also_accepted(
    receiver: OTLPReceiver,
) -> None:
    """``task.class`` is the canonical spelling; ``task_class`` is our convention.

    Producers set both (mahavishnu's enricher writes both), but a producer
    that emits only the semconv name must not become silently invisible —
    which is exactly what happened to the conversations path once already.
    """
    request = _request(
        span_attrs=[
            common_pb2.KeyValue(key="task.class", value=_sv("code_generation")),
            common_pb2.KeyValue(key="selector", value=_sv("least_loaded")),
        ]
    )
    await receiver._handle_batch(request)
    assert _stored(receiver, "SELECT task_class FROM spans_substrate")[0][0] == (
        "code_generation"
    )


async def test_latency_falls_back_to_the_span_duration_when_unparseable(
    receiver: OTLPReceiver,
) -> None:
    """A stringly-typed ``duration_ms`` must not NULL the whole p99.

    Falling back to the span's own timestamps beats storing NULL: the fitness
    analyzer reads a missing latency as "no data for this pair" when the
    number was sitting right there in start/end.
    """
    request = _request(
        start=1_000_000_000,
        span_attrs=[
            common_pb2.KeyValue(key="duration_ms", value=_sv("not-a-number"))
        ],
    )
    await receiver._handle_batch(request)
    assert _stored(receiver, "SELECT latency_ms FROM spans_substrate")[0][0] == (
        pytest.approx(5.0)
    )


async def test_gen_ai_columns_are_promoted_and_flagged(receiver: OTLPReceiver) -> None:
    """``has_gen_ai_attrs`` exists so readers can index-scan, not JSON-scan.

    A boolean the read path can filter on beats a JSON-contains over every
    row ever ingested.
    """
    request = _request(
        span_attrs=[
            common_pb2.KeyValue(key="gen_ai.request.model", value=_sv("minimax-M3")),
            common_pb2.KeyValue(
                key="gen_ai.usage.input_tokens",
                value=common_pb2.AnyValue(int_value=120),
            ),
        ]
    )
    await receiver._handle_batch(request)
    row = _stored(
        receiver,
        "SELECT gen_ai_request_model, gen_ai_usage_input_tokens, has_gen_ai_attrs "
        "FROM spans_substrate",
    )[0]
    assert row == ("minimax-M3", 120, True)


async def test_a_span_with_no_gen_ai_attrs_is_not_flagged(
    receiver: OTLPReceiver,
) -> None:
    """The flag must be False, not NULL — an index scan on NULL misses."""
    await receiver._handle_batch(_request())
    assert _stored(receiver, "SELECT has_gen_ai_attrs FROM spans_substrate")[0][0] is False


async def test_the_span_query_actually_finds_a_receiver_row(
    receiver: OTLPReceiver,
) -> None:
    """End-to-end: ingest then read, through the real SpanQuery.

    Every other test in this file asserts on SQL written here. This one goes
    through the class the fitness analyzer will actually call, so a column
    the receiver forgets to populate shows up as a failing read rather than as
    a quietly empty dashboard.
    """
    from akosha.storage.span_query import SpanQuery

    request = _request(
        span_attrs=[
            common_pb2.KeyValue(key="task_class", value=_sv("mcp_tool_call")),
            common_pb2.KeyValue(key="selector", value=_sv("pool_route_execute")),
            common_pb2.KeyValue(key="outcome", value=_sv("success")),
        ]
    )
    await receiver._handle_batch(request)

    summary = await SpanQuery(receiver._hot_store).summary(
        selector="pool_route_execute",
        task_class="mcp_tool_call",
        cutoff_unix_nano=0,
        scored_outcomes_only=False,
    )
    assert summary.span_count == 1
    assert summary.failure_count == 0
    assert summary.latency_p99_ms == pytest.approx(5.0)


async def test_received_at_comes_from_the_receiver_clock(
    receiver: OTLPReceiver,
) -> None:
    """§15's cardinal rule, pinned.

    Retention sweeps on ``received_at_unix_nano``. Deriving it from the
    producer's ``start_time_unix_nano`` makes two populations collapse: a
    producer with a fast clock has its spans deleted before anything ever
    reads them, and a producer with a slow clock has its spans outlive
    retention by exactly the skew. Both look like a retention bug and neither
    is. A batch replayed from a queue hours late is the same failure without
    any clock skew at all.
    """
    # A producer claiming to have started this span in 1970.
    await receiver._handle_batch(_request(start=0))
    got = _stored(receiver, "SELECT received_at_unix_nano FROM spans_substrate")[0][0]
    assert got > 1_600_000_000_000_000_000, "received_at must be a real wall-clock now()"


async def test_received_at_is_set_on_every_row(receiver: OTLPReceiver) -> None:
    """``NOT NULL`` with no server-side default — the receiver must supply it.

    Nothing in the OTLP wire format carries this value, so a mapping that
    omits it produces a row that the table rejects, and the rejection surfaces
    as a ConstraintException on the hot path rather than as a clear signal.
    """
    await receiver._handle_batch(_request())
    nulls = _stored(
        receiver, "SELECT count(*) FROM spans_substrate WHERE received_at_unix_nano IS NULL"
    )
    assert nulls[0][0] == 0


async def test_counters_accumulate_across_batches(receiver: OTLPReceiver) -> None:
    """All three counters, one batch each: fresh, replay, malformed.

    Sharing a receiver across batches is the normal case — batch processors
    flush on an interval and a flush can overlap a redelivery — so the three
    outcomes have to be distinguishable in one place rather than only in
    isolation.
    """
    await receiver._handle_batch(_request())
    await receiver._handle_batch(_request())  # same identity -> dedup
    await receiver._handle_batch(
        _request(service_name=None, span_id=b"\x09" * 8)
    )  # no service.name -> reject
    assert receiver.rows_inserted_total == 1
    assert receiver.rows_deduped_total == 1
    assert receiver.rows_rejected_total == 1


# ── DLQ ──────────────────────────────────────────────────────────────────


async def test_missing_service_identity_is_never_sentinel_filled(
    receiver: OTLPReceiver,
) -> None:
    """No ``"unknown"`` default for the REQUIRED_FIELDS at the mapping layer.

    A sentinel would make ``pre_validate`` unreachable: the mapping satisfies
    every required field on every row, so nothing is ever rejected and the DLQ
    stays permanently empty — while misconfigured producers write rows that no
    read-path filter can match. The absence has to survive into validation for
    an operator to have any chance of seeing it.
    """
    await receiver._handle_batch(_request(service_name=None))
    stored = _stored(receiver, "SELECT service_name FROM spans_substrate")
    assert stored == []
    quarantined = _stored(receiver, "SELECT field FROM _dlq_rows")
    assert quarantined == [("service_name",)]


async def test_a_span_with_no_name_is_rejected(receiver: OTLPReceiver) -> None:
    """``span_name`` is NOT NULL in the substrate and required at ingest.

    proto3 gives an unset string ``""``, which ``pre_validate`` treats as
    absent — so a nameless span is quarantined rather than stored under a
    sentinel that no downstream query would ever match.
    """
    result = await receiver._handle_batch(_request(span_name=""))
    assert result["rejected"] == 1
    assert _stored(receiver, "SELECT field FROM _dlq_rows") == [("span_name",)]


async def test_a_rejected_span_is_quarantined_not_inserted(
    receiver: OTLPReceiver,
) -> None:
    """Missing ``service.namespace`` must not become an "unknown" span.

    v4 added ``service_namespace`` to REQUIRED_FIELDS precisely because a
    defaulted namespace makes the substrate's tenancy story a fiction. A row
    that cannot satisfy it belongs in the DLQ where an operator can see it.
    """
    result = await receiver._handle_batch(_request(service_namespace=None))
    assert result["rejected"] == 1
    assert _stored(receiver, "SELECT 1 FROM spans_substrate") == []
    quarantined = _stored(receiver, "SELECT reason, field, span_id FROM _dlq_rows")
    assert len(quarantined) == 1
    assert (quarantined[0][0], quarantined[0][1]) == ("not_null", "service_namespace")
    assert quarantined[0][2] == "02" * 8


def _mixed_request() -> Any:
    """One request carrying a good resource and a namespace-less one.

    The two spans live in SEPARATE ``ResourceSpans`` because service identity
    is a resource attribute — appending a span to the same ResourceSpans
    would inherit its attributes, which is the bug this module pins rather
    than the behaviour under test here.
    """
    return trace_service_pb2.ExportTraceServiceRequest(
        resource_spans=[
            _resource_spans("mahavishnu", "bodai", span_id=b"\x03" * 8),
            _resource_spans(None, None, span_id=b"\x04" * 8),
        ]
    )


def _resource_spans(
    service_name: str | None,
    service_namespace: str | None,
    *,
    span_id: bytes,
) -> Any:
    attrs: list[Any] = []
    if service_name is not None:
        attrs.append(common_pb2.KeyValue(key="service.name", value=_sv(service_name)))
    if service_namespace is not None:
        attrs.append(
            common_pb2.KeyValue(key="service.namespace", value=_sv(service_namespace))
        )
    return trace_pb2.ResourceSpans(
        resource=resource_pb2.Resource(attributes=attrs),
        scope_spans=[
            trace_pb2.ScopeSpans(
                spans=[
                    trace_pb2.Span(
                        trace_id=b"\x03" * 16,
                        span_id=span_id,
                        name="mixed-span",
                        start_time_unix_nano=1_000_000_000,
                        end_time_unix_nano=2_000_000_000,
                    )
                ]
            )
        ],
    )


async def test_good_rows_land_even_when_a_bad_one_is_present(
    receiver: OTLPReceiver,
) -> None:
    """Validation is per row, not per batch.

    Rejecting the whole batch because one span in it was malformed would let a
    single bad producer stall every other producer's telemetry — and OTLP
    batches routinely mix services, since each producer flushes on its own
    schedule into a shared receiver.
    """
    result = await receiver._handle_batch(_mixed_request())
    assert (result["inserted"], result["rejected"]) == (1, 1)
    assert len(_stored(receiver, "SELECT 1 FROM spans_substrate")) == 1
    assert len(_stored(receiver, "SELECT 1 FROM _dlq_rows")) == 1


async def test_a_dlq_failure_does_not_abort_the_batch(
    receiver: OTLPReceiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§15's headline DLQ rule, and the reason the write is wrapped.

    A disk-full or locked substrate would otherwise turn a quarantine write
    into a batch-wide exception, and every good span in that batch would be
    lost to a failure that only concerned the rows that were already being
    thrown away.
    """

    async def _boom(_rows: list[dict[str, Any]]) -> int:
        raise RuntimeError("disk full")

    monkeypatch.setattr(receiver._hot_store, "insert_dlq_rows", _boom)

    result = await receiver._handle_batch(_mixed_request())
    assert result["inserted"] == 1
    assert result["rejected"] == 1
    assert len(_stored(receiver, "SELECT 1 FROM spans_substrate")) == 1
    assert receiver.dlq_insert_failed_total == 1


async def test_a_dlq_failure_still_advances_the_rejected_counter(
    receiver: OTLPReceiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rejection happened whether or not it was recorded.

    Without this the operator sees a DLQ failure counter climbing and a
    rejected counter frozen at zero, which reads as "the DLQ is broken but
    nothing is being rejected" — the opposite of the truth.
    """

    async def _boom(_rows: list[dict[str, Any]]) -> int:
        raise RuntimeError("disk full")

    monkeypatch.setattr(receiver._hot_store, "insert_dlq_rows", _boom)
    await receiver._handle_batch(_request(service_name=None))
    assert receiver.rows_rejected_total == 1
    assert receiver.dlq_insert_failed_total == 1


async def test_an_empty_export_is_a_noop(receiver: OTLPReceiver) -> None:
    empty = trace_service_pb2.ExportTraceServiceRequest()
    assert await receiver._handle_batch(empty) == {
        "inserted": 0, "deduped": 0, "rejected": 0,
    }


# ── fail-loud boot ───────────────────────────────────────────────────────


async def test_start_raises_when_the_substrate_is_missing(
    store: HotStore, receiver: OTLPReceiver
) -> None:
    """A receiver that binds against an absent table looks perfectly healthy.

    It accepts connections, returns 200s, and stores nothing. The only symptom
    is a read path that quietly returns empty forever — which is precisely the
    failure this substrate exists to make impossible. So the absence is a hard
    boot failure, not a warning.
    """
    from oneiric.core.lifecycle import LifecycleError

    store.conn.execute("DROP TABLE spans_substrate")  # type: ignore[union-attr]
    with pytest.raises(LifecycleError, match="spans_substrate_missing"):
        await receiver.start()


async def test_start_does_not_bind_when_the_substrate_is_missing(
    store: HotStore, receiver: OTLPReceiver
) -> None:
    """The assertion has to precede the bind, not follow it.

    Asserting after binding would leave listeners open on a port that will
    never receive a span, and the next start attempt would then fail with a
    port-in-use error that hides the real cause.
    """
    store.conn.execute("DROP TABLE spans_substrate")  # type: ignore[union-attr]
    try:
        await receiver.start()
    except Exception:
        pass
    assert receiver._grpc_server is None


async def test_stop_is_safe_before_start(store: HotStore) -> None:
    """Lifespan teardown runs even when start() failed partway."""
    receiver = OTLPReceiver(store, grpc_port=0, http_port=0)
    await receiver.stop()


async def test_row_validation_error_is_hashable() -> None:
    """Used as a metric-label dedup key in PR-A4; frozen dataclass gives it that."""
    error = RowValidationError(field="service_name", reason="not_null")
    assert {error} == {RowValidationError(field="service_name", reason="not_null")}