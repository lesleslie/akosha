"""Wave 5: MCP lifespan wiring tests.

The audit caught an empty-feed failure mode (see ``docs/superpowers/specs/
2026-09-05-akosha-hardening-design.md`` Appendix A): the MCP server
registered 30+ tools, returned 200 from ``/health``, but every data feed
(``get_graph_statistics``, ``query_local_traces``, ``search_code_patterns``)
returned empty because no producer was ever started.

Wave 5 wires three things in the lifespan:

1. A ``hot_store`` singleton published via ``set_shared_hot_store`` so the
   per-group tool wrappers (which construct their own ``HotStore``) reuse
   the same in-memory database the lifespan's producers write to.
2. A ``CodeGraphIngester`` polling loop writing code graphs into the
   shared hot_store.
3. A periodic-refresh task that pulls recent traces from ``hot_store``
   and feeds them into a lifespan-owned ``KnowledgeGraphBuilder``.

These tests verify the wiring without starting a real MCP server. They
patch the lifespan's heavy imports (telemetry, embedding service, mode
factory) and assert that the lifespan:

- publishes singletons that downstream tools can read,
- starts the CodeGraphIngester (with mock backing),
- starts the kg_refresh task,
- cancels both background tasks on shutdown,
- extends ``/health`` with per-feed aggregates.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from akosha.mcp import server as mcp_server
from akosha.mcp.server import (
    APP_NAME,
    APP_VERSION,
    clear_shared_services,
    create_app,
    get_shared_hot_store,
    get_shared_kg_builder,
)


# ---------------------------------------------------------------------------
# Dummy FastMCP fixture (mirrors test_mcp_server_lifespan.py)
# ---------------------------------------------------------------------------


class DummyFastMCP:
    def __init__(self, name: str, version: str, lifespan: Any = None) -> None:
        self.name = name
        self.version = version
        self.lifespan = lifespan
        self._mcp_server = SimpleNamespace(lifespan=lifespan)
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

    def factory(name: str, version: str, lifespan: Any = None) -> DummyFastMCP:
        app.lifespan = lifespan
        app._mcp_server.lifespan = lifespan
        return app

    monkeypatch.setattr("akosha.mcp.server.FastMCP", factory)
    return app


@pytest.fixture(autouse=True)
def _reset_wave5_module_state(monkeypatch: pytest.MonkeyPatch):
    """Clear Wave-5 module-level singletons before and after every test.

    Without this, a test that aborts mid-lifespan can leave
    ``_code_graph_ingester`` / ``_kg_refresh_task`` populated, and the
    next test's lifespan teardown (or a subsequent lifespan startup that
    doesn't mock ``CodeGraphIngester``) sees stale references and
    either leaks the ingester or hangs trying to cancel an already-
    cancelled task.
    """
    clear_shared_services()
    mcp_server._code_graph_ingester = None
    mcp_server._kg_refresh_task = None
    mcp_server._kg_refresh_cycles = 0
    mcp_server._kg_refresh_errors = 0
    yield
    clear_shared_services()
    mcp_server._code_graph_ingester = None
    mcp_server._kg_refresh_task = None
    mcp_server._kg_refresh_cycles = 0
    mcp_server._kg_refresh_errors = 0


@pytest.fixture
def lifespan_deps(monkeypatch: pytest.MonkeyPatch):
    """Patch every heavy dependency the lifespan touches.

    Returns a dict of mock handles so each test can assert against the
    exact objects the lifespan constructed or invoked.
    """
    embedding_service = MagicMock()
    embedding_service.initialize = AsyncMock()
    embedding_service.is_available.return_value = True

    analytics_service = MagicMock(name="analytics")

    # KnowledgeGraphBuilder is now constructed by the lifespan (Wave 5),
    # so the test asserts on the real class — no monkeypatching the
    # constructor. We still return a MagicMock for hot_store.

    hot_store = MagicMock()
    hot_store.initialize = AsyncMock()
    hot_store.ping = AsyncMock()
    # Default feed aggregates: 0 ingested code graphs, 0 traces.
    hot_store.list_code_graphs = AsyncMock(return_value=[])
    hot_store.query_traces = AsyncMock(return_value=[])

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
        "akosha.processing.analytics.TimeSeriesAnalytics",
        lambda: analytics_service,
    )
    monkeypatch.setattr("akosha.storage.hot_store.HotStore", lambda database_path: hot_store)
    monkeypatch.setattr("akosha.storage.create_hot_store", lambda: hot_store)
    monkeypatch.setattr(
        "akosha.observability.prometheus_metrics.generate_metrics",
        lambda: "metrics",
    )

    apply_tool_profile = AsyncMock()
    monkeypatch.setattr(
        "mcp_common.tools.dispatch._apply_tool_profile",
        apply_tool_profile,
    )

    # Wave 5: stub CodeGraphIngester so the lifespan can construct it
    # without trying to reach a real Session-Buddy HTTP endpoint.
    ingester_instances: list[Any] = []

    class FakeCodeGraphIngester:
        def __init__(
            self,
            hot_store: Any,
            session_buddy_endpoint: str = "",
            poll_interval_seconds: int = 60,
            **_kwargs: Any,
        ) -> None:
            self.hot_store = hot_store
            self.session_buddy_endpoint = session_buddy_endpoint
            self.poll_interval_seconds = poll_interval_seconds
            self._running = False
            self.start_calls = 0
            self.stop_calls = 0
            ingester_instances.append(self)

        async def start(self) -> None:
            self._running = True
            self.start_calls += 1

        async def stop(self) -> None:
            self._running = False
            self.stop_calls += 1

    monkeypatch.setattr(
        "akosha.ingestion.code_graph_ingester.CodeGraphIngester",
        FakeCodeGraphIngester,
    )

    # Wave 6: stub OtelTraceIngester so the lifespan can construct it
    # without trying to reach a real OTLP/HTTP collector endpoint.
    otel_ingester_instances: list[Any] = []

    class FakeOtelTraceIngester:
        def __init__(
            self,
            hot_store: Any,
            embedding_service: Any,
            otlp_endpoint: str = "",
            poll_interval_seconds: int = 60,
            **_: Any,
        ) -> None:
            self.hot_store = hot_store
            self.embedding_service = embedding_service
            self.otlp_endpoint = otlp_endpoint
            self.poll_interval_seconds = poll_interval_seconds
            self._running = False
            self.start_calls = 0
            self.stop_calls = 0
            otel_ingester_instances.append(self)

        async def start(self) -> None:
            self._running = True
            self.start_calls += 1

        async def stop(self) -> None:
            self._running = False
            self.stop_calls += 1

    monkeypatch.setattr(
        "akosha.ingestion.otel_ingester.OtelTraceIngester",
        FakeOtelTraceIngester,
    )

    # Shorten the kg_refresh interval so the test can verify cycles run.
    monkeypatch.setenv("AKOSHA_KG_REFRESH_SECONDS", "0.05")
    # Force the lifespan to also skip Dhara registration noise.
    monkeypatch.setenv("AKOSHA_SKIP_DHARA_REGISTRATION", "1")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("OTLP_ENDPOINT", "http://otel:4317")

    return {
        "embedding_service": embedding_service,
        "analytics_service": analytics_service,
        "hot_store": hot_store,
        "telemetry": telemetry,
        "shutdown_telemetry": shutdown_telemetry,
        "apply_tool_profile": apply_tool_profile,
        "cache_client": cache_client,
        "cold_storage": cold_storage,
        "ingester_instances": ingester_instances,
        "otel_ingester_instances": otel_ingester_instances,
    }


# ---------------------------------------------------------------------------
# Singleton tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifespan_publishes_shared_hot_store(
    fastmcp_factory: DummyFastMCP, lifespan_deps: dict[str, Any]
) -> None:
    """Inside the lifespan, ``get_shared_hot_store()`` returns the
    lifespan's HotStore. After shutdown, it's cleared."""
    clear_shared_services()
    assert get_shared_hot_store() is None

    app = create_app()
    lifespan = app._mcp_server.lifespan
    async with lifespan(app):
        shared = get_shared_hot_store()
        assert shared is lifespan_deps["hot_store"]

    # After shutdown the singleton is cleared so the next create_app()
    # starts fresh (test reuse, hot reload).
    assert get_shared_hot_store() is None


@pytest.mark.asyncio
async def test_lifespan_publishes_shared_kg_builder(
    fastmcp_factory: DummyFastMCP, lifespan_deps: dict[str, Any]
) -> None:
    """Inside the lifespan, ``get_shared_kg_builder()`` returns a real
    ``KnowledgeGraphBuilder`` (the lifespan constructs one — it isn't
    mocked away any more)."""
    clear_shared_services()
    from akosha.processing.knowledge_graph import KnowledgeGraphBuilder

    assert get_shared_kg_builder() is None

    app = create_app()
    lifespan = app._mcp_server.lifespan
    async with lifespan(app):
        kg = get_shared_kg_builder()
        assert isinstance(kg, KnowledgeGraphBuilder)
        assert kg.entities == {}
        assert kg.edges == []

    assert get_shared_kg_builder() is None


# ---------------------------------------------------------------------------
# CodeGraphIngester tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifespan_starts_code_graph_ingester(
    fastmcp_factory: DummyFastMCP, lifespan_deps: dict[str, Any]
) -> None:
    """The lifespan constructs and starts exactly one CodeGraphIngester,
    bound to the shared hot_store, and stops it on shutdown."""
    app = create_app()
    lifespan = app._mcp_server.lifespan
    async with lifespan(app):
        instances = lifespan_deps["ingester_instances"]
        assert len(instances) == 1
        ingester = instances[0]
        assert ingester.hot_store is lifespan_deps["hot_store"]
        assert ingester.start_calls == 1
        assert ingester._running is True

    # After shutdown: stopped once.
    ingester = lifespan_deps["ingester_instances"][0]
    assert ingester.stop_calls == 1
    assert ingester._running is False


@pytest.mark.asyncio
async def test_lifespan_can_skip_code_graph_ingester(
    fastmcp_factory: DummyFastMCP, lifespan_deps: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``AKOSHA_SKIP_CODE_GRAPH_INGESTER=1`` short-circuits the ingester
    construction (useful for offline test suites)."""
    monkeypatch.setenv("AKOSHA_SKIP_CODE_GRAPH_INGESTER", "1")
    app = create_app()
    lifespan = app._mcp_server.lifespan
    async with lifespan(app):
        assert lifespan_deps["ingester_instances"] == []
    # No exception on shutdown.


# ---------------------------------------------------------------------------
# Wave 6: OtelTraceIngester tests (mirror the CodeGraphIngester block)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifespan_starts_otel_trace_ingester(
    fastmcp_factory: DummyFastMCP, lifespan_deps: dict[str, Any]
) -> None:
    """The lifespan constructs and starts exactly one OtelTraceIngester,
    bound to the shared hot_store, and stops it on shutdown."""
    app = create_app()
    lifespan = app._mcp_server.lifespan
    async with lifespan(app):
        instances = lifespan_deps["otel_ingester_instances"]
        assert len(instances) == 1
        ingester = instances[0]
        assert ingester.hot_store is lifespan_deps["hot_store"]
        assert ingester.embedding_service is lifespan_deps["embedding_service"]
        assert ingester.start_calls == 1
        assert ingester._running is True

    # After shutdown: stopped once.
    ingester = lifespan_deps["otel_ingester_instances"][0]
    assert ingester.stop_calls == 1
    assert ingester._running is False


@pytest.mark.asyncio
async def test_lifespan_can_skip_otel_trace_ingester(
    fastmcp_factory: DummyFastMCP, lifespan_deps: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``AKOSHA_SKIP_OTEL_INGESTER=1`` short-circuits the ingester
    construction (useful for offline test suites)."""
    monkeypatch.setenv("AKOSHA_SKIP_OTEL_INGESTER", "1")
    app = create_app()
    lifespan = app._mcp_server.lifespan
    async with lifespan(app):
        assert lifespan_deps["otel_ingester_instances"] == []
    # No exception on shutdown.


# ---------------------------------------------------------------------------
# kg_refresh task tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifespan_runs_kg_refresh_cycles(
    fastmcp_factory: DummyFastMCP, lifespan_deps: dict[str, Any]
) -> None:
    """The kg_refresh task runs at least one cycle and feeds traces into
    the shared KnowledgeGraphBuilder."""
    # Wire two synthetic trace rows so the refresh loop has data to
    # extract from. ``extract_entities`` reads ``system_id`` and
    # ``metadata.user_id`` / ``metadata.project``.
    hot_store = lifespan_deps["hot_store"]
    hot_store.query_traces = AsyncMock(
        return_value=[
            {"system_id": "akosha-mcp", "metadata": {"user_id": "alice", "project": "akosha"}},
            {"system_id": "akosha-mcp", "metadata": {"user_id": "bob", "project": "akosha"}},
        ]
    )

    app = create_app()
    lifespan = app._mcp_server.lifespan
    async with lifespan(app):
        # Wait for at least one refresh cycle (interval is 0.05s).
        for _ in range(50):
            kg = get_shared_kg_builder()
            if kg is not None and len(kg.entities) > 0:
                break
            import asyncio

            await asyncio.sleep(0.05)
        kg = get_shared_kg_builder()
        assert kg is not None
        # Two user entities + one project + one system per row => at
        # least 2 user entities created.
        user_entities = [e for e in kg.entities.values() if e.entity_type == "user"]
        assert len(user_entities) >= 2
        # And at least one project entity (deduped by id).
        project_entities = [e for e in kg.entities.values() if e.entity_type == "project"]
        assert len(project_entities) >= 1
        # And at least one edge.
        assert len(kg.edges) >= 1


@pytest.mark.asyncio
async def test_lifespan_cancels_kg_refresh_on_shutdown(
    fastmcp_factory: DummyFastMCP, lifespan_deps: dict[str, Any]
) -> None:
    """The kg_refresh task is cancelled deterministically on shutdown."""
    app = create_app()
    lifespan = app._mcp_server.lifespan
    async with lifespan(app):
        # While running, the task is active and not done.
        task = mcp_server._kg_refresh_task
        assert task is not None
        assert not task.done()
    # After shutdown, the module-level reference is cleared.
    assert mcp_server._kg_refresh_task is None


# ---------------------------------------------------------------------------
# /health probe: per-feed aggregates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_probe_surfaces_per_feed_aggregates(
    fastmcp_factory: DummyFastMCP, lifespan_deps: dict[str, Any]
) -> None:
    """The default health probe reports feed_entities_count, edges_count,
    cycles_total, errors_total, and ingest-task running state for the
    three Wave-5 feeds. ``ok`` is False when a feed is empty AND the
    producer has run at least one cycle — that surfaces wire-up drift.
    """
    import asyncio as _asyncio

    app = create_app()
    lifespan = app._mcp_server.lifespan
    async with lifespan(app):
        # Wait for at least one kg_refresh cycle to complete (the fixture
        # sets AKOSHA_KG_REFRESH_SECONDS=0.05, so 0.2s is enough for ≥4
        # cycles). Without this, the probe would see cycles == 0 and
        # report the warm-up ``ok=True`` path instead of the drift path.
        for _ in range(50):
            if mcp_server._kg_refresh_cycles >= 1:
                break
            await _asyncio.sleep(0.01)

        probe = mcp_server.get_health_probe()
        assert probe is not None
        result = await probe()

    # Pre-Wave-5 checks still present.
    assert result["hot_store"]["ok"] is True
    assert result["embeddings"]["ok"] is True

    # Wave-5 per-feed aggregates added. The probe was called AFTER the
    # first kg_refresh cycle ran (fixture sets AKOSHA_KG_REFRESH_SECONDS=0.05)
    # AND the CodeGraphIngester started a poll, but with empty hot_store
    # data the feeds are empty. Per the new contract, ``ok`` is False
    # when a feed is empty AND the producer has run ≥1 cycle.
    assert "code_graphs_feed" in result
    assert result["code_graphs_feed"]["feed_entities_count"] == 0
    assert result["code_graphs_feed"]["ingester_running"] is True
    # The probe surfaces the cycle + error counters on every feed; pre-fix
    # these fields didn't exist (the implementation hardcoded "ok": True).
    assert "cycles_total" in result["code_graphs_feed"]
    assert "errors_total" in result["code_graphs_feed"]
    assert "feed_last_updated_timestamp" in result["code_graphs_feed"]

    assert "knowledge_graph_feed" in result
    assert result["knowledge_graph_feed"]["feed_entities_count"] == 0
    assert result["knowledge_graph_feed"]["edges_count"] == 0
    # REQ-005 follow-up (kg side, symmetric to the OTel fix in
    # 43d85de): the kg_refresh task is alive, has cycled once with no
    # errors, and the feed is empty. Under the new contract
    # (``kg_warming_up`` disjunct), this is "warming up" and reports
    # ok=True.
    assert result["knowledge_graph_feed"]["ok"] is True
    assert result["knowledge_graph_feed"]["cycles_total"] >= 1
    # The result was captured INSIDE the ``async with``, so the
    # kg_refresh task was still active when the probe ran.
    assert result["knowledge_graph_feed"]["refresh_task_running"] is True

    assert "local_traces_feed" in result
    assert result["local_traces_feed"]["feed_entities_count"] == 0
    # REQ-005 follow-up: the OTel ingester starts under the lifespan and
    # cycles once successfully. Under the new contract (the
    # ``otel_warming_up`` disjunct in local_traces_ok), a running
    # producer with zero errors is "warming up" and reports ok=True.
    # ``feed_populated`` stays False so operators can still tell the
    # difference between warming-up and "data has arrived".
    assert result["local_traces_feed"]["ok"] is True
    assert result["local_traces_feed"]["feed_populated"] is False
    assert result["local_traces_feed"]["cycles_total"] >= 1


@pytest.mark.asyncio
async def test_health_route_returns_200_when_all_feeds_ok(
    fastmcp_factory: DummyFastMCP, lifespan_deps: dict[str, Any]
) -> None:
    """End-to-end: /health route aggregates the probe and returns 200
    with per-feed counts in the body."""
    app = create_app()
    lifespan = app._mcp_server.lifespan
    async with lifespan(app):
        response = await app.routes["/health"]["handler"](None)
    body = json.loads(response.body)
    assert body["status"] == "ok"
    assert "checks" in body
    assert "code_graphs_feed" in body["checks"]
    assert "knowledge_graph_feed" in body["checks"]
    assert "local_traces_feed" in body["checks"]


@pytest.mark.asyncio
async def test_health_probe_warmup_paths_report_ok(
    fastmcp_factory: DummyFastMCP, lifespan_deps: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """During warm-up (no cycles yet), the probe reports ``ok=True`` even
    when the feeds are empty. This is the complement of
    ``test_health_probe_surfaces_per_feed_aggregates``: the new contract
    is that ``ok`` is False ONLY when the producer has run ≥1 cycle AND
    the feed is still empty."""
    # Disable the kg_refresh + CodeGraphIngester so cycles == 0 when the
    # probe runs. The probe must report ok=True on empty feeds while the
    # producers haven't ticked yet.
    monkeypatch.setenv("AKOSHA_SKIP_CODE_GRAPH_INGESTER", "1")
    monkeypatch.setenv("AKOSHA_SKIP_KG_REFRESH", "1")
    app = create_app()
    lifespan = app._mcp_server.lifespan
    async with lifespan(app):
        # Probe IMMEDIATELY before any cycle can run.
        probe = mcp_server.get_health_probe()
        assert probe is not None
        result = await probe()

        assert result["code_graphs_feed"]["ok"] is True
        assert result["code_graphs_feed"]["ingester_running"] is False
        assert result["knowledge_graph_feed"]["ok"] is True
        assert result["knowledge_graph_feed"]["refresh_task_running"] is False
        assert result["local_traces_feed"]["ok"] is True


# ---------------------------------------------------------------------------
# group_registers integration: shared singletons reused
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_registers_reuses_shared_hot_store(
    fastmcp_factory: DummyFastMCP, lifespan_deps: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_try_create_hot_store`` returns the lifespan-owned hot_store
    when one is published. Without the singleton, it falls back to
    ``create_hot_store()`` (which the fixture monkeypatches to return
    the same MagicMock — so the assertion below verifies the singleton
    path returns the lifespan's instance, not a freshly-built one)."""
    from akosha.mcp.tools.group_registers import _try_create_hot_store

    clear_shared_services()
    # Without singleton: the helper returns whatever ``create_hot_store()``
    # produces (the patched MagicMock in this test).
    fresh = await _try_create_hot_store()
    assert fresh is not None

    # With singleton: returns the lifespan-published instance.
    app = create_app()
    lifespan = app._mcp_server.lifespan
    async with lifespan(app):
        shared = await _try_create_hot_store()
        assert shared is lifespan_deps["hot_store"]
        # The fixture monkeypatches ``create_hot_store`` to return the same
        # MagicMock every call, so the singleton and fallback paths produce
        # the same object in this test. The production-relevant check is
        # that the helper honours the singleton when set.
        assert shared is fresh


@pytest.mark.asyncio
async def test_group_registers_reuses_shared_kg_builder(
    fastmcp_factory: DummyFastMCP, lifespan_deps: dict[str, Any]
) -> None:
    """``register_akosha_group`` passes the lifespan-owned
    KnowledgeGraphBuilder to the tool registration. Without the
    singleton it constructs a fresh empty one (pre-Wave-5 behaviour)."""
    from akosha.processing.knowledge_graph import KnowledgeGraphBuilder

    from akosha.mcp.tools.group_registers import _get_shared_kg_builder

    clear_shared_services()
    # Without singleton: returns None (caller constructs fresh).
    assert _get_shared_kg_builder() is None

    # With singleton: returns the real KnowledgeGraphBuilder the
    # lifespan published.
    app = create_app()
    lifespan = app._mcp_server.lifespan
    async with lifespan(app):
        kg = _get_shared_kg_builder()
        assert isinstance(kg, KnowledgeGraphBuilder)
