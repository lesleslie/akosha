from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from akosha.mcp.server import APP_NAME, APP_VERSION, create_app
from akosha.models import WarmRecord
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
    # The lifespan now passes the configured backend + database path so
    # the durable DuckDB setting reaches the runtime; the double has to
    # accept them.
    monkeypatch.setattr("akosha.storage.create_hot_store", lambda **_kwargs: hot_store)

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
    # The lifespan now wires the Redis-backed event subscriber; keep it
    # out of the unit-test path so no test reaches for a real Redis.
    monkeypatch.setenv("AKOSHA_SKIP_EVENT_SUBSCRIBER", "1")
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


class ColdStoreDouble:
    """Minimal stand-in for the cold store the lifespan wires up.

    The real ``ColdStore`` needs a oneiric adapter; this double carries
    only what the exporter touches, so the wiring test does not reach for
    a bucket.

    ``export_batch`` mirrors the real keyword call site
    (``export_batch(records=..., partition_path=...)``) exactly. The
    parameter was previously named ``_records``, so a real exporter
    calling it with ``records=`` raised ``TypeError`` -- swallowed by the
    export loop, which made a broken export look like a healthy one.
    """

    def __init__(self) -> None:
        self.bytes_exported = 0
        self.exported: list[str] = []
        self.exported_records = 0

    async def export_batch(self, records, partition_path: str) -> str:
        self.exported.append(partition_path)
        self.exported_records += len(records)
        return f"{partition_path}/batch.parquet"


class ColdMode:
    """A mode that hands the lifespan a cold store."""

    requires_external_services = False

    def __init__(self, cold_storage: object) -> None:
        self._cold_storage = cold_storage

    async def initialize_cache(self):
        return None

    async def initialize_cold_storage(self):
        return self._cold_storage


@pytest.mark.asyncio
async def test_cold_export_task_is_wired(
    fastmcp_factory: DummyFastMCP,
    patched_lifespan,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """The lifespan starts the warm->cold export cron (F5).

    Without it the cold tier is reachable but not writable:
    ``ColdStore.export_batch`` had no production caller at all.
    """
    from akosha.mcp import server as server_mod

    monkeypatch.setenv("AKOSHA_DATA_PATH", str(tmp_path))
    monkeypatch.setenv("AKOSHA_COLD_EXPORT_INTERVAL_SECONDS", "3600")
    monkeypatch.setattr(
        "akosha.observability.prometheus_metrics.generate_metrics", lambda: "metrics"
    )

    cold_store = ColdStoreDouble()
    app = create_app(mode=ColdMode(cold_store))
    async with app._mcp_server.lifespan(app) as context:
        assert context["cold_storage"] is cold_store
        task = server_mod._cold_export_task
        assert task is not None
        assert task.get_name() == "akosha.cold_export"

    # The task is cancelled and cleared on shutdown.
    assert server_mod._cold_export_task is None


@pytest.mark.asyncio
async def test_cold_export_task_can_be_skipped(
    fastmcp_factory: DummyFastMCP,
    patched_lifespan,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """``AKOSHA_SKIP_COLD_EXPORT=1`` suppresses the export cron."""
    from akosha.mcp import server as server_mod

    monkeypatch.setenv("AKOSHA_DATA_PATH", str(tmp_path))
    monkeypatch.setenv("AKOSHA_SKIP_COLD_EXPORT", "1")
    monkeypatch.setattr(
        "akosha.observability.prometheus_metrics.generate_metrics", lambda: "metrics"
    )

    app = create_app(mode=ColdMode(ColdStoreDouble()))
    async with app._mcp_server.lifespan(app):
        assert server_mod._cold_export_task is None


class TestColdExportWiring:
    """The seams *inside* the cold-export wiring.

    ``test_cold_export_task_is_wired`` above pins that the export cron
    starts and that ``AKOSHA_SKIP_COLD_EXPORT=1`` suppresses it. What it
    does NOT pin is the wiring within the lifespan: which age threshold
    the exporter is built with, which warm store it reads, and whether
    the loop actually drives the exporter rather than merely holding a
    task handle.

    Those are the seams F20 is about. A cold exporter pointed at the
    wrong source, or built with a hard-coded threshold that happens to
    match the shipped YAML, would be "wired" in the audit sense and dead
    in the operational sense -- and no test would go red.
    """

    @staticmethod
    def _install_recording_exporter(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        """Record the exporter's construction without faking the exporter.

        The recorder SUBCLASSES the real ``ColdExporter``, so the object
        the lifespan builds is a working exporter over the lifespan's own
        warm store. Only the constructor arguments and the call count are
        captured.
        """
        from akosha.storage import cold_exporter as cold_exporter_mod

        captured: dict[str, Any] = {"calls": 0}

        class RecordingExporter(cold_exporter_mod.ColdExporter):
            """The real exporter, with its construction and its loop recorded."""

            def __init__(self, **kwargs: Any) -> None:
                captured["kwargs"] = kwargs
                super().__init__(**kwargs)
                # The exporter's own state, so a wiring that is rewritten
                # after the call is still visible to the assertions.
                captured["exporter"] = self

            async def export_expired(self, **kwargs: Any) -> Any:
                captured["calls"] += 1
                return await super().export_expired(**kwargs)

        monkeypatch.setattr(cold_exporter_mod, "ColdExporter", RecordingExporter)
        return captured

    @staticmethod
    def _install_recording_aging_service(
        monkeypatch: pytest.MonkeyPatch,
    ) -> list[Any]:
        """Capture the warm store the aging service is given.

        The real ``AgingService`` is replaced: it is not what this test
        is about, and the real one would query the hot store double and
        log a pre-warm failure on every run.
        """
        from akosha.storage import aging as aging_mod

        warm_stores: list[Any] = []

        class RecordingAgingService:
            """Records the warm store the lifespan hands the aging service."""

            def __init__(self, hot_store: Any = None, warm_store: Any = None, **_kw: Any):
                self.hot_store = hot_store
                self.warm_store = warm_store
                warm_stores.append(warm_store)

            async def migrate_hot_to_warm(self, **_kw: Any) -> Any:
                return SimpleNamespace(records_migrated=0, errors=0)

        monkeypatch.setattr(aging_mod, "AgingService", RecordingAgingService)
        return warm_stores

    @pytest.mark.asyncio
    async def test_the_exporter_is_built_with_the_configured_age_threshold(
        self,
        fastmcp_factory: DummyFastMCP,
        patched_lifespan,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:
        """The threshold comes from the config, not from a literal.

        The threshold drives IRREVERSIBLE eviction from the warm tier, so
        a hard-coded default that happens to match the shipped YAML would
        silently ignore the operator's setting. The value asserted here
        (7) is deliberately different from the shipped default (90).
        """
        monkeypatch.setenv("AKOSHA_DATA_PATH", str(tmp_path))
        monkeypatch.setenv("AKOSHA__STORAGE__COLD__AGE_THRESHOLD_DAYS", "7")
        monkeypatch.setattr(
            "akosha.observability.prometheus_metrics.generate_metrics", lambda: "metrics"
        )
        captured = self._install_recording_exporter(monkeypatch)

        app = create_app(mode=ColdMode(ColdStoreDouble()))
        async with app._mcp_server.lifespan(app):
            assert "kwargs" in captured, "the lifespan never built a ColdExporter"
            assert captured["kwargs"]["age_threshold_days"] == 7

    @pytest.mark.asyncio
    async def test_the_exporter_reads_the_warm_store_the_aging_service_writes_to(
        self,
        fastmcp_factory: DummyFastMCP,
        patched_lifespan,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:
        """The cold tier's only source is the warm tier the aging service fills.

        The exporter MUST be handed the lifespan's own warm store: a
        second ``duckdb.connect()`` on the same file from the same
        process would fail on the file lock, and a different store would
        be a different (empty) database.
        """
        monkeypatch.setenv("AKOSHA_DATA_PATH", str(tmp_path))
        monkeypatch.setattr(
            "akosha.observability.prometheus_metrics.generate_metrics", lambda: "metrics"
        )
        captured = self._install_recording_exporter(monkeypatch)
        aging_warm_stores = self._install_recording_aging_service(monkeypatch)

        cold_store = ColdStoreDouble()
        app = create_app(mode=ColdMode(cold_store))
        async with app._mcp_server.lifespan(app):
            assert aging_warm_stores, "the lifespan never built an AgingService"
            # What the lifespan passed...
            assert captured["kwargs"]["warm_store"] is aging_warm_stores[0]
            assert captured["kwargs"]["cold_store"] is cold_store
            # ...and what the exporter actually ended up holding.
            exporter = captured["exporter"]
            assert exporter._warm_store is aging_warm_stores[0]
            assert exporter._cold_store is cold_store

    @pytest.mark.asyncio
    async def test_the_export_loop_actually_moves_warm_rows_to_the_cold_tier(
        self,
        fastmcp_factory: DummyFastMCP,
        patched_lifespan,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:
        """A task handle existing is not the same as the task running.

        The task can exist while the loop body never reaches the
        exporter -- a renamed method, a bad await, a guard that skips
        the call -- and the cold tier would sit empty every cycle. The
        assertion is behavioural: a warm row that is past the configured
        threshold actually reaches the cold store.
        """
        from akosha.mcp import server as server_mod

        monkeypatch.setenv("AKOSHA_DATA_PATH", str(tmp_path))
        monkeypatch.setenv("AKOSHA__STORAGE__COLD__AGE_THRESHOLD_DAYS", "7")
        # A short interval so the loop runs again after the row lands.
        monkeypatch.setenv("AKOSHA_COLD_EXPORT_INTERVAL_SECONDS", "0.05")
        monkeypatch.setattr(
            "akosha.observability.prometheus_metrics.generate_metrics", lambda: "metrics"
        )
        captured = self._install_recording_exporter(monkeypatch)
        aging_warm_stores = self._install_recording_aging_service(monkeypatch)

        cold_store = ColdStoreDouble()
        app = create_app(mode=ColdMode(cold_store))
        async with app._mcp_server.lifespan(app):
            warm_store = aging_warm_stores[0]
            await warm_store.insert(
                WarmRecord(
                    system_id="sys-1",
                    conversation_id="conv-aged",
                    embedding=[1] * 384,
                    summary="First sentence. Second sentence.",
                    timestamp=datetime.now(UTC) - timedelta(days=10),
                    metadata={"task_class": "mcp_tool_call"},
                )
            )
            for _ in range(100):
                if cold_store.exported:
                    break
                await asyncio.sleep(0.02)

            assert captured["calls"] >= 1, "the export loop never called the exporter"
            assert server_mod._cold_export_cycles >= 1
            # The 10-day-old row is past the configured 7-day threshold,
            # so it reached the cold tier.
            assert cold_store.exported, "the aged warm row never reached the cold tier"
            assert cold_store.exported_records == 1
