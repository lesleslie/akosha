from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from akosha.mcp.server import APP_NAME, APP_VERSION, create_app
from tests._support.dummy_fastmcp import DummyFastMCP, make_fastmcp_factory


@pytest.fixture
def fastmcp_factory(monkeypatch: pytest.MonkeyPatch) -> DummyFastMCP:
    return make_fastmcp_factory(monkeypatch, APP_NAME, APP_VERSION)


@pytest.fixture
def patched_lifespan(monkeypatch: pytest.MonkeyPatch):
    embedding_service = MagicMock()
    embedding_service.initialize = AsyncMock()
    embedding_service.is_available.return_value = True

    analytics_service = MagicMock(name="analytics")
    # The lifespan does ``await analytics_service.initialize()``. A bare
    # MagicMock returns a MagicMock from that call, which is not awaitable --
    # TypeError at akosha/mcp/server.py:510. Mirrors the embedding_service and
    # hot_store mocks below, which set .initialize to AsyncMock.
    analytics_service.initialize = AsyncMock()
    graph_builder = MagicMock(name="graph_builder")
    hot_store = MagicMock()
    hot_store.initialize = AsyncMock()
    # The default health probe in akosha.mcp.server awaits ``hot_store.ping()``
    # when present. Mark it async so the probe treats it as healthy.
    hot_store.ping = AsyncMock()

    cache_client = object()
    cold_storage = object()
    telemetry = (object(), object())

    monkeypatch.setattr("akosha.mcp.auth.validate_auth_config", lambda: True)
    monkeypatch.setattr(
        "akosha.observability.setup_telemetry",
        lambda **kwargs: telemetry,
    )
    shutdown_telemetry = MagicMock()
    monkeypatch.setattr("akosha.observability.shutdown_telemetry", shutdown_telemetry)
    monkeypatch.setattr(
        "akosha.processing.embeddings.get_embedding_service",
        lambda: embedding_service,
    )
    monkeypatch.setattr(
        "akosha.processing.analytics.TimeSeriesAnalytics", lambda: analytics_service
    )
    monkeypatch.setattr(
        "akosha.processing.knowledge_graph.KnowledgeGraphBuilder",
        lambda: graph_builder,
    )
    monkeypatch.setattr("akosha.storage.hot_store.HotStore", lambda database_path: hot_store)
    monkeypatch.setattr("akosha.storage.create_hot_store", lambda: hot_store)

    # v7 PR-A3: substitute the OTLP receiver, for the same reason the
    # CodeGraphIngester is skipped below — this module asserts the lifespan's
    # health-aggregator wiring, and a real receiver would open two network
    # listeners as a side effect of assertions that have nothing to do with
    # telemetry ingest. No socket is created here. Receiver behaviour is
    # covered in tests/unit/observability/test_otlp_receiver.py.
    otel_receiver = MagicMock(name="otel_receiver")
    otel_receiver.start = AsyncMock()
    otel_receiver.stop = AsyncMock()
    otel_receiver.run_canary_emitter = AsyncMock()
    otel_receiver.run_retention_sweep = AsyncMock()
    monkeypatch.setattr("akosha.mcp.server.OTLPReceiver", lambda *a, **k: otel_receiver)

    # Wave 5: skip the CodeGraphIngester and kg_refresh task in tests that
    # aren't designed to exercise them. The ingester would otherwise start
    # a real httpx client that attempts to reach ``SESSION_BUDDY_MCP_URL``,
    # which is not mocked in this fixture and would either hang or
    # produce noisy connection-refused warnings in stdout. The dedicated
    # Wave 5 wiring tests in ``test_wave5_lifespan_wiring.py`` exercise
    # the ingester with a fake implementation.
    monkeypatch.setenv("AKOSHA_SKIP_CODE_GRAPH_INGESTER", "1")
    monkeypatch.setenv("AKOSHA_SKIP_KG_REFRESH", "1")
    # The OtelTraceIngester poller needs no skip env var. PR-A8 retired
    # AKOSHA_SKIP_OTEL_INGESTER -- it is read nowhere -- and the poller is
    # gated by ``otel.receiver.trace_ingester_poller``, which ships false.
    # The tests that DO exercise the poller flip the flag
    # (test_wave5_lifespan_wiring.py).

    register_all_tools = MagicMock()
    monkeypatch.setattr("akosha.mcp.tools.register_all_tools", register_all_tools)

    # The W1.3 refactor moved tool registration from the legacy
    # ``register_all_tools`` call site to ``_apply_tool_profile`` (called
    # from inside ``akosha.mcp.server``'s lifespan). Mock the new entry
    # point at its import source so the lifespan doesn't try to call
    # ``mcp.tool()`` on the DummyFastMCP test fixture.
    apply_tool_profile = AsyncMock()
    monkeypatch.setattr("mcp_common.tools.dispatch._apply_tool_profile", apply_tool_profile)

    return {
        "embedding_service": embedding_service,
        "analytics_service": analytics_service,
        "graph_builder": graph_builder,
        "hot_store": hot_store,
        "telemetry": telemetry,
        "shutdown_telemetry": shutdown_telemetry,
        "register_all_tools": register_all_tools,
        "apply_tool_profile": apply_tool_profile,
        "cache_client": cache_client,
        "cold_storage": cold_storage,
    }


@pytest.mark.asyncio
async def test_create_app_standard_mode_lifespan(
    fastmcp_factory: DummyFastMCP, patched_lifespan, monkeypatch
):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("OTLP_ENDPOINT", "http://otel:4317")
    monkeypatch.setattr(
        "akosha.observability.prometheus_metrics.generate_metrics", lambda: "metrics"
    )

    app = create_app()
    assert app is fastmcp_factory
    assert "/health" in app.routes
    assert "/healthz" in app.routes
    assert "/metrics" in app.routes

    route = app.routes["/metrics"]["handler"]
    response = await route(None)
    assert response.media_type == "text/plain; version=0.0.4; charset=utf-8"
    assert response.body == b"metrics"

    health_response = await app.routes["/health"]["handler"](None)
    healthz_response = await app.routes["/healthz"]["handler"](None)
    # Pre-lifespan: no probe registered → /health must be 503 with explicit
    # reason (fail-loud default per mcp-backend-wiring-discipline). This is
    # exactly the audit C2 failure mode the discipline was written to catch.
    pre_health_body = json.loads(health_response.body)
    assert pre_health_body["status"] == "degraded"
    assert pre_health_body["service"] == APP_NAME
    assert pre_health_body["version"] == APP_VERSION
    assert pre_health_body["checks"]["probe"]["ok"] is False
    assert "no health probe registered" in pre_health_body["checks"]["probe"]["error"]
    # /healthz is process-liveness only and always returns 200.
    assert json.loads(healthz_response.body) == {"status": "ok"}

    lifespan = app._mcp_server.lifespan
    async with lifespan(app) as context:
        assert context["akosha_ready"] is True
        assert context["mode"] == "standard"
        assert context["embedding_service"] is patched_lifespan["embedding_service"]
        assert context["analytics_service"] is patched_lifespan["analytics_service"]
        assert context["cache_client"] is None
        assert context["cold_storage"] is None

        # Post-lifespan: the default probe is registered. /health now reports
        # the real feed state — embedding_service.is_available() is True
        # because the lifespan initialized it.
        post_health_response = await app.routes["/health"]["handler"](None)
        post_health_body = json.loads(post_health_response.body)
        # Phase 4: the body ``status`` reflects the aggregator's worst
        # verdict across the data feeds. This test fixture sets
        # ``AKOSHA_SKIP_*=1`` so the producers don't start, but the
        # module-level ``_code_graph_ingester`` / ``_kg_refresh_task``
        # may still hold values from a previous test that ran in the
        # same session. The aggregator's verdict is therefore either
        # ``degraded`` (cycles=0 + ingester alive → HNSW hardening) or
        # ``failed`` (no ingester alive). Both are correct
        # non-``healthy`` states; the HTTP code is 503 either way.
        # Phase 4 surfaces both via ``feed_never_populated`` so the
        # operator sees a producer that hasn't started OR a
        # broken-before-first-success producer. Pre-Phase-4 the same
        # probe reported ``ok=True`` for both states, masking them.
        aggregate = post_health_body["checks"]["_aggregate"]
        assert aggregate["status"] in {"degraded", "failed"}
        assert post_health_body["status"] == aggregate["status"]
        assert post_health_response.status_code == 503
        assert "hot_store" in post_health_body["checks"]
        assert "embeddings" in post_health_body["checks"]
        assert post_health_body["checks"]["embeddings"]["ok"] is True
        # ``feed_never_populated`` is the canonical signal for both
        # never-started and never-cycled-alive.
        assert "feed_never_populated" in aggregate["reason_codes"]

    patched_lifespan["embedding_service"].initialize.assert_awaited_once()
    patched_lifespan["hot_store"].initialize.assert_awaited_once()
    patched_lifespan["apply_tool_profile"].assert_awaited_once()
    patched_lifespan["shutdown_telemetry"].assert_called_once()


class LiteMode:
    requires_external_services = False

    async def initialize_cache(self):
        return "cache-client"

    async def initialize_cold_storage(self):
        return "cold-storage"


@pytest.mark.asyncio
async def test_create_app_lite_mode_and_auth_failure(
    fastmcp_factory: DummyFastMCP,
    patched_lifespan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setattr(
        "akosha.observability.prometheus_metrics.generate_metrics", lambda: "metrics"
    )

    app = create_app(mode=LiteMode())
    async with app._mcp_server.lifespan(app) as context:
        assert context["mode"] == "lite"
        assert context["analytics_service"] is None
        assert context["cache_client"] == "cache-client"
        assert context["cold_storage"] == "cold-storage"

    monkeypatch.setattr(
        "akosha.mcp.auth.validate_auth_config",
        lambda: (_ for _ in ()).throw(ValueError("bad auth config")),
    )

    with pytest.raises(RuntimeError, match="Authentication configuration failed"):
        async with app._mcp_server.lifespan(app):
            pass
