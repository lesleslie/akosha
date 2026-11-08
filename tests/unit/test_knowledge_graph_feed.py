"""Regression tests for the empty knowledge-graph feed.

Four independent defects kept ``mcp__akosha__akosha_get_graph_statistics``
returning ``{"total_entities": 0, "total_edges": 0, "entity_types": {},
"edge_types": {}}`` forever:

R1 — the tool read a DIFFERENT builder than the writer filled.
    ``set_shared_kg_builder`` ran AFTER ``_apply_tool_profile``, so the
    registration-time lookup returned ``None`` and the tool closures
    captured an ORPHAN builder while the refresh loop wrote into the
    lifespan's instance.

R2 — ``extract_entities`` raised ``AttributeError`` on every real row.
    DuckDB returns a ``JSON`` column as TEXT, so ``metadata.get(...)``
    blew up; the exception was swallowed at DEBUG level.

R3 — nothing produced the ``conversations`` table in the MCP process.
    The Redis event subscriber was wired only into the standalone
    FastAPI app, and the hot tier was an in-memory DuckDB that lost
    every row on restart.

CONFIG — the operator's XDG overlay was invisible: the readers parsed
    ``<repo>/settings/akosha.yaml`` directly instead of going through
    the Oneiric layered loader.

Each test below pins one of those, so a regression is a test failure
rather than a silently empty feed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from akosha.json_utils import coerce_json_object
from akosha.mcp.server import (
    clear_shared_services,
    create_app,
    get_shared_kg_builder,
)
from akosha.processing.knowledge_graph import GraphEntity, KnowledgeGraphBuilder
from tests._support.dummy_fastmcp import DummyFastMCP, make_fastmcp_factory

# ---------------------------------------------------------------------------
# R2 — the JSON-string metadata
# ---------------------------------------------------------------------------


class TestJsonStringMetadata:
    """``metadata`` arrives as TEXT from DuckDB; it must be decoded."""

    def test_coerce_decodes_a_json_string(self) -> None:
        assert coerce_json_object('{"user_id": "alice"}') == {"user_id": "alice"}

    def test_coerce_passes_a_dict_through(self) -> None:
        payload = {"user_id": "alice"}
        assert coerce_json_object(payload) is payload

    def test_coerce_maps_null_to_an_empty_dict(self) -> None:
        assert coerce_json_object(None) == {}

    def test_coerce_maps_blank_to_an_empty_dict(self) -> None:
        assert coerce_json_object("   ") == {}

    def test_coerce_drops_undecodable_text(self) -> None:
        assert coerce_json_object("{not json") == {}

    def test_coerce_drops_a_non_object_json_value(self) -> None:
        assert coerce_json_object("[1, 2, 3]") == {}

    @pytest.mark.asyncio
    async def test_extract_entities_accepts_a_json_string_metadata(self) -> None:
        """The regression: a raw JSON-string row must not raise.

        Before the fix this raised ``AttributeError: 'str' object has no
        attribute 'get'`` on EVERY real row, and the refresh loop
        swallowed it at DEBUG level — so the graph stayed empty with no
        trace in the log.
        """
        builder = KnowledgeGraphBuilder()
        entities = await builder.extract_entities(
            {
                "system_id": "akosha-mcp",
                "metadata": json.dumps({"user_id": "alice", "project": "akosha"}),
            }
        )
        entity_ids = {e.entity_id for e in entities}
        assert "system:akosha-mcp" in entity_ids
        assert "user:alice" in entity_ids
        assert "project:akosha" in entity_ids

    @pytest.mark.asyncio
    async def test_extract_entities_accepts_a_dict_metadata(self) -> None:
        """The dict form keeps working."""
        builder = KnowledgeGraphBuilder()
        entities = await builder.extract_entities(
            {"system_id": "akosha-mcp", "metadata": {"user_id": "bob"}}
        )
        assert "user:bob" in {e.entity_id for e in entities}

    @pytest.mark.asyncio
    async def test_extract_entities_accepts_a_missing_metadata(self) -> None:
        """A row with no metadata must not raise."""
        builder = KnowledgeGraphBuilder()
        entities = await builder.extract_entities({"system_id": "akosha-mcp"})
        assert [e.entity_id for e in entities] == ["system:akosha-mcp"]

    @pytest.mark.asyncio
    async def test_query_traces_decodes_the_json_column(self) -> None:
        """The storage boundary returns ``metadata`` as a dict.

        ``query_traces`` selects the substrate's ``metadata JSON``
        column. DuckDB returns it as TEXT, so the boundary decodes it —
        that is the ONE decode site (see ``akosha/json_utils.py``).
        """
        from akosha.models import HotRecord
        from akosha.storage.hot_store import HotStore

        store = HotStore(database_path=":memory:")
        await store.initialize()
        await store.insert(
            HotRecord(
                system_id="akosha",
                conversation_id="kg-r2-canned",
                content="hello",
                embedding=[0.0] * store._embedding_dim,
                timestamp=datetime(2026, 11, 5, 0, 0, 0, tzinfo=UTC),
                metadata={"task_class": "code_generation"},
            )
        )
        rows = await store.query_traces(system_id="akosha")
        await store.close()

        assert rows, "expected the canned row back"
        metadata = rows[0]["metadata"]
        assert isinstance(metadata, dict), (
            f"metadata must be decoded at the storage boundary, got {type(metadata).__name__}"
        )
        assert metadata["task_class"] == "code_generation"


# ---------------------------------------------------------------------------
# R1 — the tool and the writer must share ONE builder
# ---------------------------------------------------------------------------


@pytest.fixture
def fastmcp_factory(monkeypatch: pytest.MonkeyPatch) -> DummyFastMCP:
    return make_fastmcp_factory(monkeypatch, "Akosha", "0.0.0-test")


@pytest.fixture(autouse=True)
def _reset_module_state() -> Any:
    clear_shared_services()
    yield
    clear_shared_services()


class TestSharedBuilderInstance:
    """The writer and the reader must be the SAME object."""

    def test_registration_captures_the_published_builder(self) -> None:
        """``register_akosha_group`` reuses the published builder.

        The tool closures capture the builder at REGISTRATION time.
        If the shared builder is published after the registration, the
        lookup returns ``None`` and the closures capture a fresh orphan
        — the exact mechanism behind the permanently-empty graph.
        """
        from akosha.mcp.server import set_shared_kg_builder
        from akosha.mcp.tools.group_registers import _get_shared_kg_builder

        published = KnowledgeGraphBuilder()
        set_shared_kg_builder(published)

        assert _get_shared_kg_builder() is published

    @pytest.mark.asyncio
    async def test_the_tool_reads_what_the_writer_wrote(self) -> None:
        """End-to-end: an entity added to the shared builder shows up in
        ``akosha_get_graph_statistics``.

        This is the R1 regression guard. It drives the REAL tool
        registration (``register_graph_tools``), then writes into the
        same builder the refresh loop writes into, and reads the
        statistics back through the registered tool.
        """
        from akosha.mcp.tools.akosha_tools import register_graph_tools
        from akosha.mcp.tools.tool_registry import FastMCPToolRegistry

        shared = KnowledgeGraphBuilder()
        from akosha.mcp.server import set_shared_kg_builder

        set_shared_kg_builder(shared)

        registry = FastMCPToolRegistry(MagicMock())
        register_graph_tools(registry, shared)

        # The writer (kg_refresh) fills THIS builder.
        shared.entities["system:akosha-mcp"] = GraphEntity(
            entity_id="system:akosha-mcp",
            entity_type="system",
            properties={"name": "akosha-mcp"},
            source_system="akosha-mcp",
        )

        get_statistics = registry._tools["akosha_get_graph_statistics"].coroutine
        stats = await get_statistics()
        assert stats["total_entities"] == 1
        assert stats["entity_types"] == {"system": 1}

    @pytest.mark.asyncio
    async def test_the_lifespan_publishes_the_builder_before_tool_registration(
        self,
        fastmcp_factory: DummyFastMCP,  # noqa: ARG002 — fixture requested for its side effect
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ORDERING GUARD: the shared builder exists when the tools register.

        The tool groups capture the builder during
        ``_apply_tool_profile``. This test spies on that call and
        asserts the singleton is already published at that moment —
        which is exactly what was broken (the publish used to happen
        afterwards, so the lookup returned ``None`` and the tool
        captured an orphan).
        """
        observed: list[Any] = []

        async def _spy_profile(*_args: Any, **_kwargs: Any) -> None:
            observed.append(get_shared_kg_builder())

        monkeypatch.setattr("mcp_common.tools.dispatch._apply_tool_profile", _spy_profile)

        # Minimal lifespan dependencies.
        hot_store = MagicMock()
        hot_store.initialize = AsyncMock()
        hot_store.ping = AsyncMock()
        hot_store.list_code_graphs = AsyncMock(return_value=[])
        hot_store.query_traces = AsyncMock(return_value=[])
        hot_store.spans_substrate_table = "spans_substrate"
        hot_store.table_exists = AsyncMock(return_value=True)
        hot_store.insert_spans_substrate = AsyncMock(return_value=(0, 0))
        hot_store.insert_dlq_rows = AsyncMock(return_value=0)

        embedding_service = MagicMock()
        embedding_service.initialize = AsyncMock()
        embedding_service.is_available.return_value = True

        monkeypatch.setattr("akosha.storage.create_hot_store", lambda **_kw: hot_store)
        monkeypatch.setattr(
            "akosha.processing.embeddings.get_embedding_service",
            lambda: embedding_service,
        )
        monkeypatch.setattr("akosha.mcp.auth.validate_auth_config", lambda: True)
        monkeypatch.setattr(
            "akosha.observability.setup_telemetry", lambda **_kw: (object(), object())
        )
        monkeypatch.setattr("akosha.observability.shutdown_telemetry", MagicMock())
        monkeypatch.setattr(
            "akosha.observability.prometheus_metrics.generate_metrics",
            lambda: "metrics",
        )
        monkeypatch.setenv("AKOSHA_SKIP_CODE_GRAPH_INGESTER", "1")
        monkeypatch.setenv("AKOSHA_SKIP_KG_REFRESH", "1")
        monkeypatch.setenv("AKOSHA_SKIP_EVENT_SUBSCRIBER", "1")
        monkeypatch.setenv("AKOSHA_SKIP_AGING", "1")

        receiver = MagicMock(name="otel_receiver")
        receiver.start = AsyncMock()
        receiver.stop = AsyncMock()
        receiver.run_canary_emitter = AsyncMock()
        receiver.run_retention_sweep = AsyncMock()
        monkeypatch.setattr("akosha.mcp.server.OTLPReceiver", lambda *_args, **_kwargs: receiver)

        app = create_app()
        lifespan = app._mcp_server.lifespan
        async with lifespan(app):
            pass

        assert observed, "the tool profile was never applied"
        assert observed[0] is not None, (
            "the shared KnowledgeGraphBuilder was NOT published when the tools "
            "registered — the tool closures would capture an orphan builder"
        )
        assert isinstance(observed[0], KnowledgeGraphBuilder)


# ---------------------------------------------------------------------------
# R3 — a producer, and durable storage
# ---------------------------------------------------------------------------


class TestEventSubscriberWiring:
    """The MCP lifespan must own the event subscriber."""

    def test_build_event_subscribers_returns_none_when_disabled(self) -> None:
        from akosha.config import (
            BodaiToolInvocationSubscriberConfig,
            WebSocketInvocationsSubscriberConfig,
        )
        from akosha.ingestion.subscriber_wiring import build_event_subscribers

        subscriber = build_event_subscribers(
            MagicMock(),
            websocket_cfg=WebSocketInvocationsSubscriberConfig(enabled=False),
            bodai_cfg=BodaiToolInvocationSubscriberConfig(enabled=True),
        )
        assert subscriber is None

    def test_build_event_subscribers_builds_the_stack(self) -> None:
        from akosha.config import (
            BodaiToolInvocationSubscriberConfig,
            WebSocketInvocationsSubscriberConfig,
        )
        from akosha.ingestion.subscriber_wiring import build_event_subscribers

        subscriber = build_event_subscribers(
            MagicMock(),
            websocket_cfg=WebSocketInvocationsSubscriberConfig(enabled=True),
            bodai_cfg=BodaiToolInvocationSubscriberConfig(
                enabled=True, redis_url="redis://example:6379/1"
            ),
        )
        assert subscriber is not None
        assert subscriber._bodai_subscriber is not None
        assert subscriber._bodai_subscriber._redis_url == "redis://example:6379/1"

    def test_build_event_subscribers_skips_the_push_subscriber_when_disabled(self) -> None:
        from akosha.config import (
            BodaiToolInvocationSubscriberConfig,
            WebSocketInvocationsSubscriberConfig,
        )
        from akosha.ingestion.subscriber_wiring import build_event_subscribers

        subscriber = build_event_subscribers(
            MagicMock(),
            websocket_cfg=WebSocketInvocationsSubscriberConfig(enabled=True),
            bodai_cfg=BodaiToolInvocationSubscriberConfig(enabled=False),
        )
        assert subscriber is not None
        assert subscriber._bodai_subscriber is None

    def test_skip_env_var_disables_the_subscriber(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from akosha.config import (
            BodaiToolInvocationSubscriberConfig,
            WebSocketInvocationsSubscriberConfig,
        )
        from akosha.ingestion.subscriber_wiring import build_event_subscribers

        monkeypatch.setenv("AKOSHA_SKIP_EVENT_SUBSCRIBER", "1")
        subscriber = build_event_subscribers(
            MagicMock(),
            websocket_cfg=WebSocketInvocationsSubscriberConfig(enabled=True),
            bodai_cfg=BodaiToolInvocationSubscriberConfig(enabled=True),
        )
        assert subscriber is None

    @pytest.mark.asyncio
    async def test_the_lifespan_starts_the_event_subscriber(
        self,
        fastmcp_factory: DummyFastMCP,  # noqa: ARG002 — fixture requested for its side effect
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The lifespan owns the subscriber.

        Before the fix the subscriber existed ONLY in the standalone
        FastAPI app (``akosha/main.py``). The only Akosha process that
        actually runs is ``python -m akosha mcp start``, so the MCP
        process had no producer for the ``conversations`` table.
        """
        started: list[Any] = []

        class _FakeSubscriber:
            def __init__(self, **_kwargs: Any) -> None:
                self._running = False

            @property
            def running(self) -> bool:
                return self._running

            async def start(self) -> None:
                self._running = True
                started.append(self)

            async def stop(self) -> None:
                self._running = False

        monkeypatch.setattr(
            # NOTE: the patch target is the DEFINING module, not
            # ``subscriber_wiring``. ``build_event_subscribers`` imports the
            # class inside the function body, so the name is bound from
            # its own module at call time — patching
            # ``subscriber_wiring.WebSocketInvocationsSubscriber`` would
            # have no effect and the REAL subscriber would run.
            "akosha.ingestion.websocket_invocations_subscriber.WebSocketInvocationsSubscriber",
            _FakeSubscriber,
            raising=False,
        )

        hot_store = MagicMock()
        hot_store.initialize = AsyncMock()
        hot_store.ping = AsyncMock()
        hot_store.list_code_graphs = AsyncMock(return_value=[])
        hot_store.query_traces = AsyncMock(return_value=[])
        hot_store.spans_substrate_table = "spans_substrate"
        hot_store.table_exists = AsyncMock(return_value=True)
        hot_store.insert_spans_substrate = AsyncMock(return_value=(0, 0))
        hot_store.insert_dlq_rows = AsyncMock(return_value=0)

        embedding_service = MagicMock()
        embedding_service.initialize = AsyncMock()
        embedding_service.is_available.return_value = True

        monkeypatch.setattr("akosha.storage.create_hot_store", lambda **_kw: hot_store)
        monkeypatch.setattr(
            "akosha.processing.embeddings.get_embedding_service",
            lambda: embedding_service,
        )
        monkeypatch.setattr("akosha.mcp.auth.validate_auth_config", lambda: True)
        monkeypatch.setattr(
            "akosha.observability.setup_telemetry", lambda **_kw: (object(), object())
        )
        monkeypatch.setattr("akosha.observability.shutdown_telemetry", MagicMock())
        monkeypatch.setattr(
            "akosha.observability.prometheus_metrics.generate_metrics",
            lambda: "metrics",
        )
        monkeypatch.setattr(
            "akosha.observability.resource_validator.ResourceAttributeValidator",
            lambda **_kw: MagicMock(),
        )
        monkeypatch.setattr(
            "mcp_common.tools.dispatch._apply_tool_profile",
            AsyncMock(),
        )
        monkeypatch.setenv("AKOSHA_SKIP_CODE_GRAPH_INGESTER", "1")
        monkeypatch.setenv("AKOSHA_SKIP_KG_REFRESH", "1")
        monkeypatch.setenv("AKOSHA_SKIP_AGING", "1")
        monkeypatch.delenv("AKOSHA_SKIP_EVENT_SUBSCRIBER", raising=False)

        receiver = MagicMock(name="otel_receiver")
        receiver.start = AsyncMock()
        receiver.stop = AsyncMock()
        receiver.run_canary_emitter = AsyncMock()
        receiver.run_retention_sweep = AsyncMock()
        monkeypatch.setattr("akosha.mcp.server.OTLPReceiver", lambda *_args, **_kwargs: receiver)

        # The subscriber config must say "enabled" for the lifespan to
        # start it.
        from akosha.config import (
            BodaiToolInvocationSubscriberConfig,
            WebSocketInvocationsSubscriberConfig,
        )

        real_get_config = __import__("akosha.config", fromlist=["get_config"]).get_config
        real_cfg = real_get_config()

        class _Cfg:
            spans_substrate = real_cfg.spans_substrate
            resource_validator = real_cfg.resource_validator
            hot = real_cfg.hot
            otel = real_cfg.otel
            websocket_invocations_subscriber = WebSocketInvocationsSubscriberConfig(enabled=True)
            bodai_tool_invocation_subscriber = BodaiToolInvocationSubscriberConfig(enabled=True)

        monkeypatch.setattr("akosha.config.get_config", lambda: _Cfg())

        app = create_app()
        lifespan = app._mcp_server.lifespan
        async with lifespan(app):
            assert started, "the lifespan never started the event subscriber"

    @pytest.mark.asyncio
    async def test_lite_mode_does_not_start_the_subscriber(
        self,
        fastmcp_factory: DummyFastMCP,  # noqa: ARG002 — fixture requested for its side effect
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Lite mode disables external services — and says so.

        The requirement: a mode that disables services must not silently
        hide a broken feed. Lite mode skips the Redis-backed producer
        and the health payload reports the producer as not running.
        """
        from akosha.mcp import server as mcp_server

        started: list[Any] = []

        class _FakeSubscriber:
            def __init__(self, **_kwargs: Any) -> None:
                self._running = False

            @property
            def running(self) -> bool:
                return self._running

            async def start(self) -> None:
                self._running = True
                started.append(self)

            async def stop(self) -> None:
                self._running = False

        monkeypatch.setattr(
            # NOTE: the patch target is the DEFINING module, not
            # ``subscriber_wiring``. ``build_event_subscribers`` imports the
            # class inside the function body, so the name is bound from
            # its own module at call time — patching
            # ``subscriber_wiring.WebSocketInvocationsSubscriber`` would
            # have no effect and the REAL subscriber would run.
            "akosha.ingestion.websocket_invocations_subscriber.WebSocketInvocationsSubscriber",
            _FakeSubscriber,
            raising=False,
        )

        hot_store = MagicMock()
        hot_store.initialize = AsyncMock()
        hot_store.ping = AsyncMock()
        hot_store.list_code_graphs = AsyncMock(return_value=[])
        hot_store.query_traces = AsyncMock(return_value=[])
        hot_store.spans_substrate_table = "spans_substrate"
        hot_store.table_exists = AsyncMock(return_value=True)
        hot_store.insert_spans_substrate = AsyncMock(return_value=(0, 0))
        hot_store.insert_dlq_rows = AsyncMock(return_value=0)

        embedding_service = MagicMock()
        embedding_service.initialize = AsyncMock()
        embedding_service.is_available.return_value = True

        monkeypatch.setattr("akosha.storage.create_hot_store", lambda **_kw: hot_store)
        monkeypatch.setattr(
            "akosha.processing.embeddings.get_embedding_service",
            lambda: embedding_service,
        )
        monkeypatch.setattr("akosha.mcp.auth.validate_auth_config", lambda: True)
        monkeypatch.setattr(
            "akosha.observability.setup_telemetry", lambda **_kw: (object(), object())
        )
        monkeypatch.setattr("akosha.observability.shutdown_telemetry", MagicMock())
        monkeypatch.setattr(
            "akosha.observability.prometheus_metrics.generate_metrics",
            lambda: "metrics",
        )
        monkeypatch.setattr(
            "akosha.observability.resource_validator.ResourceAttributeValidator",
            lambda **_kw: MagicMock(),
        )
        monkeypatch.setattr(
            "mcp_common.tools.dispatch._apply_tool_profile",
            AsyncMock(),
        )
        monkeypatch.setenv("AKOSHA_SKIP_CODE_GRAPH_INGESTER", "1")
        monkeypatch.setenv("AKOSHA_SKIP_KG_REFRESH", "1")
        monkeypatch.setenv("AKOSHA_SKIP_AGING", "1")
        monkeypatch.delenv("AKOSHA_SKIP_EVENT_SUBSCRIBER", raising=False)

        receiver = MagicMock(name="otel_receiver")
        receiver.start = AsyncMock()
        receiver.stop = AsyncMock()
        receiver.run_canary_emitter = AsyncMock()
        receiver.run_retention_sweep = AsyncMock()
        monkeypatch.setattr("akosha.mcp.server.OTLPReceiver", lambda *_args, **_kwargs: receiver)

        lite_mode = SimpleNamespace(
            mode_config=SimpleNamespace(description="lite"),
            requires_external_services=False,
            initialize_cache=AsyncMock(return_value=None),
            initialize_cold_storage=AsyncMock(return_value=None),
        )

        app = create_app(mode=lite_mode)
        lifespan = app._mcp_server.lifespan
        async with lifespan(app):
            assert not started, "lite mode must not start the Redis-backed subscriber"
            assert mcp_server._event_subscriber_running is False


class TestDurableHotTier:
    """The hot tier must be durable, and the setting must reach the runtime."""

    def test_the_shipped_config_selects_the_durable_backend(self) -> None:
        """The committed repo default must be the durable DuckDB variant.

        ``duckdb-memory`` loses every row on restart — which is what
        kept the knowledge graph empty across restarts.
        """
        import yaml

        repo_settings = Path(__file__).resolve().parents[2] / "settings" / "akosha.yaml"
        data = yaml.safe_load(repo_settings.read_text()) or {}
        hot = data["hot"]
        assert hot["backend"] == "duckdb-ssd"
        assert hot["path"] not in (":memory:", "")

    @pytest.mark.asyncio
    async def test_the_configured_path_reaches_the_hot_store_factory(
        self,
        fastmcp_factory: DummyFastMCP,  # noqa: ARG002 — fixture requested for its side effect
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The lifespan passes the configured backend + path to the factory.

        Before the fix the lifespan called ``create_hot_store()`` with NO
        arguments, so the hot tier was always the default in-memory DuckDB
        (``:memory:``) — every row was lost on restart and the configured
        backend never reached the runtime. This drives the real lifespan
        and asserts the factory received the layered config's
        ``hot.backend`` and ``hot.path``.
        """
        from akosha.config import get_config

        calls: list[dict[str, Any]] = []

        def _spy(**kwargs: Any) -> MagicMock:
            calls.append(dict(kwargs))
            store = MagicMock()
            store.initialize = AsyncMock()
            return store

        monkeypatch.setattr("akosha.storage.create_hot_store", _spy)

        # Minimal lifespan dependencies.
        hot_store = MagicMock()
        hot_store.initialize = AsyncMock()
        hot_store.ping = AsyncMock()
        hot_store.list_code_graphs = AsyncMock(return_value=[])
        hot_store.query_traces = AsyncMock(return_value=[])
        hot_store.spans_substrate_table = "spans_substrate"
        hot_store.table_exists = AsyncMock(return_value=True)
        hot_store.insert_spans_substrate = AsyncMock(return_value=(0, 0))
        hot_store.insert_dlq_rows = AsyncMock(return_value=0)

        embedding_service = MagicMock()
        embedding_service.initialize = AsyncMock()
        embedding_service.is_available.return_value = True

        monkeypatch.setattr(
            "akosha.processing.embeddings.get_embedding_service",
            lambda: embedding_service,
        )
        monkeypatch.setattr("akosha.mcp.auth.validate_auth_config", lambda: True)
        monkeypatch.setattr(
            "akosha.observability.setup_telemetry", lambda **_kw: (object(), object())
        )
        monkeypatch.setattr("akosha.observability.shutdown_telemetry", MagicMock())
        monkeypatch.setattr(
            "akosha.observability.prometheus_metrics.generate_metrics",
            lambda: "metrics",
        )
        # Tool registration is irrelevant here and DummyFastMCP has no
        # ``list_tools``; the real dispatcher needs one.
        monkeypatch.setattr(
            "mcp_common.tools.dispatch._apply_tool_profile",
            AsyncMock(return_value=None),
        )
        monkeypatch.setenv("AKOSHA_SKIP_CODE_GRAPH_INGESTER", "1")
        monkeypatch.setenv("AKOSHA_SKIP_KG_REFRESH", "1")
        monkeypatch.setenv("AKOSHA_SKIP_EVENT_SUBSCRIBER", "1")
        monkeypatch.setenv("AKOSHA_SKIP_AGING", "1")

        receiver = MagicMock(name="otel_receiver")
        receiver.start = AsyncMock()
        receiver.stop = AsyncMock()
        receiver.run_canary_emitter = AsyncMock()
        receiver.run_retention_sweep = AsyncMock()
        monkeypatch.setattr("akosha.mcp.server.OTLPReceiver", lambda *_args, **_kwargs: receiver)

        app = create_app()
        lifespan = app._mcp_server.lifespan
        async with lifespan(app):
            pass

        hot_cfg = get_config().hot
        assert {"backend": hot_cfg.backend, "database_path": hot_cfg.path} in calls, (
            "the lifespan did not forward the configured hot backend + path to "
            f"create_hot_store; calls were {calls!r}"
        )

    def test_a_relative_hot_path_resolves_against_the_storage_base(self) -> None:
        """A relative path is portable — it does not depend on the CWD."""
        from akosha.config import HotStorageConfig

        cfg = HotStorageConfig(backend="duckdb-ssd", path="hot/hot.db")
        assert cfg.path != "hot/hot.db"
        assert cfg.path.endswith("hot/hot.db")
        assert Path(cfg.path).is_absolute()

    def test_the_memory_path_stays_memory(self) -> None:
        from akosha.config import HotStorageConfig

        cfg = HotStorageConfig(backend="duckdb-memory", path=":memory:")
        assert cfg.path == ":memory:"
