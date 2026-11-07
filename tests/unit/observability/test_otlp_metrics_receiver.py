"""``_MetricsServiceServicer`` — the OTLP MetricsService on the substrate.

Driven with real protobuf messages, not doubles. The mapping from
``ResourceMetrics`` to a substrate row is where the silent-wrongness bugs
live — a service identity read off the wrong level, an attribute blob
pre-serialized in the producer's key order, a timestamp in the wrong unit —
and a hand-rolled fake would happily carry the wrong field names through a
test that passes.

The port binding itself is NOT covered here: it needs a live listener, and
it lives in ``tests/integration/test_otlp_metrics_receiver_e2e.py``, which
binds a real gRPC server and sends a real export over the wire. What IS
covered here is that both servicers are REGISTERED on the same server,
because a receiver that answers ``UNIMPLEMENTED`` for the metric method is
the failure this whole change exists to fix.

One constraint the tests inherit: the Prometheus registry is a PROCESS-WIDE
singleton, and ``reset_all_metrics`` zeroes its values without removing
its collectors. A metric name another test already registered — with a
DIFFERENT attribute key set — is therefore exposed under a SIBLING name
(``<name>__<signature>``), because one instrument name carries exactly one
label set. So a metric name used here must not be reused elsewhere in the
suite with a different key set.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.metrics.v1 import metrics_pb2
from opentelemetry.proto.resource.v1 import resource_pb2
from prometheus_client import generate_latest

from akosha.observability.otlp_receiver import OTLPReceiver, _MetricsServiceServicer
from akosha.observability.prometheus_metrics import (
    get_metrics_registry,
    reset_all_metrics,
)
from akosha.storage.hot_store import HotStore

if TYPE_CHECKING:
    from collections.abc import Generator

#: A fixed instant, so a test that replays the same point replays the SAME
#: point. The substrate's identity includes the timestamp.
TS: int = 1_700_000_000_000_000_000


@pytest.fixture(autouse=True)
def _reset_shared_registry() -> Generator[None]:
    """Zero the shared registry around every test in this module.

    The registry is a process-wide singleton and the receiver exposes into
    it, so a counter left at 7 by one test would make the next test's
    assertion a lie. ``reset_all_metrics`` zeroes values in place — it
    does not replace the registry — so every module that captured the
    registry stays wired.
    """
    reset_all_metrics()
    yield
    reset_all_metrics()


@pytest.fixture
async def store() -> HotStore:
    hs = HotStore(database_path=":memory:", embedding_dim=384)
    await hs.initialize()
    return hs


@pytest.fixture
def receiver(store: HotStore) -> OTLPReceiver:
    return OTLPReceiver(store, grpc_port=0, http_port=0)


@pytest.fixture
def servicer(receiver: OTLPReceiver) -> _MetricsServiceServicer:
    return _MetricsServiceServicer(receiver)


# ── builders ─────────────────────────────────────────────────────────────


def _sv(value: str) -> common_pb2.AnyValue:
    return common_pb2.AnyValue(string_value=value)


def _resource(
    *,
    service_name: str | None = "mahavishnu",
    service_namespace: str | None = "bodai",
    scope_name: str = "akosha.test",
    scope_version: str = "1.0",
    metrics: list[metrics_pb2.Metric] | None = None,
) -> metrics_pb2.ResourceMetrics:
    """A real ``ResourceMetrics`` carrying ``metrics``."""
    resource_attributes: list[common_pb2.KeyValue] = []
    if service_name is not None:
        resource_attributes.append(common_pb2.KeyValue(key="service.name", value=_sv(service_name)))
    if service_namespace is not None:
        resource_attributes.append(
            common_pb2.KeyValue(key="service.namespace", value=_sv(service_namespace))
        )
    return metrics_pb2.ResourceMetrics(
        resource=resource_pb2.Resource(attributes=resource_attributes),
        scope_metrics=[
            metrics_pb2.ScopeMetrics(
                scope=common_pb2.InstrumentationScope(name=scope_name, version=scope_version),
                metrics=metrics or [],
            )
        ],
    )


def _sum(
    name: str,
    value: int,
    *,
    monotonic: bool = True,
    timestamp: int = TS,
    attributes: list[common_pb2.KeyValue] | None = None,
) -> metrics_pb2.Metric:
    """A monotonic (or not) sum metric."""
    return metrics_pb2.Metric(
        name=name,
        sum=metrics_pb2.Sum(
            data_points=[
                metrics_pb2.NumberDataPoint(
                    attributes=attributes or [],
                    time_unix_nano=timestamp,
                    as_int=value,
                )
            ],
            aggregation_temporality=(
                metrics_pb2.AggregationTemporality.AGGREGATION_TEMPORALITY_CUMULATIVE
            ),
            is_monotonic=monotonic,
        ),
    )


def _gauge(
    name: str,
    value: float,
    *,
    timestamp: int = TS,
    attributes: list[common_pb2.KeyValue] | None = None,
) -> metrics_pb2.Metric:
    """A gauge metric."""
    return metrics_pb2.Metric(
        name=name,
        gauge=metrics_pb2.Gauge(
            data_points=[
                metrics_pb2.NumberDataPoint(
                    attributes=attributes or [],
                    time_unix_nano=timestamp,
                    as_double=value,
                )
            ]
        ),
    )


def _histogram(
    name: str,
    *,
    counts: list[int] | None = None,
    bounds: list[float] | None = None,
    total: float = 6.0,
    observation_count: int = 3,
    timestamp: int = TS,
) -> metrics_pb2.Metric:
    """An explicit-bucket histogram metric."""
    return metrics_pb2.Metric(
        name=name,
        histogram=metrics_pb2.Histogram(
            data_points=[
                metrics_pb2.HistogramDataPoint(
                    time_unix_nano=timestamp,
                    count=observation_count,
                    sum=total,
                    bucket_counts=counts if counts is not None else [1, 1, 1],
                    explicit_bounds=bounds if bounds is not None else [1.0, 2.0],
                )
            ],
            aggregation_temporality=(
                metrics_pb2.AggregationTemporality.AGGREGATION_TEMPORALITY_CUMULATIVE
            ),
        ),
    )


def _request(
    *resources: metrics_pb2.ResourceMetrics,
) -> metrics_service_pb2.ExportMetricsServiceRequest:
    """An ``ExportMetricsServiceRequest`` carrying ``resources``."""
    return metrics_service_pb2.ExportMetricsServiceRequest(resource_metrics=list(resources))


def _delta_sum(
    name: str,
    value: int,
    *,
    timestamp: int = TS,
    attributes: list[common_pb2.KeyValue] | None = None,
) -> metrics_pb2.Metric:
    """A monotonic sum with DELTA temporality.

    A delta point is an increment, not a level: the same point exported
    twice is the same increment twice, and the receiver has to treat the
    second one as a replay.
    """
    return metrics_pb2.Metric(
        name=name,
        sum=metrics_pb2.Sum(
            data_points=[
                metrics_pb2.NumberDataPoint(
                    attributes=attributes or [],
                    time_unix_nano=timestamp,
                    as_int=value,
                )
            ],
            aggregation_temporality=(
                metrics_pb2.AggregationTemporality.AGGREGATION_TEMPORALITY_DELTA
            ),
            is_monotonic=True,
        ),
    )


def _stored(receiver: OTLPReceiver, sql: str) -> list[tuple[Any, ...]]:
    conn = receiver._hot_store.conn
    assert conn is not None
    return conn.execute(sql).fetchall()


def _registry_text() -> str:
    return generate_latest(get_metrics_registry()).decode("utf-8")


def _sample(needle: str) -> float | None:
    """The value of the first rendered sample whose line contains ``needle``.

    ``None`` when the sample is not on the registry at all, which is a
    different fact from "the sample is zero".
    """
    for line in _registry_text().splitlines():
        if line.startswith("#"):
            continue
        if needle in line:
            return float(line.rsplit(" ", 1)[1])
    return None


# ── acceptance ───────────────────────────────────────────────────────────


async def test_a_valid_batch_is_accepted_with_no_rejections(
    servicer: _MetricsServiceServicer,
) -> None:
    """A sum, a gauge and a histogram in one export: all of them land.

    The three types are exercised together because they share the walk
    (``resource_metrics → scope_metrics → metrics → data points``) and
    diverge only at the leaf. A walk that handled sums but not histograms
    would still pass a sum-only test.
    """
    request = _request(
        _resource(
            metrics=[
                _sum("akosha.requests", 7),
                _gauge("akosha.queue.depth", 3.5),
                _histogram("akosha.request.duration"),
            ]
        )
    )
    response = await servicer.Export(request, None)
    assert response.partial_success.rejected_data_points == 0
    assert response.partial_success.error_message == ""
    names = {
        row[0] for row in _stored(servicer._receiver, "SELECT metric_name FROM metrics_substrate")
    }
    assert names == {"akosha.requests", "akosha.queue.depth", "akosha.request.duration"}


async def test_the_response_is_an_export_metrics_service_response(
    servicer: _MetricsServiceServicer,
) -> None:
    """The counts ride on ``partial_success`` — the only place the OTLP
    protobuf carries them.

    A producer reading ``rejected_data_points`` is the signal that a
    validation regression is visible from outside the process; a count on
    the receiver's own counter is invisible to the producer.
    """
    response = await servicer.Export(_request(_resource(metrics=[_sum("akosha.ok", 1)])), None)
    assert isinstance(response, metrics_service_pb2.ExportMetricsServiceResponse)
    assert response.partial_success.rejected_data_points == 0


async def test_the_rejected_count_travels_back_to_the_producer(
    servicer: _MetricsServiceServicer,
) -> None:
    """A schema-invalid point is COUNTED, not turned into an RPC failure."""
    request = _request(
        _resource(metrics=[_sum("akosha.no.namespace", 1)]),
        _resource(service_name=None, metrics=[_sum("akosha.unnamed", 1)]),
    )
    response = await servicer.Export(request, None)
    assert response.partial_success.rejected_data_points == 1


# ── persistence ──────────────────────────────────────────────────────────


async def test_rows_land_in_the_metrics_substrate(receiver: OTLPReceiver) -> None:
    """The durable half of the contract: the point is in the substrate."""
    await receiver._handle_metrics_batch(_request(_resource(metrics=[_sum("akosha.requests", 7)])))
    row = _stored(
        receiver,
        "SELECT ts, service_name, service_namespace, scope_name, metric_name, "
        "data_type, value_sum, value_count, is_monotonic FROM metrics_substrate",
    )
    assert row == [
        (TS, "mahavishnu", "bodai", "akosha.test", "akosha.requests", "sum", 7.0, 1, True)
    ]


async def test_the_timestamp_is_stored_in_epoch_nanoseconds(
    receiver: OTLPReceiver,
) -> None:
    """NANOSECONDS, not milliseconds.

    The sibling ``spans_substrate`` table stores ``start_time_unix_nano``
    in the same DuckDB file. Two time units in one substrate is a silent
    trap: every read path that compares a span's timestamp with a metric's
    would be off by a factor of a million, and nothing would say so.
    """
    await receiver._handle_metrics_batch(_request(_resource(metrics=[_sum("akosha.requests", 7)])))
    stored = _stored(receiver, "SELECT ts FROM metrics_substrate")[0][0]
    assert stored == TS
    assert stored > 1_000_000_000_000_000_000


async def test_a_replay_dedupes_rather_than_double_storing(
    receiver: OTLPReceiver,
) -> None:
    """A replay is a no-op, not an error.

    A metric data point is a sample of a time series, so its identity is
    the series plus the instant — not a key the producer supplies. The
    same point arriving twice is the same point.
    """
    request = _request(_resource(metrics=[_sum("akosha.requests", 7)]))
    first = await receiver._handle_metrics_batch(request)
    second = await receiver._handle_metrics_batch(request)
    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert second["deduped"] == 1
    assert len(_stored(receiver, "SELECT 1 FROM metrics_substrate")) == 1


async def test_attribute_key_order_does_not_create_two_identities(
    receiver: OTLPReceiver,
) -> None:
    """The canonicalization contract, pinned.

    DuckDB compares a JSON column inside a UNIQUE constraint **as text**,
    so the substrate canonicalizes the attributes itself (sorted keys,
    compact separators). If the receiver pre-serialized them in the
    producer's own key order, ``{"a":1,"b":2}`` and ``{"b":2,"a":1}`` would
    be two rows and the same measurement would be stored twice — for every
    producer whose attribute order is not stable.
    """
    first = _sum(
        "akosha.requests",
        7,
        attributes=[
            common_pb2.KeyValue(key="a", value=common_pb2.AnyValue(int_value=1)),
            common_pb2.KeyValue(key="b", value=common_pb2.AnyValue(int_value=2)),
        ],
    )
    second = _sum(
        "akosha.requests",
        7,
        attributes=[
            common_pb2.KeyValue(key="b", value=common_pb2.AnyValue(int_value=2)),
            common_pb2.KeyValue(key="a", value=common_pb2.AnyValue(int_value=1)),
        ],
    )
    await receiver._handle_metrics_batch(_request(_resource(metrics=[first])))
    result = await receiver._handle_metrics_batch(_request(_resource(metrics=[second])))
    assert result["inserted"] == 0
    assert result["deduped"] == 1
    assert len(_stored(receiver, "SELECT 1 FROM metrics_substrate")) == 1


async def test_a_producer_without_a_namespace_is_accepted(
    receiver: OTLPReceiver,
) -> None:
    """oneiric's ``OTLPObservabilityAdapter`` declares no namespace.

    Its Resource carries ``service.name``, ``deployment.environment`` and
    (optionally) ``service.version`` — and nothing else. Requiring a
    namespace would quarantine every metric oneiric sends, which is the
    whole of REQ-M07's second producer: the export would answer "accepted,
    zero points stored" and nothing would say why.

    The namespace is stored as the empty string, which says "not declared"
    — a different fact from "unknown", and it keeps the substrate's NOT
    NULL constraint satisfied without inventing a value.
    """
    await receiver._handle_metrics_batch(
        _request(_resource(service_namespace=None, metrics=[_sum("akosha.requests", 7)]))
    )
    row = _stored(receiver, "SELECT service_name, service_namespace FROM metrics_substrate")
    assert row == [("mahavishnu", "")]


# ── quarantine ───────────────────────────────────────────────────────────


async def test_an_invalid_point_is_quarantined_and_the_rest_still_lands(
    receiver: OTLPReceiver,
) -> None:
    """Validation is per point, not per batch.

    Rejecting the whole batch because one point in it was malformed would
    let a single bad producer stall every other producer's telemetry — and
    OTLP batches routinely mix services, since each producer flushes on
    its own schedule into a shared receiver.
    """
    request = _request(
        _resource(metrics=[_sum("akosha.good", 7)]),
        # No service.name: the point cannot be attributed to a series.
        _resource(service_name=None, metrics=[_sum("akosha.unnamed", 1)]),
    )
    result = await receiver._handle_metrics_batch(request)
    assert result["inserted"] == 1
    assert result["rejected"] == 1
    assert len(_stored(receiver, "SELECT 1 FROM metrics_substrate")) == 1
    quarantined = _stored(receiver, "SELECT reason, field FROM _dlq_rows")
    assert quarantined == [("not_null", "service_name")]


async def test_a_point_with_no_timestamp_is_rejected(
    receiver: OTLPReceiver,
) -> None:
    """A point that cannot be placed on a time series is not stored.

    The substrate's identity includes the timestamp, so a point at
    ``ts=0`` would collide with every other point at ``ts=0`` — and a
    zero timestamp is not a time.
    """
    request = _request(_resource(metrics=[_sum("akosha.requests", 7, timestamp=0)]))
    result = await receiver._handle_metrics_batch(request)
    assert result["rejected"] == 1
    assert _stored(receiver, "SELECT reason, field FROM _dlq_rows") == [("not_positive", "ts")]


async def test_a_dlq_failure_does_not_abort_the_batch(
    receiver: OTLPReceiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DLQ outage must not take the good points with it.

    A disk-full or locked substrate would otherwise turn a quarantine
    write into a batch-wide exception, and every good point in that batch
    would be lost to a failure that only concerned the rows that were
    already being thrown away.
    """

    async def _boom(_rows: list[dict[str, Any]]) -> int:
        raise RuntimeError("disk full")

    monkeypatch.setattr(receiver._hot_store, "insert_dlq_rows", _boom)
    request = _request(
        _resource(metrics=[_sum("akosha.good", 7)]),
        _resource(service_name=None, metrics=[_sum("akosha.unnamed", 1)]),
    )
    result = await receiver._handle_metrics_batch(request)
    assert result["inserted"] == 1
    assert result["rejected"] == 1
    assert len(_stored(receiver, "SELECT 1 FROM metrics_substrate")) == 1
    assert receiver.dlq_insert_failed_total == 1


async def test_a_substrate_failure_does_not_fail_the_export(
    receiver: OTLPReceiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception must never escape the servicer.

    gRPC would answer UNKNOWN, and the producer would drop the ENTIRE
    batch — every point in it, not just the one that failed.
    """

    async def _boom(_rows: list[dict[str, Any]]) -> tuple[int, int]:
        raise RuntimeError("disk full")

    monkeypatch.setattr(receiver._hot_store, "insert_metrics_substrate", _boom, raising=False)
    result = await receiver._handle_metrics_batch(
        _request(_resource(metrics=[_sum("akosha.good", 7)]))
    )
    assert result["inserted"] == 0
    assert result["rejected"] == 0
    assert receiver.metrics_insert_failed_total == 1


# ── Prometheus exposure ──────────────────────────────────────────────────


async def test_the_metric_is_visible_on_the_shared_registry(
    receiver: OTLPReceiver,
) -> None:
    """The exposure contract: the series reaches ``/metrics``.

    ``akosha/mcp/server.py`` renders the shared registry on the ``/metrics``
    route, and Prometheus already scrapes that target — so a metric that
    lands in the registry is queryable without a new collector, a new
    scrape target, or a second telemetry pipeline.
    """
    await receiver._handle_metrics_batch(
        _request(
            _resource(
                metrics=[
                    _sum(
                        "mahavishnu.observability.drift_detected",
                        7,
                        attributes=[common_pb2.KeyValue(key="task.class", value=_sv("code"))],
                    )
                ]
            )
        )
    )
    text = _registry_text()
    assert 'mahavishnu_observability_drift_detected_total{task_class="code"} 7.0' in text


async def test_a_producer_naming_its_metrics_after_itself_is_not_double_prefixed(
    receiver: OTLPReceiver,
) -> None:
    """The plan's own acceptance check is ``grep mahavishnu_observability``.

    A naive ``<service>_<metric>`` namespace would render mahavishnu's
    instruments as ``mahavishnu_mahavishnu_observability_drift_detected_total``
    and the grep would find nothing — a "successful" implementation that
    no operator could verify.
    """
    await receiver._handle_metrics_batch(
        _request(_resource(metrics=[_sum("mahavishnu.workflows.executed", 7)]))
    )
    text = _registry_text()
    assert "mahavishnu_workflows_executed_total 7.0" in text
    assert "mahavishnu_mahavishnu" not in text


async def test_two_producers_produce_distinct_series(receiver: OTLPReceiver) -> None:
    """REQ-M07: mahavishnu's exporter and oneiric's adapter are
    indistinguishable on the wire, and both speak MetricsService to :4317.

    They can emit the same instrument name, so the exposed series are
    namespaced by the resource's ``service.name``. Without that, the
    second producer's metric either collides with the first's in the
    shared registry or silently overwrites it.
    """
    await receiver._handle_metrics_batch(
        _request(
            _resource(service_name="mahavishnu", metrics=[_sum("shared.metric", 1)]),
            _resource(service_name="oneiric", metrics=[_sum("shared.metric", 2)]),
        )
    )
    text = _registry_text()
    assert "mahavishnu_shared_metric_total 1.0" in text
    assert "oneiric_shared_metric_total 2.0" in text


async def test_a_cumulative_counter_is_not_counted_twice(
    receiver: OTLPReceiver,
) -> None:
    """A cumulative export carries an ABSOLUTE total, not an increment.

    Adding it straight to a Prometheus counter would double the value on
    every export. The bridge turns a cumulative value into the delta since
    the previous export, which is what makes the exposed counter mean what
    a Prometheus counter means.
    """
    await receiver._handle_metrics_batch(_request(_resource(metrics=[_sum("akosha.requests", 10)])))
    await receiver._handle_metrics_batch(_request(_resource(metrics=[_sum("akosha.requests", 10)])))
    assert "mahavishnu_akosha_requests_total 10.0" in _registry_text()
    await receiver._handle_metrics_batch(_request(_resource(metrics=[_sum("akosha.requests", 15)])))
    assert "mahavishnu_akosha_requests_total 15.0" in _registry_text()


async def test_a_non_monotonic_sum_is_exposed_as_a_gauge(
    receiver: OTLPReceiver,
) -> None:
    """An "up-down counter" has no Prometheus counterpart.

    The OpenTelemetry convention is to expose a non-monotonic sum as a
    Gauge, and a Gauge is a LEVEL: the latest value simply wins.
    """
    await receiver._handle_metrics_batch(
        _request(_resource(metrics=[_sum("akosha.queue.depth", 3, monotonic=False)]))
    )
    text = _registry_text()
    assert "mahavishnu_akosha_queue_depth 3.0" in text
    assert "mahavishnu_akosha_queue_depth_total" not in text


async def test_a_histogram_keeps_the_producers_bucket_boundaries(
    receiver: OTLPReceiver,
) -> None:
    """The bucket ladder comes from the producer, not from a default.

    ``prometheus_client``'s default buckets are tuned for HTTP latencies
    in seconds; a producer measuring bytes would have every observation
    land in the same default bucket, and the histogram would answer
    nothing.
    """
    await receiver._handle_metrics_batch(
        _request(
            _resource(
                metrics=[
                    _histogram(
                        "akosha.payload.size",
                        counts=[2, 3, 1],
                        bounds=[10.0, 100.0],
                        total=450.0,
                        observation_count=6,
                    )
                ]
            )
        )
    )
    text = _registry_text()
    assert 'akosha_payload_size_bucket{le="10.0"} 2.0' in text
    assert 'akosha_payload_size_bucket{le="100.0"} 5.0' in text
    assert 'akosha_payload_size_bucket{le="+Inf"} 6.0' in text
    assert "akosha_payload_size_count 6.0" in text


async def test_target_info_and_scope_info_are_published(
    receiver: OTLPReceiver,
) -> None:
    """The Collector's resource and scope conversions.

    ``target_info`` says which service a series came from and
    ``otel_scope_info`` which instrumentation scope produced it — without
    them a series in a shared registry is unattributable.
    """
    await receiver._handle_metrics_batch(
        _request(
            _resource(
                scope_name="mahavishnu.core",
                scope_version="1.2",
                metrics=[_sum("akosha.requests", 1)],
            )
        )
    )
    text = _registry_text()
    assert 'target_info{deployment_environment="",service_instance_id="",' in text
    assert 'service_name="mahavishnu"' in text
    assert 'otel_scope_info{otel_scope_name="mahavishnu.core"' in text
    assert 'otel_scope_version="1.2"' in text


async def test_the_cardinality_cap_drops_the_excess_but_keeps_the_point(
    receiver: OTLPReceiver,
) -> None:
    """A producer that puts a request id in an attribute must not be able
    to mint an unbounded number of series in a registry that is rendered
    on every scrape.

    The excess is dropped from the EXPOSURE, not from the substrate: the
    point is still stored, with its attributes verbatim, so nothing is
    lost — it is only not rendered.
    """
    for index in range(105):
        await receiver._handle_metrics_batch(
            _request(
                _resource(
                    metrics=[
                        _sum(
                            "akosha.cardinality.probe",
                            1,
                            attributes=[
                                common_pb2.KeyValue(key="request_id", value=_sv(str(index)))
                            ],
                        )
                    ]
                )
            )
        )
    stored = _stored(receiver, "SELECT count(*) FROM metrics_substrate")
    assert stored[0][0] == 105
    text = _registry_text()
    # One rendered sample per surviving label combination, and no more.
    assert text.count("mahavishnu_akosha_cardinality_probe_total{") == 100


# ── the other metric types ───────────────────────────────────────────────


async def test_an_exponential_histogram_is_accepted_and_stored(
    receiver: OTLPReceiver,
) -> None:
    """The exponential layout is stored verbatim, not flattened.

    Re-expressing an exponential histogram's buckets as explicit bounds
    would lose the scale that makes them compact, and the substrate's
    ``buckets_json`` is free-form for exactly this.
    """
    metric = metrics_pb2.Metric(
        name="akosha.exp.duration",
        exponential_histogram=metrics_pb2.ExponentialHistogram(
            data_points=[
                metrics_pb2.ExponentialHistogramDataPoint(
                    time_unix_nano=TS,
                    count=2,
                    sum=3.0,
                    scale=0,
                    zero_count=0,
                    positive=metrics_pb2.ExponentialHistogramDataPoint.Buckets(
                        offset=0, bucket_counts=[1, 1]
                    ),
                )
            ],
            aggregation_temporality=(
                metrics_pb2.AggregationTemporality.AGGREGATION_TEMPORALITY_CUMULATIVE
            ),
        ),
    )
    result = await receiver._handle_metrics_batch(_request(_resource(metrics=[metric])))
    assert result["rejected"] == 0
    row = _stored(
        receiver,
        "SELECT data_type, value_sum, value_count, buckets_json FROM metrics_substrate",
    )[0]
    assert row[0] == "exponential_histogram"
    assert row[1] == pytest.approx(3.0)
    assert row[2] == 2
    assert '"scale":0' in row[3]


async def test_a_summary_is_accepted_and_stored(receiver: OTLPReceiver) -> None:
    """A summary's count and sum are stored; its quantiles have no column."""
    metric = metrics_pb2.Metric(
        name="akosha.latency.summary",
        summary=metrics_pb2.Summary(
            data_points=[metrics_pb2.SummaryDataPoint(time_unix_nano=TS, count=4, sum=8.0)]
        ),
    )
    result = await receiver._handle_metrics_batch(_request(_resource(metrics=[metric])))
    assert result["rejected"] == 0
    row = _stored(
        receiver, "SELECT data_type, value_sum, value_count, buckets_json FROM metrics_substrate"
    )[0]
    assert row == ("summary", pytest.approx(8.0), 4, None)


# ── registration ─────────────────────────────────────────────────────────


async def test_both_services_are_registered_on_the_same_server(
    receiver: OTLPReceiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MetricsService is registered BESIDE the TraceService.

    One endpoint serving both signals is not a convenience: oneiric's
    ``OTLPObservabilityAdapter`` sends traces and metrics to ONE endpoint,
    so a receiver that serves only one of the two cannot satisfy that
    producer at all. And a receiver that answers ``UNIMPLEMENTED`` for the
    metric method is the exact failure this change exists to fix.
    """
    from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2_grpc
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2_grpc

    registered: list[str] = []

    def _record_trace(_servicer: Any, _server: Any) -> None:
        registered.append("trace")

    def _record_metrics(_servicer: Any, _server: Any) -> None:
        registered.append("metrics")

    monkeypatch.setattr(
        trace_service_pb2_grpc,
        "add_TraceServiceServicer_to_server",
        _record_trace,
    )
    monkeypatch.setattr(
        metrics_service_pb2_grpc,
        "add_MetricsServiceServicer_to_server",
        _record_metrics,
    )
    await receiver._start_grpc()
    try:
        assert registered == ["trace", "metrics"]
    finally:
        await receiver.stop()


# ── the substrate contract (a broken substrate is not a success) ───────────


async def test_a_broken_substrate_is_reported_to_the_producer(
    receiver: OTLPReceiver,
    servicer: _MetricsServiceServicer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A durable write that cannot happen must not look like a clean success.

    The failure this pins: the substrate write was wrapped in a blanket
    ``except``, so a substrate that is missing — the oneiric dependency
    gap, or a table that was never created — incremented a counter and
    then answered ``partial_success`` with ``rejected_data_points=0`` and
    an empty message. The producer was told "accepted" for data that was
    being thrown away.

    The point is NOT rejected (it is perfectly valid), so the count stays
    zero; what the producer needs is the MESSAGE. And the exposure still
    happens: the points reach the Prometheus registry, and only the
    durable write failed.
    """

    async def _boom(_rows: list[dict[str, Any]]) -> tuple[int, int]:
        raise RuntimeError("metrics_substrate is not there")

    monkeypatch.setattr(receiver._hot_store, "insert_metrics_substrate", _boom, raising=False)
    response = await servicer.Export(
        _request(_resource(metrics=[_sum("akosha.requests", 7)])),
        None,
    )
    # The producer is told: this is not a clean success.
    assert response.HasField("partial_success")
    assert response.partial_success.error_message != ""
    # The point was not INVALID, so it is not counted as a rejection.
    assert response.partial_success.rejected_data_points == 0
    # The exposure still happened — only the durable write failed.
    assert "mahavishnu_akosha_requests_total 7.0" in _registry_text()
    # And the failure is distinguishable from a normal batch failure: the
    # reason label says WHICH fault it was. Which one depends on whether
    # the substrate itself is there — a missing one is an operator-actionable
    # outage, a failed write is usually transient.
    expected = "write_failed" if receiver.metrics_substrate_ready else "substrate_unavailable"
    assert _sample(f'akosha_otlp_metrics_insert_failed_total{{reason="{expected}"}}') == 1.0


async def test_the_receiver_still_binds_without_the_phase_one_primitives(
    store: HotStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The PUBLISHED oneiric shape: the receiver boots, says so, and serves.

    **Which oneiric release carries REQ-M03's primitives? As of this
    commit, none.** ``insert_metrics_substrate`` and
    ``metrics_substrate_table`` exist only on oneiric's LOCAL, unpushed
    ``main`` (commit ``f869951``, branch ``feat/metrics-substrate``). The
    published 0.34.0 that akosha's ``uv.lock`` pins has neither the
    primitive nor the DDL, so a clean ``uv sync`` in akosha produces
    exactly the shape modelled here.

    The dependency gap must be LOUD, not silent:

    * the receiver BINDS — the MetricsService rides the same gRPC server as
      the TraceService, so failing the boot over a METRICS dependency would
      take the trace path down with it and would break every deployment
      that has not upgraded oneiric yet;
    * the readiness gauge reads 0, so ``/metrics`` says the durable write
      is not happening;
    * the export's ``partial_success`` carries a message, so the PRODUCER
      knows its data is not being stored;
    * the metric is still exposed on ``/metrics``, because the points DID
      reach the Prometheus registry.

    This test does not make the suite fail because the dependency has not
    been published: it simulates the published shape on purpose.
    """
    from oneiric.adapters.vector.duckdb_hot_store import DuckdbHotStore

    # ``raising=False`` so the test holds in BOTH shapes: the normal one
    # (the primitives present) and the published-oneiric one (they are
    # already absent). The contract it pins does not depend on which.
    monkeypatch.delattr(DuckdbHotStore, "insert_metrics_substrate", raising=False)
    monkeypatch.delattr(DuckdbHotStore, "metrics_substrate_table", raising=False)
    store_conn = store.conn
    assert store_conn is not None
    store_conn.execute("DROP TABLE IF EXISTS metrics_substrate")

    receiver = OTLPReceiver(store, grpc_port=0, http_port=0)
    await receiver.start()
    try:
        assert receiver.metrics_substrate_ready is False
        assert "akosha_otlp_metrics_substrate_ready 0.0" in _registry_text()

        servicer = _MetricsServiceServicer(receiver)
        response = await servicer.Export(
            _request(_resource(metrics=[_sum("akosha.requests", 7)])),
            None,
        )
        assert response.HasField("partial_success")
        assert "substrate" in response.partial_success.error_message
        # The exposure path does not touch the substrate.
        assert "mahavishnu_akosha_requests_total 7.0" in _registry_text()
        assert (
            _sample('akosha_otlp_metrics_insert_failed_total{reason="substrate_unavailable"}')
            == 1.0
        )
    finally:
        await receiver.stop()


async def test_a_healthy_substrate_reports_ready(
    receiver: OTLPReceiver,
) -> None:
    """The gauge is 1 when the substrate is there — the other half of the signal."""
    ready, detail = await receiver._refresh_substrate_readiness(force=True)
    assert ready is True
    assert detail == ""
    assert "akosha_otlp_metrics_substrate_ready 1.0" in _registry_text()


# ── the response contract ────────────────────────────────────────────────


async def test_a_clean_export_carries_no_partial_success(
    servicer: _MetricsServiceServicer,
) -> None:
    """The OTLP spec: a fully successful export MUST NOT set partial_success.

    An always-present empty ``partial_success`` is exactly the shape a
    "the receiver is throwing your data away" message would have if it
    were always empty — a producer that learns to expect the field learns
    to ignore it. The OTel Python SDK tolerates its absence.
    """
    response = await servicer.Export(
        _request(_resource(metrics=[_sum("akosha.requests", 7)])),
        None,
    )
    assert not response.HasField("partial_success")


async def test_a_rejected_point_carries_a_message(
    servicer: _MetricsServiceServicer,
) -> None:
    """The message says what was rejected, not just how many."""
    request = _request(
        _resource(metrics=[_sum("akosha.good", 7)]),
        _resource(service_name=None, metrics=[_sum("akosha.unnamed", 1)]),
    )
    response = await servicer.Export(request, None)
    assert response.HasField("partial_success")
    assert response.partial_success.rejected_data_points == 1
    assert "rejected" in response.partial_success.error_message
    assert "service_name" in response.partial_success.error_message


# ── temporality: the counter and the histogram agree about a restart ─────


async def test_a_producer_restart_does_not_lose_the_histogram_interval(
    receiver: OTLPReceiver,
) -> None:
    """A cumulative histogram whose counts move backwards is a RESTART.

    The bucket ladder clamps its deltas at zero, so a producer that
    restarted used to have that whole interval's observations discarded
    for ever: the counts went back to near zero, every delta clamped to
    0, and the exposed histogram froze at its pre-restart value until
    the producer climbed past it again.

    The counter path already handled this — a value that went backwards
    is re-baselined, which is what a Prometheus counter reset means — and
    the histogram path now applies the SAME rule rather than a second,
    different one.
    """
    await receiver._handle_metrics_batch(
        _request(
            _resource(
                metrics=[
                    _histogram(
                        "akosha.duration",
                        counts=[100, 50, 10],
                        bounds=[1.0, 2.0],
                        total=600.0,
                        observation_count=160,
                    )
                ]
            )
        )
    )
    assert _sample("mahavishnu_akosha_duration_count") == 160.0

    # The producer restarts: its cumulative counts are back near zero.
    await receiver._handle_metrics_batch(
        _request(
            _resource(
                metrics=[
                    _histogram(
                        "akosha.duration",
                        counts=[2, 1, 0],
                        bounds=[1.0, 2.0],
                        total=3.0,
                        observation_count=3,
                        timestamp=TS + 1,
                    )
                ]
            )
        )
    )
    # The first interval after the restart is NOT lost.
    assert _sample("mahavishnu_akosha_duration_count") == 163.0
    assert _sample('mahavishnu_akosha_duration_bucket{le="1.0"}') == 102.0

    await receiver._handle_metrics_batch(
        _request(
            _resource(
                metrics=[
                    _histogram(
                        "akosha.duration",
                        counts=[5, 3, 0],
                        bounds=[1.0, 2.0],
                        total=8.0,
                        observation_count=8,
                        timestamp=TS + 2,
                    )
                ]
            )
        )
    )
    assert _sample("mahavishnu_akosha_duration_count") == 168.0


async def test_the_counter_and_the_histogram_agree_about_a_restart(
    receiver: OTLPReceiver,
) -> None:
    """One rule, two paths: a backwards value is a re-baseline.

    The counter went to 2 after a restart and exposed 102 — the old value
    plus the whole new one. The histogram has to do the same thing, or
    the two halves of the same export describe different worlds.
    """
    await receiver._handle_metrics_batch(
        _request(_resource(metrics=[_sum("akosha.requests", 100)]))
    )
    await receiver._handle_metrics_batch(
        _request(_resource(metrics=[_sum("akosha.requests", 2, timestamp=TS + 1)]))
    )
    assert _sample("mahavishnu_akosha_requests_total") == 102.0


# ── the label key set ────────────────────────────────────────────────────


async def test_a_changed_attribute_key_set_creates_a_sibling_family(
    receiver: OTLPReceiver,
) -> None:
    """A key-set change must not freeze the metric.

    The OTel SDK emits ONE ``Metric`` whose data points carry different
    attribute key sets, so this is routine rather than exotic. The
    family's label set used to be fixed by the FIRST point ever seen for
    that metric name, and every later point with a different key set was
    dropped — permanently, leaving the metric stuck at a stale value on
    ``/metrics`` for ever.

    The fix keys the family by the metric name AND the attribute key-set
    signature, so a change creates a SIBLING family: both are exposed,
    and the original keeps updating.

    The metric name is unique to this test on purpose: the registry is a
    process-wide singleton that survives between tests (only its VALUES are
    reset), so a name another test already registered would make the
    sibling-naming depend on test order rather than on the producer.
    """
    first = _sum(
        "akosha.keyset.change",
        10,
        attributes=[common_pb2.KeyValue(key="a", value=_sv("1"))],
    )
    second = _sum(
        "akosha.keyset.change",
        20,
        attributes=[
            common_pb2.KeyValue(key="a", value=_sv("1")),
            common_pb2.KeyValue(key="b", value=_sv("2")),
        ],
    )
    third = _sum(
        "akosha.keyset.change",
        30,
        attributes=[common_pb2.KeyValue(key="a", value=_sv("1"))],
    )
    await receiver._handle_metrics_batch(_request(_resource(metrics=[first])))
    await receiver._handle_metrics_batch(_request(_resource(metrics=[second])))
    await receiver._handle_metrics_batch(_request(_resource(metrics=[third])))

    text = _registry_text()
    # The original family is NOT frozen: it moved on to 30.
    assert 'mahavishnu_akosha_keyset_change_total{a="1"} 30.0' in text
    # And the sibling family carries its own point. The signature is
    # appended to the INSTRUMENT name, so the rendered sample is
    # ``<name>__<signature>_total`` — prometheus_client adds the ``_total``
    # suffix itself.
    assert "mahavishnu_akosha_keyset_change__" in text
    assert 'a="1",b="2"} 20.0' in text
    # Nothing was dropped: both key sets are representable.
    assert receiver._prometheus_bridge.dropped_by_reason == {}


# ── the replay ────────────────────────────────────────────────────────────


async def test_a_replayed_delta_point_is_not_counted_twice(
    receiver: OTLPReceiver,
) -> None:
    """The substrate and the bridge must agree about what a replay is.

    The substrate treats a replay as a no-op — the point's identity
    (series plus instant) is already stored — while the bridge re-applied
    the value, so one delta point of 5 exported twice read 10.0 on
    ``/metrics`` while the truth was 5. The two halves of the same
    handler disagreed, and the exposed number was the wrong one.
    """
    request = _request(_resource(metrics=[_delta_sum("akosha.requests", 5)]))
    await receiver._handle_metrics_batch(request)
    await receiver._handle_metrics_batch(request)

    assert _sample("mahavishnu_akosha_requests_total") == 5.0
    # The substrate holds exactly one row for the two exports.
    assert len(_stored(receiver, "SELECT 1 FROM metrics_substrate")) == 1


# ── a shape-dependent failure is not permanent ────────────────────────────


async def test_a_bounds_less_histogram_does_not_poison_the_metric_name(
    receiver: OTLPReceiver,
) -> None:
    """A histogram with no explicit bounds is legal OTLP — it is a
    single-bucket histogram — and it must not poison the metric name.

    ``_create_family`` returns "unsupported" for shape-dependent reasons,
    and the failure used to be cached BY NAME, so one bounds-less
    histogram meant every later export of that metric — including a
    perfectly valid histogram with proper bounds — was dropped for ever.

    Only a PERMANENT failure (an instrument conflict) belongs in that
    cache.
    """
    bounds_less = metrics_pb2.Metric(
        name="akosha.duration",
        histogram=metrics_pb2.Histogram(
            data_points=[
                metrics_pb2.HistogramDataPoint(
                    time_unix_nano=TS,
                    count=1,
                    sum=1.0,
                    bucket_counts=[1],
                    explicit_bounds=[],
                )
            ],
            aggregation_temporality=(
                metrics_pb2.AggregationTemporality.AGGREGATION_TEMPORALITY_CUMULATIVE
            ),
        ),
    )
    await receiver._handle_metrics_batch(_request(_resource(metrics=[bounds_less])))
    assert receiver._prometheus_bridge.dropped_by_reason == {"unsupported": 1}

    # The producer's next export carries a properly bounded histogram.
    await receiver._handle_metrics_batch(
        _request(
            _resource(
                metrics=[
                    _histogram(
                        "akosha.duration",
                        counts=[1, 1, 1],
                        bounds=[1.0, 2.0],
                        total=6.0,
                        observation_count=3,
                        timestamp=TS + 1,
                    )
                ]
            )
        )
    )
    assert 'mahavishnu_akosha_duration_bucket{le="1.0"} 1.0' in _registry_text()


# ── the namespace rule ────────────────────────────────────────────────────


async def test_a_metric_outside_the_span_allowlist_is_accepted(
    receiver: OTLPReceiver,
) -> None:
    """The metric path deliberately does NOT apply the span path's
    namespace allowlist.

    oneiric's ``OTLPObservabilityAdapter`` declares NO
    ``service.namespace`` at all, so requiring one — or allowlisting one
    — would quarantine every metric oneiric sends, which is the whole of
    REQ-M07's second producer. The export would answer "accepted, zero
    points stored" and nothing would say why.

    The divergence is DELIBERATE, so it is pinned here: the span path
    rejects this namespace, and the metric path accepts and persists it.
    A later change that re-adds the allowlist to the metric path would
    break REQ-M07 silently — this test is what makes that change loud.
    """
    from akosha.observability.resource_validator import ResourceAttributeValidator

    rogue = "totally-not-on-the-allowlist"
    # The span path's rule, spelled out: this namespace is drift.
    verdict = ResourceAttributeValidator().validate({"service_namespace": rogue})
    assert not verdict.accepted

    # The metric path's rule: no allowlist, no required namespace.
    result = await receiver._handle_metrics_batch(
        _request(_resource(service_namespace=rogue, metrics=[_sum("akosha.requests", 1)]))
    )
    assert result["rejected"] == 0
    assert result["inserted"] == 1
    stored = _stored(receiver, "SELECT service_namespace FROM metrics_substrate")
    assert stored == [(rogue,)]


# ── the cardinality drop counter ──────────────────────────────────────────


async def test_the_cardinality_drop_counter_increments(
    receiver: OTLPReceiver,
) -> None:
    """A dropped point is COUNTED, not just dropped.

    The cap is only useful if hitting it is visible: an operator who sees
    a metric's series stop growing needs to know the points are being
    dropped, and where they went.
    """
    for index in range(105):
        await receiver._handle_metrics_batch(
            _request(
                _resource(
                    metrics=[
                        _sum(
                            "akosha.cardinality.probe",
                            1,
                            attributes=[
                                common_pb2.KeyValue(key="request_id", value=_sv(str(index)))
                            ],
                        )
                    ]
                )
            )
        )
    assert _sample('akosha_otlp_metrics_exposure_dropped_total{reason="cardinality"}') == 5.0
    # The points are still in the substrate: only the EXPOSURE was capped.
    assert _stored(receiver, "SELECT count(*) FROM metrics_substrate")[0][0] == 105


async def test_the_family_and_resource_caps_bound_the_registry() -> None:
    """Nothing bounded the number of metric FAMILIES, or ``target_info``'s children.

    ``MAX_SERIES_PER_METRIC`` bounds the label-value combinations WITHIN a
    family, but one instrument is registered per distinct metric name and
    nothing bounded how many names a producer could mint — a producer that
    named a metric per request would grow the shared registry without
    limit, and the registry is rendered on every scrape.

    ``target_info`` is keyed by the producer's resource, so it is as
    unbounded as the number of resource instances; ``otel_scope_info`` is
    keyed by the instrumentation scope.

    Every cap DROPS the point from the exposure and COUNTS it, so hitting
    one is visible rather than silent. The point is still persisted in the
    substrate — only the exposure is capped.
    """
    from akosha.observability.otlp_prometheus_bridge import OTLPPrometheusBridge

    bridge = OTLPPrometheusBridge(max_metric_families=3, max_target_info_series=2)
    for index in range(6):
        bridge.export(
            {
                "metric_name": f"akosha.cap.probe.{index}",
                "service_name": "mahavishnu",
                "data_type": "sum",
                "is_monotonic": True,
                "value_sum": 1.0,
                "temporality": "cumulative",
                "attributes": {},
                "resource": {"service.instance.id": f"instance-{index}"},
            }
        )
    assert len(bridge._families) == 3
    assert bridge.dropped_by_reason.get("family_cap") == 3
    assert bridge.dropped_by_reason.get("target_info_cap") == 1
