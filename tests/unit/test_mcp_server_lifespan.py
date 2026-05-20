from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from akosha.mcp.server import APP_NAME, APP_VERSION, create_app


class DummyFastMCP:
    def __init__(self, name: str, version: str) -> None:
        self.name = name
        self.version = version
        self._mcp_server = SimpleNamespace()
        self.routes: dict[str, object] = {}

    def custom_route(self, path: str, methods: list[str]):
        def decorator(func):
            self.routes[path] = {"methods": methods, "handler": func}
            return func

        return decorator

    def http_app(self) -> str:
        return "http-app"


@pytest.fixture
def fastmcp_factory(monkeypatch: pytest.MonkeyPatch) -> DummyFastMCP:
    app = DummyFastMCP(APP_NAME, APP_VERSION)
    monkeypatch.setattr("akosha.mcp.server.FastMCP", lambda name, version: app)
    return app


@pytest.fixture
def patched_lifespan(monkeypatch: pytest.MonkeyPatch):
    embedding_service = MagicMock()
    embedding_service.initialize = AsyncMock()
    embedding_service.is_available.return_value = True

    analytics_service = MagicMock(name="analytics")
    graph_builder = MagicMock(name="graph_builder")
    hot_store = MagicMock()
    hot_store.initialize = AsyncMock()

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
    monkeypatch.setattr("akosha.processing.analytics.TimeSeriesAnalytics", lambda: analytics_service)
    monkeypatch.setattr(
        "akosha.processing.knowledge_graph.KnowledgeGraphBuilder",
        lambda: graph_builder,
    )
    monkeypatch.setattr("akosha.storage.hot_store.HotStore", lambda database_path: hot_store)

    register_all_tools = MagicMock()
    monkeypatch.setattr("akosha.mcp.tools.register_all_tools", register_all_tools)

    return {
        "embedding_service": embedding_service,
        "analytics_service": analytics_service,
        "graph_builder": graph_builder,
        "hot_store": hot_store,
        "telemetry": telemetry,
        "shutdown_telemetry": shutdown_telemetry,
        "register_all_tools": register_all_tools,
        "cache_client": cache_client,
        "cold_storage": cold_storage,
    }


@pytest.mark.asyncio
async def test_create_app_standard_mode_lifespan(fastmcp_factory: DummyFastMCP, patched_lifespan, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("OTLP_ENDPOINT", "http://otel:4317")
    monkeypatch.setattr("akosha.observability.prometheus_metrics.generate_metrics", lambda: "metrics")

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
    assert json.loads(health_response.body) == {
        "status": "ok",
        "service": "akosha",
        "version": APP_VERSION,
    }
    assert json.loads(healthz_response.body) == {"status": "ok"}

    lifespan = app._mcp_server.lifespan
    async with lifespan(app) as context:
        assert context["akosha_ready"] is True
        assert context["mode"] == "standard"
        assert context["embedding_service"] is patched_lifespan["embedding_service"]
        assert context["analytics_service"] is patched_lifespan["analytics_service"]
        assert context["cache_client"] is None
        assert context["cold_storage"] is None

    patched_lifespan["embedding_service"].initialize.assert_awaited_once()
    patched_lifespan["hot_store"].initialize.assert_awaited_once()
    patched_lifespan["register_all_tools"].assert_called_once()
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
    monkeypatch.setattr("akosha.observability.prometheus_metrics.generate_metrics", lambda: "metrics")

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
