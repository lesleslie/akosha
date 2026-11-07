"""The metric path's ``/health`` feed.

The metric path had no feed of its own, and that was the gap this closes:
a metrics receiver that accepts exports, exposes them on ``/metrics`` and
stores NOTHING left ``/health`` reporting green. The operator saw a live
series on the Prometheus route and a healthy health check, and the durable
substrate was empty.

Three states are pinned here, and they are three different answers:

* **WARMING UP** — the receiver is up and has not been exported to yet.
  Not a 503: the launchd wrapper polls ``/health`` with ``curl -fsS``, and
  ``-f`` fails on any 4xx/5xx, so a feed that flapped to degraded on every
  cold start would make the wrapper kill the server it just started.
* **HEALTHY** — points are landing in the substrate.
* **DEGRADED** — the substrate is unusable. A 503, which is the point:
  the failure this plan exists to fix has to be visible on the health
  surface, not only on ``/metrics``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

import pytest
from mcp_common.health.feed import is_healthy
from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.metrics.v1 import metrics_pb2
from opentelemetry.proto.resource.v1 import resource_pb2

from akosha.mcp.server import _metrics_feed_state
from akosha.observability.otlp_receiver import OTLPReceiver
from akosha.storage.hot_store import HotStore

# The lifespan fixtures are IMPORTED rather than redeclared: they are the
# ones ``tests/unit/test_mcp_server_lifespan.py`` uses, and pytest resolves
# an imported fixture like any other. Redeclaring them here would be a
# second copy of a 90-line fixture that has to track the real lifespan.
from tests.unit.test_mcp_server_lifespan import (  # noqa: F401
    fastmcp_factory,
    patched_lifespan,
)

if TYPE_CHECKING:
    from collections.abc import Generator

TS: int = 1_700_000_000_000_000_000

AnyValue = common_pb2.AnyValue
AggregationTemporality = metrics_pb2.AggregationTemporality
InstrumentationScope = common_pb2.InstrumentationScope
KeyValue = common_pb2.KeyValue
Metric = metrics_pb2.Metric
NumberDataPoint = metrics_pb2.NumberDataPoint
Resource = resource_pb2.Resource
ResourceMetrics = metrics_pb2.ResourceMetrics
ScopeMetrics = metrics_pb2.ScopeMetrics
Sum = metrics_pb2.Sum


@pytest.fixture
async def store() -> HotStore:
    hs = HotStore(database_path=":memory:", embedding_dim=384)
    await hs.initialize()
    return hs


@pytest.fixture
async def started_receiver(store: HotStore, monkeypatch: pytest.MonkeyPatch) -> OTLPReceiver:
    """A started receiver. The binds are stubbed — the sockets are not the point."""
    receiver = OTLPReceiver(store, grpc_port=0, http_port=0)
    monkeypatch.setattr(receiver, "_start_grpc", AsyncMock())
    monkeypatch.setattr(receiver, "_start_http", AsyncMock())
    await receiver.start()
    return receiver


def _sum_point(metric_name: str, value: int) -> dict[str, Any]:
    """A substrate row shaped the way the receiver's mapping produces one."""
    return {
        "ts": TS,
        "service_name": "mahavishnu",
        "service_namespace": "bodai",
        "scope_name": "akosha.test",
        "metric_name": metric_name,
        "data_type": "sum",
        "unit": None,
        "description": None,
        "value_sum": float(value),
        "value_count": 1,
        "value_min": float(value),
        "value_max": float(value),
        "value_sum_of_squares": None,
        "is_monotonic": True,
        "buckets_json": None,
        "attributes": {},
        "resource": {},
        "temporality": "cumulative",
        "scope_version": "1.0",
    }


def _export_request() -> metrics_service_pb2.ExportMetricsServiceRequest:
    """A real export carrying one metric point."""
    return metrics_service_pb2.ExportMetricsServiceRequest(
        resource_metrics=[
            ResourceMetrics(
                resource=Resource(
                    attributes=[
                        KeyValue(
                            key="service.name",
                            value=AnyValue(string_value="mahavishnu"),
                        )
                    ]
                ),
                scope_metrics=[
                    ScopeMetrics(
                        scope=InstrumentationScope(name="akosha.test"),
                        metrics=[
                            Metric(
                                name="akosha.requests",
                                sum=Sum(
                                    data_points=[NumberDataPoint(time_unix_nano=TS, as_int=7)],
                                    is_monotonic=True,
                                    aggregation_temporality=(
                                        AggregationTemporality.AGGREGATION_TEMPORALITY_CUMULATIVE
                                    ),
                                ),
                            )
                        ],
                    )
                ],
            )
        ]
    )


async def test_a_fresh_receiver_is_warming_up_not_degraded(
    started_receiver: OTLPReceiver,
    store: HotStore,
) -> None:
    """A receiver that has simply not been exported to yet is WARMING UP.

    The aggregator maps "alive but never cycled" to DEGRADED, which is a
    503. The receiver counts its startup as its first cycle precisely so
    the metric path reaches the aggregator with ``cycles_total >= 1`` and
    lands in WARMING UP instead.
    """
    state = await _metrics_feed_state(started_receiver, store)
    assert state.cycles_total >= 1
    assert state.entities_count == 0
    healthy, status, _codes = is_healthy(state)
    assert not healthy
    assert status.value == "warming_up", status.value


async def test_a_healthy_metric_path_is_healthy(
    started_receiver: OTLPReceiver,
    store: HotStore,
) -> None:
    """Points in the substrate make the feed healthy."""
    await store.insert_metrics_substrate([_sum_point("akosha.requests", 7)])
    state = await _metrics_feed_state(started_receiver, store)
    assert state.entities_count == 1
    assert state.errors_total == 0
    healthy, status, _codes = is_healthy(state)
    assert healthy
    assert status.value == "healthy", status.value


async def test_a_broken_substrate_degrades_the_feed(
    started_receiver: OTLPReceiver,
    store: HotStore,
) -> None:
    """An unusable substrate is a 503, not a green health check.

    The substrate is the ground truth: the Prometheus exposure is a
    SEPARATE path that keeps working when the durable write does not, so
    a live series on ``/metrics`` says nothing about whether anything is
    being stored. The feed is what closes that gap.
    """
    conn = store.conn
    assert conn is not None
    conn.execute("DROP TABLE IF EXISTS metrics_substrate")

    # An export arrives and cannot be persisted. The receiver re-probes
    # and learns that the substrate is gone.
    await started_receiver._handle_metrics_batch(_export_request())
    assert started_receiver.metrics_substrate_ready is False

    state = await _metrics_feed_state(started_receiver, store)
    assert state.last_error_at is not None
    assert state.errors_total >= 1
    healthy, status, codes = is_healthy(state)
    assert not healthy
    assert status.value == "degraded", status.value
    assert "error_within_halflife" in [code.value for code in codes]


@pytest.fixture(autouse=True)
def _reset_probe_state() -> Generator[None]:
    """Keep the shared registry's counters from leaking between tests."""
    from akosha.observability.prometheus_metrics import reset_all_metrics

    reset_all_metrics()
    yield
    reset_all_metrics()


@pytest.mark.asyncio
@pytest.mark.usefixtures("fastmcp_factory", "patched_lifespan")
async def test_the_health_payload_carries_the_metrics_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The feed is WIRED into ``/health``, not merely implemented.

    The feed state is built by ``_metrics_feed_state``; this asserts the
    aggregator receives it and the route's per-feed dict carries the four
    canonical fields every feed owes its consumers. Without the wiring the
    feed would be a well-behaved object nobody reads.
    """
    import json

    from akosha.mcp.server import create_app

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setattr(
        "akosha.observability.prometheus_metrics.generate_metrics", lambda: "metrics"
    )
    app = cast("Any", create_app())
    lifespan = app._mcp_server.lifespan
    async with lifespan(app):
        response = await app.routes["/health"]["handler"](None)
        body = json.loads(response.body)
        assert "metrics_feed" in body["checks"]
        feed = body["checks"]["metrics_feed"]
        for field in (
            "entities_count",
            "last_updated_timestamp",
            "cycles_total",
            "errors_total",
        ):
            assert field in feed
        assert feed["entities_count"] == 0
        assert feed["status"] in {"healthy", "warming_up", "degraded", "failed"}
