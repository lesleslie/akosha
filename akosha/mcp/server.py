"""Akosha MCP Server - Universal Memory Aggregation via MCP.

This MCP server exposes Akosha's cross-system memory intelligence capabilities
through the Model Context Protocol (MCP).

Usage:
    python -m akosha.mcp

Example:
    >>> from akosha.mcp import create_app
    >>> app = create_app()
    >>> # Server is now ready to accept MCP connections
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Final, cast

from fastmcp import FastMCP

from akosha.config import DEFAULT_MCP_PORT

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

# Check optional dependencies
try:
    import importlib.util

    MCP_COMMON_AVAILABLE = importlib.util.find_spec("mcp_common.server") is not None
    RATE_LIMITING_AVAILABLE = (
        importlib.util.find_spec("fastmcp.server.middleware.rate_limiting") is not None
    )
    SERVERPANELS_AVAILABLE = importlib.util.find_spec("mcp_common.ui") is not None
except Exception:
    MCP_COMMON_AVAILABLE = False
    RATE_LIMITING_AVAILABLE = False
    SERVERPANELS_AVAILABLE = False

import logging

logger = logging.getLogger(__name__)

APP_NAME: Final = "akosha-mcp"
APP_VERSION: Final = "0.14.7"

DHARA_DEFAULT_URL = "http://localhost:8683"

# ---------------------------------------------------------------------------
# /health probe registration
# ---------------------------------------------------------------------------
# Per the MCP backend wiring discipline (see
# mahavishnu/.claude/decisions/mcp-backend-wiring-discipline.md), every
# registered tool's data feed must expose ``feed.entities_count``,
# ``feed.last_updated_timestamp``, ``feed.errors_total``, ``feed.cycles_total``,
# and ``/health`` must aggregate those feeds and return 503 when any is not
# healthy. The lifespan below registers a default probe at the end of
# initialization; tests (and any custom integrations) can swap the probe out
# via :func:`set_health_probe`.
_health_probe_fn: Callable[[], Awaitable[dict[str, dict[str, Any]]]] | None = None


def set_health_probe(
    probe: Callable[[], Awaitable[dict[str, dict[str, Any]]]] | None,
) -> None:
    """Register (or clear) the probe that ``/health`` will execute.

    The probe returns ``{"feed_name": {"ok": True}}`` (or ``{"ok": False,
    "error": "..."}``). When unset, ``/health`` returns 503 — fail-loud
    default so a misconfigured server can't quietly report healthy.
    """
    global _health_probe_fn
    _health_probe_fn = probe


def get_health_probe() -> Callable[[], Awaitable[dict[str, dict[str, Any]]]] | None:
    """Return the currently-registered health probe (test helper)."""
    return _health_probe_fn


# ---------------------------------------------------------------------------
# Shared service singletons (Wave 5)
# ---------------------------------------------------------------------------
# The W0 ``_apply_tool_profile`` dispatch in this module's lifespan invokes
# per-group register functions (``register_akosha_group``,
# ``register_session_buddy_group``, etc.). Each wrapper constructs its own
# ``HotStore`` via ``create_hot_store()`` because the W0 contract passes only
# the ``app`` instance. When the default backend is ``duckdb-memory``
# (``:memory:`` per-process), each construction produces a fresh in-memory
# database — so data written by lifespan-owned workers (e.g.
# ``CodeGraphIngester``) is invisible to MCP tool handlers.
#
# To close that wire-up gap, the lifespan publishes its initialised
# ``hot_store`` and ``KnowledgeGraphBuilder`` here. Tool-group wrappers read
# from these singletons first; when unset (e.g. in tests that bypass the
# lifespan) they fall back to per-call construction, preserving the
# pre-Wave-5 behaviour.
_shared_hot_store: Any | None = None
_shared_kg_builder: Any | None = None
# Background task handles for the lifespan-owned ingester + graph refresh.
# Storing them here so the shutdown path can cancel them deterministically.
# ``CodeGraphIngester`` owns its own polling task internally; we only hold
# a reference to the instance so we can call ``stop()`` on shutdown.
_code_graph_ingester: Any | None = None
_kg_refresh_task: asyncio.Task[None] | None = None
_kg_refresh_cycles: int = 0
_kg_refresh_errors: int = 0


def set_shared_hot_store(store: Any) -> None:
    """Publish the lifespan-owned HotStore so tool-group wrappers can reuse it.

    Cleared by :func:`clear_shared_services` on lifespan shutdown so the
    next ``create_app()`` call (e.g. test reuse) starts fresh.
    """
    global _shared_hot_store
    _shared_hot_store = store


def get_shared_hot_store() -> Any | None:
    """Return the lifespan-owned HotStore, or ``None`` if unset."""
    return _shared_hot_store


def set_shared_kg_builder(builder: Any) -> None:
    """Publish the lifespan-owned KnowledgeGraphBuilder."""
    global _shared_kg_builder
    _shared_kg_builder = builder


def get_shared_kg_builder() -> Any | None:
    """Return the lifespan-owned KnowledgeGraphBuilder, or ``None``."""
    return _shared_kg_builder


def clear_shared_services() -> None:
    """Clear all singletons. Called from the lifespan teardown."""
    global _shared_hot_store, _shared_kg_builder
    _shared_hot_store = None
    _shared_kg_builder = None


def _get_mcp_url() -> str:
    """Get Akosha's MCP server URL from environment or config.

    Returns:
        MCP server URL string (e.g., "http://localhost:8682/mcp")
    """
    # Check env var first
    mcp_url = os.getenv("AKOSHA_MCP_URL", "")
    if mcp_url:
        return mcp_url

    # Fall back to host + port from config
    host = os.getenv("AKOSHA_HOST", "localhost")
    mcp_port = int(os.getenv("AKOSHA_MCP_PORT", str(DEFAULT_MCP_PORT)))
    return f"http://{host}:{mcp_port}/mcp"


async def _register_to_dhara_once(dhara_url: str, key: str, mcp_url: str) -> str:
    """Single attempt to write component_endpoint/{name} -> mcp_url to Dhara.

    Returns:
        ``"success"`` when Dhara accepted the put; ``"retry"`` for transient
        errors (timeouts, connection refused) that the bounded-retry loop
        in :func:`_register_component_to_dhara` should keep trying; or
        ``"give_up"`` for non-transient errors (HTTP 4xx/5xx) where retrying
        would just waste startup time.
    """
    import httpx2 as httpx

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{dhara_url}/tools/call",
                json={"name": "put", "arguments": {"key": key, "value": mcp_url}},
            )
            response.raise_for_status()
            return "success"
    except httpx.HTTPStatusError:
        # Dhara explicitly rejected the put (4xx/5xx). Retrying won't fix
        # an API bug; bail so lifespan startup doesn't sit in a 31s backoff.
        return "give_up"
    except httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError:
        # Transient — Dhara might come up, the network might recover.
        return "retry"
    except Exception:
        # Unknown failure mode: default to retry so we don't silently drop
        # a fixable hiccup.
        return "retry"


# Module-level task reference so shutdown can cancel the heartbeat loop
_heartbeat_task: asyncio.Task[None] | None = None


async def _register_component_to_dhara(mcp_url: str) -> None:
    """Register Akosha's MCP endpoint to Dhara with retry + periodic heartbeat.

    Key: component_endpoint/akosha
    Value: MCP server URL string

    Phase 1 (startup): retries with exponential backoff (1s, 2s, 4s, 8s, 16s)
    until registration succeeds or max retries are exhausted.
    Phase 2 (heartbeat): re-registers every 5 minutes to keep the TTL fresh.
    Akosha's own FitnessAnalyzer is the consumer of this key — it reads it on
    startup and re-reads it periodically via _populate_component_endpoints_from_dhara.
    """
    import asyncio

    # Test/opt-out escape hatch. The startup retry loop waits ~31s when Dhara
    # is unreachable; without this, every lifespan entry in an offline test
    # suite hangs for 31s. Setting ``AKOSHA_SKIP_DHARA_REGISTRATION=1``
    # short-circuits both Phase 1 (retry) and Phase 2 (heartbeat).
    if os.getenv("AKOSHA_SKIP_DHARA_REGISTRATION", "").lower() in ("1", "true", "yes"):
        logger.debug("Phase 0: skipped via AKOSHA_SKIP_DHARA_REGISTRATION")
        return

    dhara_url = os.getenv("DHARA_MCP_URL", DHARA_DEFAULT_URL)
    key = "component_endpoint/akosha"

    # Phase 1: bounded exponential-backoff retry. The previous code used
    # itertools.count() with a for/else — but itertools.count() is infinite,
    # so the else branch was unreachable, and if Dhara was unreachable this
    # loop blocked the lifespan startup forever. The bug was hidden because
    # the lifespan didn't fire (private-attribute poke no-op'd in FastMCP
    # 3.x); the public-API lifespan fix activates it. Bounding to
    # MAX_STARTUP_ATTEMPTS gives ~31s of startup wait before falling through
    # to the heartbeat (which retries every 5 minutes).
    MAX_STARTUP_ATTEMPTS = 5
    for attempt in range(MAX_STARTUP_ATTEMPTS):
        outcome = await _register_to_dhara_once(dhara_url, key, mcp_url)
        if outcome == "success":
            logger.info(
                "Phase 0: registered akosha endpoint to Dhara: %s -> %s",
                key,
                mcp_url,
            )
            break
        if outcome == "give_up":
            logger.warning(
                "Phase 0: non-retryable registration failure for %s — skipping retries",
                key,
            )
            break
        wait = min(2**attempt, 32)
        logger.debug(
            "Phase 0: registration attempt %d failed, retrying in %ds",
            attempt + 1,
            wait,
        )
        await asyncio.sleep(wait)
    else:
        logger.warning(
            "Phase 0: exhausted %d startup retries for %s — heartbeat will continue",
            MAX_STARTUP_ATTEMPTS,
            key,
        )

    # Phase 2: periodic heartbeat — cancelled on server shutdown via _heartbeat_task
    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(300)  # 5 minutes
            outcome = await _register_to_dhara_once(dhara_url, key, mcp_url)
            if outcome != "success":
                logger.debug("Phase 0 heartbeat: failed to refresh %s", key)

    # Guard against create_app() being called twice without an intervening
    # shutdown (e.g. test fixture reuse, hot-reload). Without this, the prior
    # task's reference is overwritten in the module-level global and the old
    # task leaks, holding the previous dhara_url/key/mcp_url closure until
    # the loop ends.
    global _heartbeat_task
    if _heartbeat_task is not None and not _heartbeat_task.done():
        _heartbeat_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await _heartbeat_task
    _heartbeat_task = asyncio.create_task(heartbeat())


def create_app(mode: Any | None = None) -> FastMCP:
    """Create and configure the FastMCP application.

    This function initializes the FastMCP server with all necessary services,
    including authentication, telemetry, embedding generation, analytics,
    and knowledge graph capabilities. The server uses a custom lifespan
    manager to handle startup and shutdown sequences.

    Args:
        mode: Optional mode instance (LiteMode, StandardMode) controlling
            service initialization behavior. If None, uses default behavior.

    The initialization process includes:
    1. Authentication configuration validation
    2. OpenTelemetry setup for observability
    3. Embedding service initialization (with graceful fallback)
    4. Time-series analytics service initialization
    5. Knowledge graph builder initialization
    6. MCP tool registration

    Returns:
        FastMCP: Configured FastMCP application instance ready to serve
            MCP requests. The app includes all registered tools and
            middleware.

    Raises:
        RuntimeError: If authentication configuration validation fails.
            This ensures the server doesn't start with invalid security
            settings.

    Example:
        >>> app = create_app()
        >>> # Use with MCP client
        >>> async with app.get_client() as client:
        ...     result = await client.call_tool("search_all_systems", {...})
    """
    # Capture mode for lifespan closure. Must be set before the lifespan
    # is defined since the lifespan closes over this local.
    mode_instance = mode

    # Lifespan is defined BEFORE the FastMCP constructor so it can be
    # passed via the public `lifespan=` kwarg. The previous code defined
    # `lifespan` as a closure AFTER the constructor and then assigned it
    # via `app._mcp_server.lifespan = lifespan` — a private-API attribute
    # poke that silently no-ops in FastMCP 3.x, which caused Claude Code's
    # transport auto-detector to fall back to REST-style /mcp/tools/call
    # routing (returns 404, since only /mcp is mounted). Using the public
    # constructor kwarg ensures the lifespan is captured by FastMCP's
    # _lifespan_proxy correctly.
    @asynccontextmanager
    async def lifespan(server: Any) -> AsyncGenerator[dict[str, Any]]:
        """Custom lifespan manager for Akosha.

        Manages the complete lifecycle of the Akosha MCP server, including
        service initialization, health checks, and graceful shutdown.

        Startup sequence:
        1. Validates authentication configuration
        2. Initializes OpenTelemetry tracing and metrics
        3. Initializes embedding service (with graceful fallback)
        4. Initializes analytics service
        5. Initializes knowledge graph builder
        6. Registers all MCP tools (using the framework-passed `server` param)
        7. Phase 0: registers MCP endpoint to Dhara

        Shutdown sequence:
        1. Cancels the heartbeat task created in Phase 0
        2. Flushes OpenTelemetry telemetry
        3. Logs shutdown completion

        Args:
            server: FastMCP server instance, passed by the framework. This is
                the same object as the FastMCP instance returned by create_app();
                we use it to register tools instead of capturing `app` from
                the enclosing closure (which doesn't exist yet at definition time
                when the lifespan is defined above the constructor).

        Yields:
            dict[str, Any]: Context dictionary containing initialized services.

        Raises:
            RuntimeError: If authentication configuration is invalid.
        """
        logger.info(f"{APP_NAME} v{APP_VERSION} starting up")

        # Validate authentication configuration
        from akosha.mcp.auth import validate_auth_config

        try:
            validate_auth_config()
        except ValueError as e:
            logger.error(f"Authentication configuration error: {e}")
            raise RuntimeError(f"Authentication configuration failed: {e}") from e

        # Initialize OpenTelemetry
        from akosha.observability import setup_telemetry, shutdown_telemetry

        environment = os.getenv("ENVIRONMENT", "development")
        otlp_endpoint = os.getenv("OTLP_ENDPOINT", None)

        tracer, meter = setup_telemetry(
            service_name="akosha-mcp",
            environment=environment,
            otlp_endpoint=otlp_endpoint,
            enable_console_export=(environment == "development"),
            sample_rate=1.0 if environment == "development" else 0.1,
        )
        logger.info("OpenTelemetry tracing initialized")

        # Initialize Phase 2 services
        from akosha.processing.analytics import TimeSeriesAnalytics
        from akosha.processing.embeddings import get_embedding_service
        from akosha.storage import create_hot_store

        # Check if we're in lite mode
        is_lite_mode = (
            mode_instance is not None
            and hasattr(mode_instance, "requires_external_services")
            and not mode_instance.requires_external_services
        )

        if is_lite_mode:
            logger.info("Running in lite mode - using minimal services")
        else:
            logger.info("Running in standard mode - using full services")

        # Initialize cache layer based on mode
        if mode_instance is not None and hasattr(mode_instance, "initialize_cache"):
            cache_client = await mode_instance.initialize_cache()
        else:
            cache_client = None

        embedding_service = get_embedding_service()
        await embedding_service.initialize()
        logger.info(
            f"Embedding service initialized: "
            f"{'real' if embedding_service.is_available() else 'fallback'} mode"
        )

        # In lite mode, skip analytics services
        if not is_lite_mode:
            analytics_service = TimeSeriesAnalytics()
            logger.info("Time-series analytics service initialized")
        else:
            analytics_service = None

        # Initialize hot store using factory (Task 1.1a: wire PgvectorHotStore)
        hot_store = create_hot_store()
        await hot_store.initialize()
        logger.info(
            "Hot store initialized (%s)",
            type(hot_store).__name__,
        )

        # Initialize cold storage if available
        if mode_instance is not None and hasattr(mode_instance, "initialize_cold_storage"):
            cold_storage = await mode_instance.initialize_cold_storage()
        else:
            cold_storage = None

        # Apply ToolProfile dispatch via the W0 helper from mcp-common 0.18.0.
        #
        # Replaces the legacy ``register_all_tools`` path (which read
        # AKOSHA_TOOL_PROFILE directly and dispatched per-group manually).
        # PROFILE_REGISTRATIONS routes the per-tier lists to per-group
        # callables in REGISTRATION_MAP; AKOSHA_MANDATORY_GROUPS guarantees
        # health probes are reachable from any profile tier.
        from mcp_common.tools.dispatch import _apply_tool_profile

        from akosha.mcp.tools.profiles import (
            AKOSHA_MANDATORY_GROUPS,
            PROFILE_REGISTRATIONS,
            REGISTRATION_MAP,
        )

        await _apply_tool_profile(
            server,
            profile_env_var="AKOSHA_TOOL_PROFILE",
            registrations=cast("Any", PROFILE_REGISTRATIONS),
            registration_map=REGISTRATION_MAP,
            register_all_fn=None,
            mandatory_groups=AKOSHA_MANDATORY_GROUPS,
            essential_tool_names=set(),
            discovery_fn=None,
            yaml_loader=None,
        )

        # ------------------------------------------------------------------
        # Wave 5: publish the lifespan-owned HotStore so the per-group
        # tool wrappers (which each call ``create_hot_store()`` themselves)
        # reuse this instance instead of producing a fresh in-memory DuckDB
        # database per call. Without this, data written by the CodeGraphIngester
        # below is invisible to ``list_ingested_code_graphs`` /
        # ``get_graph_statistics`` / ``query_local_traces``.
        # ------------------------------------------------------------------
        set_shared_hot_store(hot_store)

        # ------------------------------------------------------------------
        # Wave 5: publish the lifespan-owned KnowledgeGraphBuilder so the
        # ``register_akosha_group`` wrapper reuses it. Construct it once
        # here so the periodic-refresh task (below) can write into the same
        # instance the tools read from via ``get_graph_statistics``.
        # ------------------------------------------------------------------
        from akosha.processing.knowledge_graph import KnowledgeGraphBuilder

        kg_builder = KnowledgeGraphBuilder()
        set_shared_kg_builder(kg_builder)
        logger.info("Knowledge graph builder initialised (Wave 5)")

        # ------------------------------------------------------------------
        # Wave 5: start the CodeGraphIngester so ingested code graphs land in
        # the shared HotStore. The class has a complete start/stop/polling
        # lifecycle that was never wired anywhere (see Wave-5 spec Appendix A,
        # finding A.3). Polling interval defaults to 60s; opt-out via
        # ``AKOSHA_SKIP_CODE_GRAPH_INGESTER=1`` for offline test suites.
        # ------------------------------------------------------------------
        global _code_graph_ingester
        if os.getenv("AKOSHA_SKIP_CODE_GRAPH_INGESTER", "").lower() not in ("1", "true", "yes"):
            try:
                from akosha.ingestion.code_graph_ingester import CodeGraphIngester

                session_buddy_endpoint = os.getenv(
                    "SESSION_BUDDY_MCP_URL", "http://localhost:8678/mcp"
                )
                _code_graph_ingester = CodeGraphIngester(
                    hot_store=hot_store,
                    session_buddy_endpoint=session_buddy_endpoint,
                )
                await _code_graph_ingester.start()
                logger.info(
                    "CodeGraphIngester started (Wave 5) -> %s", session_buddy_endpoint
                )
            except Exception as exc:
                logger.warning(
                    "CodeGraphIngester start failed (%s); search_code_patterns "
                    "will fall back to PyCharm HTTP path",
                    exc,
                )
                _code_graph_ingester = None
        else:
            logger.debug("CodeGraphIngester skipped via AKOSHA_SKIP_CODE_GRAPH_INGESTER")

        # ------------------------------------------------------------------
        # Wave 5: start the knowledge-graph periodic-refresh task. Every
        # ``AKOSHA_KG_REFRESH_SECONDS`` (default 60), pull the most recent
        # traces from HotStore and feed them to the kg_builder as
        # ``conversation`` records. opt-out via
        # ``AKOSHA_SKIP_KG_REFRESH=1``.
        # ------------------------------------------------------------------

        async def _kg_refresh_loop() -> None:
            global _kg_refresh_cycles, _kg_refresh_errors
            try:
                interval = float(os.getenv("AKOSHA_KG_REFRESH_SECONDS", "60"))
            except ValueError:
                interval = 60.0
            while True:
                try:
                    await asyncio.sleep(interval)
                    _kg_refresh_cycles += 1
                    # ``query_traces`` is the consumer API for traces landed
                    # via the BodaiToolInvocationSubscriber. It returns
                    # ``[]`` when the subscriber is disabled or no events
                    # have arrived — both are valid empty-feed states; the
                    # loop simply runs again next interval.
                    rows = await hot_store.query_traces(limit=100)
                    for row in rows:
                        try:
                            entities = await kg_builder.extract_entities(row)
                            edges = await kg_builder.extract_relationships(row, entities)
                            await kg_builder.add_to_graph(entities, edges)
                        except Exception as exc:
                            _kg_refresh_errors += 1
                            logger.debug(
                                "kg_builder per-row extract failed (%s)", exc
                            )
                    if rows:
                        logger.info(
                            "kg_refresh: cycle=%d rows=%d entities=%d edges=%d",
                            _kg_refresh_cycles,
                            len(rows),
                            len(kg_builder.entities),
                            len(kg_builder.edges),
                        )
                except asyncio.CancelledError:
                    logger.info(
                        "kg_refresh loop cancelled after %d cycles (%d errors)",
                        _kg_refresh_cycles,
                        _kg_refresh_errors,
                    )
                    break
                except Exception as exc:
                    _kg_refresh_errors += 1
                    logger.warning(
                        "kg_refresh loop iteration failed (%s); will retry", exc
                    )

        if os.getenv("AKOSHA_SKIP_KG_REFRESH", "").lower() not in ("1", "true", "yes"):
            global _kg_refresh_task
            _kg_refresh_task = asyncio.create_task(
                _kg_refresh_loop(), name="akosha.kg_refresh"
            )
            logger.info(
                "kg_refresh task started (Wave 5, interval=%ss)",
                os.getenv("AKOSHA_KG_REFRESH_SECONDS", "60"),
            )

        # Phase 0: register this component's MCP endpoint to Dhara
        mcp_url = _get_mcp_url()
        await _register_component_to_dhara(mcp_url)

        # Register a default health probe that surfaces the state of every
        # in-process data feed. Per mcp-backend-wiring-discipline.md, /health
        # returns 503 unless every feed reports ``ok=True``.
        async def _default_health_probe() -> dict[str, dict[str, Any]]:
            """Probe every data feed the MCP server depends on."""
            checks: dict[str, dict[str, Any]] = {}

            # Hot store: ping if available, otherwise just check it exists.
            if hot_store is None:
                checks["hot_store"] = {"ok": False, "error": "not initialized"}
            else:
                ping = getattr(hot_store, "ping", None)
                if callable(ping):
                    try:
                        await ping()
                        checks["hot_store"] = {"ok": True}
                    except Exception as exc:
                        checks["hot_store"] = {"ok": False, "error": str(exc)}
                else:
                    checks["hot_store"] = {"ok": True}

            # Embedding service: report fallback-mode as a soft warning but
            # still healthy (the server works without real embeddings).
            if embedding_service is None:
                checks["embeddings"] = {"ok": False, "error": "not initialized"}
            else:
                is_avail = embedding_service.is_available()
                checks["embeddings"] = {
                    "ok": True,
                    "mode": "real" if is_avail else "fallback",
                }

            # Cold storage: optional; ``None`` means the mode disabled it.
            checks["cold_storage"] = (
                {"ok": True}
                if cold_storage is not None
                else {"ok": True, "note": "disabled in current mode"}
            )

            # Wave 5: per-feed aggregates. Each feed reports entities_count
            # (the size of the feed), cycles_total (how many refresh ticks
            # have run since startup), errors_total (how many ticks failed),
            # and last_updated_timestamp. ``ok`` is False when the feed is
            # empty AND the ingester/refresh has run at least one cycle —
            # that surfaces wire-up drift that would otherwise be invisible.
            code_graphs_count = 0
            try:
                if hot_store is not None and hasattr(hot_store, "list_code_graphs"):
                    code_graphs_count = len(await hot_store.list_code_graphs(limit=1000))
            except Exception:
                pass
            kg_entities_count = len(kg_builder.entities) if kg_builder is not None else 0
            kg_edges_count = len(kg_builder.edges) if kg_builder is not None else 0
            local_traces_count = 0
            try:
                if hot_store is not None and hasattr(hot_store, "query_traces"):
                    local_traces_count = len(await hot_store.query_traces(limit=1000))
            except Exception:
                pass
            ingester_running = bool(
                _code_graph_ingester is not None
                and getattr(_code_graph_ingester, "_running", False)
            )
            ingester_cycles = getattr(_code_graph_ingester, "_cycles_total", 0) or 0
            last_poll_at = (
                getattr(_code_graph_ingester, "_last_poll_at", None)
                if _code_graph_ingester is not None
                else None
            )
            # ``ok`` is False iff the feed is empty AND the producer has
            # run at least one cycle. The empty-feed case while still
            # warming up (``cycles == 0``) is intentionally True so the
            # probe doesn't fail during normal startup.
            code_graphs_ok = (
                code_graphs_count > 0
                or ingester_cycles == 0
                or not ingester_running
            )
            kg_ok = (
                kg_entities_count > 0
                or _kg_refresh_cycles == 0
                or _kg_refresh_task is None
                or _kg_refresh_task.done()
            )
            local_traces_ok = (
                local_traces_count > 0
                or _kg_refresh_cycles == 0
                or _kg_refresh_task is None
                or _kg_refresh_task.done()
            )
            checks["code_graphs_feed"] = {
                "ok": code_graphs_ok,
                "ingester_running": ingester_running,
                "feed_entities_count": code_graphs_count,
                "cycles_total": ingester_cycles,
                "errors_total": getattr(_code_graph_ingester, "_errors_total", 0) or 0,
                "feed_last_updated_timestamp": last_poll_at,
            }
            checks["knowledge_graph_feed"] = {
                "ok": kg_ok,
                "feed_entities_count": kg_entities_count,
                "edges_count": kg_edges_count,
                "cycles_total": _kg_refresh_cycles,
                "errors_total": _kg_refresh_errors,
                "refresh_task_running": _kg_refresh_task is not None
                and not _kg_refresh_task.done(),
            }
            checks["local_traces_feed"] = {
                "ok": local_traces_ok,
                "feed_entities_count": local_traces_count,
                "cycles_total": _kg_refresh_cycles,
                "errors_total": _kg_refresh_errors,
                "source": "hot_store.query_traces (populated via BodaiToolInvocationSubscriber)",
            }

            return checks

        set_health_probe(_default_health_probe)

        yield {
            "akosha_ready": True,
            "embedding_service": embedding_service,
            "analytics_service": analytics_service,
            "tracer": tracer,
            "meter": meter,
            "mode": "lite" if is_lite_mode else "standard",
            "cache_client": cache_client,
            "cold_storage": cold_storage,
        }

        # Shutdown: cancel the heartbeat task created by
        # _register_component_to_dhara. cancel() is fire-and-forget — must
        # await to ensure the task has finished its cleanup (closing the
        # httpx client) before the lifespan returns and uvicorn tears down.
        global _heartbeat_task
        if _heartbeat_task is not None and not _heartbeat_task.done():
            _heartbeat_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await _heartbeat_task
            _heartbeat_task = None

        # Wave 5: cancel the kg_refresh task and stop the CodeGraphIngester
        # before the lifespan returns. Order matters: cancel kg_refresh
        # first (it reads from hot_store), then stop CodeGraphIngester
        # (it writes to hot_store), then clear the singletons so the
        # next create_app() starts fresh.
        # NB: ``_kg_refresh_task`` and ``_code_graph_ingester`` are both
        # declared global at their assignment sites above (lines ~497 and
        # ~576 respectively); no re-declaration is needed here.
        if _kg_refresh_task is not None and not _kg_refresh_task.done():
            _kg_refresh_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await _kg_refresh_task
            _kg_refresh_task = None

        if _code_graph_ingester is not None:
            try:
                await _code_graph_ingester.stop()
            except Exception as exc:
                logger.warning("CodeGraphIngester stop failed: %s", exc)
            _code_graph_ingester = None

        # Reset the per-feed cycle/error counters so the next create_app()
        # call starts from zero. Without this, the probe would carry stale
        # cycle counts across lifespans in tests.
        global _kg_refresh_cycles, _kg_refresh_errors
        _kg_refresh_cycles = 0
        _kg_refresh_errors = 0

        clear_shared_services()

        # Clear the health probe so the next ``create_app()`` call sees a
        # fresh, unregistered state. Without this, a previous lifespan's
        # probe leaks into the next test and ``/health`` reports ``ok``
        # instead of the expected ``degraded`` (no probe registered) before
        # the new lifespan's init runs.
        set_health_probe(None)

        # Shutdown telemetry (synchronous call, no await needed)
        shutdown_telemetry()
        logger.info(f"{APP_NAME} shutdown complete")

    # Construct FastMCP with the public `lifespan=` kwarg. This is the fix
    # for the routing-404 bug: the previous code assigned lifespan via
    # `app._mcp_server.lifespan = lifespan` AFTER the constructor, which
    # FastMCP 3.x silently drops because the internal Server captures its
    # own lifespan reference at __init__ time.
    app = FastMCP(
        name=APP_NAME,
        version=APP_VERSION,
        lifespan=lifespan,
    )

    # HTTP health endpoint for Claude Code compatibility.
    # Aggregates per-feed state via the registered probe; returns 503 when
    # any feed is degraded or no probe is registered (fail-loud default).
    @app.custom_route("/health", methods=["GET"])
    async def health_check(request: Any) -> Any:  # noqa: ARG001
        """HTTP readiness check — 200 only when all data feeds are healthy."""
        from starlette.responses import JSONResponse

        if _health_probe_fn is None:
            body = {
                "status": "degraded",
                "service": APP_NAME,
                "version": APP_VERSION,
                "checks": {
                    "probe": {
                        "ok": False,
                        "error": "no health probe registered (lifespan not run?)",
                    }
                },
            }
            return JSONResponse(body, status_code=503)

        try:
            checks = await _health_probe_fn()
        except Exception as exc:  # probe raised — surface as degraded
            body = {
                "status": "degraded",
                "service": APP_NAME,
                "version": APP_VERSION,
                "checks": {"probe": {"ok": False, "error": str(exc)}},
            }
            return JSONResponse(body, status_code=503)

        all_ok = all(bool(c.get("ok")) for c in checks.values())
        body = {
            "status": "ok" if all_ok else "degraded",
            "service": APP_NAME,
            "version": APP_VERSION,
            "checks": checks,
        }
        return JSONResponse(body, status_code=200 if all_ok else 503)

    @app.custom_route("/healthz", methods=["GET"])
    async def healthz_check(request: Any) -> Any:  # noqa: ARG001
        """Kubernetes-style health check endpoint."""
        from starlette.responses import JSONResponse

        return JSONResponse({"status": "ok"})

    @app.custom_route("/metrics", methods=["GET"])
    async def metrics(request: Any) -> Any:  # noqa: ARG001
        """Canonical Prometheus metrics endpoint on the main HTTP port."""
        from starlette.responses import Response

        from akosha.observability.prometheus_metrics import generate_metrics

        return Response(
            content=generate_metrics(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    logger.debug("Akosha MCP server created")
    return app


def __getattr__(name: str) -> Any:
    """Lazy app initialization.

    Provides lazy initialization pattern for the app instance, deferring
    expensive setup until first access. This enables fast module imports
    while still providing convenient `app` attribute access.

    Args:
        name: Attribute name to access. Supported values:
            - "app": Returns the FastMCP application instance
            - "http_app": Returns the ASGI HTTP application wrapper

    Returns:
        Any: The requested attribute value (FastMCP app or HTTP app).

    Raises:
        AttributeError: If the requested attribute is not supported.
            Only "app" and "http_app" are valid attribute names.

    Example:
        >>> from akosha.mcp import app
        >>> # App is created on first access
        >>> tools = await app.list_tools()
    """
    if name == "app":
        return create_app()
    if name == "http_app":
        return create_app().http_app()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


__all__ = [
    "APP_NAME",
    "APP_VERSION",
    "MCP_COMMON_AVAILABLE",
    "RATE_LIMITING_AVAILABLE",
    "SERVERPANELS_AVAILABLE",
    "create_app",
]
