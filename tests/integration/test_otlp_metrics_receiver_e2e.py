"""End-to-end: a real OTLP MetricsService export over a real gRPC listener.

The unit suite calls ``_MetricsServiceServicer.Export`` directly, which
proves the mapping but not the WIRE. The wire is where the plan's
acceptance criterion lives: "an OTLP metrics export to :4317 is accepted,
appears in ``curl http://localhost:8682/metrics``, and is queryable
through Prometheus."

So this module binds a REAL gRPC server on a free port, sends a REAL
``ExportMetricsServiceRequest`` over it, and checks all three claims:

* the export is accepted, and ``partial_success`` says what it has to say;
* the series appears on the shared registry the ``/metrics`` route renders;
* the point is durable in ``metrics_substrate``.

A second test drives the same path with the OpenTelemetry SDK's own gRPC
exporter, so the protobuf is produced by the SDK rather than by this
repo — which is the producer that actually has to work.

Two harness details, both of which are traps:

* A SYNCHRONOUS grpc client inside ``asyncio.run()`` deadlocks the aio
  server it is talking to — the client blocks the event loop the server
  needs to answer on. The client here is ``grpc.aio``.
* The SDK's OTLP gRPC exporter uses a SYNCHRONOUS channel, so
  ``force_flush`` blocks. It is moved off the event loop with
  ``asyncio.to_thread``. In production the producer is a separate process,
  so this only affects the harness.
"""

from __future__ import annotations

import asyncio
import socket
import time
from typing import TYPE_CHECKING

import pytest
from opentelemetry.proto.collector.metrics.v1 import (
    metrics_service_pb2,
    metrics_service_pb2_grpc,
)
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.metrics.v1 import metrics_pb2
from opentelemetry.proto.resource.v1 import resource_pb2
from prometheus_client import generate_latest

from akosha.observability.otlp_receiver import OTLPReceiver
from akosha.observability.prometheus_metrics import (
    get_metrics_registry,
    reset_all_metrics,
)
from akosha.storage.hot_store import HotStore

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator

pytestmark = [pytest.mark.integration]


def _free_port() -> int:
    """An ephemeral port, released before the receiver binds it."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture(autouse=True)
def _reset_shared_registry() -> Generator[None]:
    """Zero the shared registry around every test in this module.

    The registry is a process-wide singleton and the receiver exposes into
    it, so a counter left at 7 by one test would make the next test's
    assertion a lie.
    """
    reset_all_metrics()
    yield
    reset_all_metrics()


@pytest.fixture
async def receiver() -> AsyncGenerator[OTLPReceiver]:
    """A receiver bound on a REAL gRPC port, with a real substrate."""
    store = HotStore(database_path=":memory:", embedding_dim=384)
    await store.initialize()
    rx = OTLPReceiver(store, grpc_port=_free_port(), http_port=0)
    await rx.start()
    try:
        yield rx
    finally:
        await rx.stop()
        await store.close()


def _kv(key: str, value: str) -> common_pb2.KeyValue:
    return common_pb2.KeyValue(key=key, value=common_pb2.AnyValue(string_value=value))


def _export_request(now: int) -> metrics_service_pb2.ExportMetricsServiceRequest:
    """A request carrying two producers' metrics — REQ-M07's two exporters.

    The metric names carry an ``e2e.`` infix on purpose. The Prometheus
    registry is a process-wide singleton that survives between test files
    (only its VALUES are reset), and one instrument name carries exactly
    one attribute key set — so a metric name another test already
    registered with a DIFFERENT key set is exposed under a sibling name
    (``<name>__<signature>``). A name unique to this file keeps the
    assertions below about the WIRE rather than about the registry's
    history.
    """
    return metrics_service_pb2.ExportMetricsServiceRequest(
        resource_metrics=[
            metrics_pb2.ResourceMetrics(
                resource=resource_pb2.Resource(
                    attributes=[
                        _kv("service.name", "mahavishnu"),
                        _kv("deployment.environment", "local"),
                    ]
                ),
                scope_metrics=[
                    metrics_pb2.ScopeMetrics(
                        scope=common_pb2.InstrumentationScope(name="mahavishnu.core"),
                        metrics=[
                            metrics_pb2.Metric(
                                name="mahavishnu.e2e.workflows.executed",
                                unit="1",
                                description="workflows executed",
                                sum=metrics_pb2.Sum(
                                    data_points=[
                                        metrics_pb2.NumberDataPoint(
                                            time_unix_nano=now,
                                            as_int=7,
                                        )
                                    ],
                                    is_monotonic=True,
                                    aggregation_temporality=(
                                        metrics_pb2.AggregationTemporality.AGGREGATION_TEMPORALITY_CUMULATIVE
                                    ),
                                ),
                            ),
                            metrics_pb2.Metric(
                                name="mahavishnu.e2e.observability.drift_detected",
                                unit="1",
                                description="drift detected",
                                gauge=metrics_pb2.Gauge(
                                    data_points=[
                                        metrics_pb2.NumberDataPoint(
                                            time_unix_nano=now,
                                            as_double=3.5,
                                            attributes=[_kv("kind", "volume")],
                                        )
                                    ]
                                ),
                            ),
                        ],
                    )
                ],
            ),
            # A SECOND producer on the SAME port. Indistinguishable on the
            # wire except for its resource attributes — REQ-M07.
            metrics_pb2.ResourceMetrics(
                resource=resource_pb2.Resource(attributes=[_kv("service.name", "oneiric")]),
                scope_metrics=[
                    metrics_pb2.ScopeMetrics(
                        scope=common_pb2.InstrumentationScope(name="oneiric.adapters.vector"),
                        metrics=[
                            metrics_pb2.Metric(
                                name="oneiric.adapters.vector.rows",
                                unit="1",
                                sum=metrics_pb2.Sum(
                                    data_points=[
                                        metrics_pb2.NumberDataPoint(time_unix_nano=now, as_int=11)
                                    ],
                                    is_monotonic=True,
                                    aggregation_temporality=(
                                        metrics_pb2.AggregationTemporality.AGGREGATION_TEMPORALITY_CUMULATIVE
                                    ),
                                ),
                            )
                        ],
                    )
                ],
            ),
        ]
    )


def _registry_text() -> str:
    return generate_latest(get_metrics_registry()).decode("utf-8")


async def test_a_real_export_over_the_wire_is_accepted_and_exposed(
    receiver: OTLPReceiver,
) -> None:
    """The plan's acceptance criterion, over the wire.

    A receiver that answers ``UNIMPLEMENTED`` for the metric method is the
    failure this whole change exists to fix, and only a real RPC can show
    that it does not.
    """
    import grpc

    channel = grpc.aio.insecure_channel(f"127.0.0.1:{receiver._grpc_port}")
    stub = metrics_service_pb2_grpc.MetricsServiceStub(channel)
    try:
        request = _export_request(time.time_ns())
        response = await stub.Export(request, timeout=15)

        # A fully successful export carries NO partial_success.
        assert not response.HasField("partial_success")

        # The registry renders them — the /metrics route's content.
        rendered = _registry_text()
        assert "mahavishnu_e2e_workflows_executed_total" in rendered
        assert "mahavishnu_e2e_observability_drift_detected" in rendered
        # The second producer's series is namespaced, not collided. Its
        # metric name already begins with its service name, so the prefix
        # is skipped rather than doubled.
        assert "oneiric_adapters_vector_rows_total" in rendered
        assert "mahavishnu_mahavishnu" not in rendered

        # And the points are durable.
        conn = receiver._hot_store.conn
        assert conn is not None
        rows = conn.execute(
            "SELECT service_name, metric_name FROM metrics_substrate ORDER BY service_name"
        ).fetchall()
        assert len(rows) == 3

        # A replay of the same export dedupes: one row per point.
        await stub.Export(request, timeout=15)
        count_row = conn.execute("SELECT count(*) FROM metrics_substrate").fetchone()
        assert count_row is not None
        assert count_row[0] == 3
    finally:
        await channel.close()


async def test_a_real_otel_sdk_export_reaches_the_receiver(
    receiver: OTLPReceiver,
) -> None:
    """The OpenTelemetry SDK's own exporter, not this repo's protobuf.

    The SDK builds the request, chooses the temporality, and serialises the
    attributes — the parts a hand-built request cannot vouch for.
    """
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource

    exporter = OTLPMetricExporter(endpoint=f"127.0.0.1:{receiver._grpc_port}", insecure=True)
    provider = MeterProvider(
        metric_readers=[PeriodicExportingMetricReader(exporter, export_interval_millis=60000)],
        resource=Resource.create({"service.name": "e2e.producer"}),
    )
    meter = provider.get_meter("e2e.producer")
    counter = meter.create_counter("sdk.counter.total")
    counter.add(5)

    # The SDK's OTLP gRPC exporter uses a SYNCHRONOUS channel; forcing the
    # flush on the event-loop thread blocks the very loop the receiver's
    # aio server must answer on. Off the loop it goes.
    await asyncio.to_thread(provider.force_flush, timeout_millis=15000)
    try:
        rendered = _registry_text()
        assert "e2e_producer_sdk_counter_total" in rendered
        assert receiver.metrics_received_total >= 1
    finally:
        await asyncio.to_thread(provider.shutdown)


async def test_a_wire_export_with_an_invalid_point_reports_it(
    receiver: OTLPReceiver,
) -> None:
    """The producer sees the rejection on the wire, not just in a log.

    A point with no ``service.name`` cannot be attributed to a series. The
    export still succeeds — the other point in it is good — and the
    producer is told that one point was rejected.
    """
    import grpc

    channel = grpc.aio.insecure_channel(f"127.0.0.1:{receiver._grpc_port}")
    stub = metrics_service_pb2_grpc.MetricsServiceStub(channel)
    try:
        now = time.time_ns()
        request = metrics_service_pb2.ExportMetricsServiceRequest(
            resource_metrics=[
                metrics_pb2.ResourceMetrics(
                    resource=resource_pb2.Resource(attributes=[]),
                    scope_metrics=[
                        metrics_pb2.ScopeMetrics(
                            scope=common_pb2.InstrumentationScope(name="e2e.producer"),
                            metrics=[
                                metrics_pb2.Metric(
                                    name="e2e.unnamed",
                                    sum=metrics_pb2.Sum(
                                        data_points=[
                                            metrics_pb2.NumberDataPoint(
                                                time_unix_nano=now, as_int=1
                                            )
                                        ],
                                        is_monotonic=True,
                                        aggregation_temporality=(
                                            metrics_pb2.AggregationTemporality.AGGREGATION_TEMPORALITY_CUMULATIVE
                                        ),
                                    ),
                                )
                            ],
                        )
                    ],
                )
            ]
        )
        response = await stub.Export(request, timeout=15)
        assert response.HasField("partial_success")
        assert response.partial_success.rejected_data_points == 1
        assert response.partial_success.error_message != ""
    finally:
        await channel.close()
