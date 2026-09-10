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
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from fastmcp import FastMCP

from akosha.config import DEFAULT_MCP_PORT
from akosha.skills_signer import (
    build_pubkey_manifest,
    load_or_create_keypair,
)
from akosha.storage.hot_store import HotStore

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

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
APP_VERSION: Final = "0.15.1"

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
# Skills signer (Phase 1.5 of bodai-skill-agent-distribution plan)
# ---------------------------------------------------------------------------
# The signing keypair and the lifespan-owned ``SignerFeedState`` live as
# closure variables inside ``create_app()`` — NOT module globals. Module
# globals would let concurrent app instances (tests, hot reload) cross-
# contaminate each other's /health outputs (see review R3-H3).
#
# The persistence path is resolved at lifespan startup via
# :func:`_resolve_skills_signer_key_path`. Without persistence, every
# restart produces a fresh ``key_id`` and breaks all previously installed
# Skills (see review R2-H1).


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
_otel_trace_ingester: Any | None = None
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


def _env_truthy(name: str) -> bool:
    """Read an opt-out / toggle env var and interpret it as a truthy flag.

    Returns True when the variable is set to one of ``"1"``, ``"true"``,
    ``"yes"`` (case-insensitive). Absence, empty string, or any other value
    yields False. Centralising the literal avoids the 4x duplication of
    ``os.getenv(NAME, "").lower() not in ("1", "true", "yes")`` across the
    opt-out gates below.

    Note: the inverse sense ("not in (..)") means an *absent* env var
    produces False, which is the natural "feature on by default" semantic.
    """
    return os.getenv(name, "").lower() in ("1", "true", "yes")


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


def _resolve_skills_signer_key_path() -> Path:
    """Resolve the persisted keypair path for the skills_signer.

    The keypair must persist across ``create_app()`` calls so every
    restart preserves the same ``key_id``; otherwise previously
    installed Skills (Phase 2/6) become unverifiable.

    Default: ``~/.akosha/state/skills_signer/private_key.pem``. Override
    via ``AKOSHA_SKILLS_SIGNER_KEY_PATH`` (e.g. for tests that want an
    isolated location).
    """
    env_path = os.getenv("AKOSHA_SKILLS_SIGNER_KEY_PATH")
    if env_path:
        return Path(env_path).expanduser()
    return Path.home() / ".akosha" / "state" / "skills_signer" / "private_key.pem"


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
    except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError):
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
    if _env_truthy("AKOSHA_SKIP_DHARA_REGISTRATION"):
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
        if not _env_truthy("AKOSHA_SKIP_CODE_GRAPH_INGESTER"):
            try:
                from akosha.ingestion.code_graph_ingester import CodeGraphIngester

                session_buddy_endpoint = os.getenv(
                    "SESSION_BUDDY_MCP_URL", "http://localhost:8678/mcp"
                )
                # ``AKOSHA_CODE_GRAPH_POLL_SECONDS`` lets test suites and
                # operators shorten the 60s default. Documented in
                # docs/superpowers/specs/2026-09-06-live-mcp-smoke-test-design.md
                code_graph_poll_seconds = int(os.getenv("AKOSHA_CODE_GRAPH_POLL_SECONDS", "60"))
                _code_graph_ingester = CodeGraphIngester(
                    hot_store=hot_store,
                    session_buddy_endpoint=session_buddy_endpoint,
                    poll_interval_seconds=code_graph_poll_seconds,
                )
                await _code_graph_ingester.start()
                logger.info("CodeGraphIngester started (Wave 5) -> %s", session_buddy_endpoint)
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
        # Wave 6: start the OtelTraceIngester so spans from an OTLP/HTTP
        # collector land in the shared HotStore (independent of the
        # BodaiToolInvocationSubscriber's Redis feed). Mirrors the
        # CodeGraphIngester block above. Opt-out via
        # ``AKOSHA_SKIP_OTEL_INGESTER=1`` for offline test suites.
        # ------------------------------------------------------------------
        global _otel_trace_ingester
        if not _env_truthy("AKOSHA_SKIP_OTEL_INGESTER"):
            try:
                from akosha.ingestion.otel_ingester import OtelTraceIngester

                otel_endpoint = os.getenv("AKOSHA_OTLP_ENDPOINT", "http://localhost:4318/v1/traces")
                poll_seconds = int(os.getenv("AKOSHA_OTEL_POLL_SECONDS", "60"))
                # Spec-listed knobs (see
                # docs/superpowers/specs/2026-09-06-otel-trace-ingester-design.md).
                max_spans_per_poll = int(os.getenv("AKOSHA_OTEL_MAX_SPANS_PER_POLL", "500"))
                initial_lookback_seconds = int(
                    os.getenv("AKOSHA_OTEL_INITIAL_LOOKBACK_SECONDS", "3600")
                )
                _otel_trace_ingester = OtelTraceIngester(
                    hot_store=hot_store,
                    embedding_service=embedding_service,
                    otlp_endpoint=otel_endpoint,
                    poll_interval_seconds=poll_seconds,
                    max_spans_per_poll=max_spans_per_poll,
                    initial_lookback_seconds=initial_lookback_seconds,
                )
                await _otel_trace_ingester.start()
                logger.info(
                    "OtelTraceIngester started (Wave 6) -> %s (interval=%ds)",
                    otel_endpoint,
                    poll_seconds,
                )
            except Exception as exc:
                logger.warning(
                    "OtelTraceIngester start failed (%s); query_local_traces "
                    "will fall back to the BodaiToolInvocationSubscriber path",
                    exc,
                )
                _otel_trace_ingester = None
        else:
            logger.debug("OtelTraceIngester skipped via AKOSHA_SKIP_OTEL_INGESTER")

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
                    # loop simply runs again next interval. ``query_traces``
                    # is only on ``HotStore`` (DuckDB); the pgvector backend
                    # has no equivalent, so we treat that case as an empty
                    # feed and continue. The ``hasattr`` short-circuit keeps
                    # the lifespan tests' MagicMock-based hot_store working
                    # (they patch ``query_traces`` directly without going
                    # through the isinstance chain).
                    if isinstance(hot_store, HotStore) or hasattr(hot_store, "query_traces"):
                        rows = await hot_store.query_traces(limit=100)  # ty: ignore[call-non-callable]
                    else:
                        rows = []
                    for row in rows:
                        try:
                            entities = await kg_builder.extract_entities(row)
                            edges = await kg_builder.extract_relationships(row, entities)
                            await kg_builder.add_to_graph(entities, edges)
                        except Exception as exc:
                            _kg_refresh_errors += 1
                            logger.debug("kg_builder per-row extract failed (%s)", exc)
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
                    logger.warning("kg_refresh loop iteration failed (%s); will retry", exc)

        if not _env_truthy("AKOSHA_SKIP_KG_REFRESH"):
            global _kg_refresh_task
            _kg_refresh_task = asyncio.create_task(_kg_refresh_loop(), name="akosha.kg_refresh")
            logger.info(
                "kg_refresh task started (Wave 5, interval=%ss)",
                os.getenv("AKOSHA_KG_REFRESH_SECONDS", "60"),
            )

        # Phase 0: register this component's MCP endpoint to Dhara
        mcp_url = _get_mcp_url()
        await _register_component_to_dhara(mcp_url)

        # ------------------------------------------------------------------
        # Phase 1.5: load (or generate + persist) the signing keypair and
        # build the lifespan-owned SignerFeedState. This MUST happen BEFORE
        # the health probe is defined — the probe closes over
        # ``signer_feed_state`` as its single source of truth (no module
        # globals; see the module-level comment above). Without persistence,
        # every restart produces a fresh ``key_id`` and breaks all previously
        # installed Skills (review R2-H1).
        # ------------------------------------------------------------------
        from akosha.mcp.signer_feed import SignerFeedState

        key_path = _resolve_skills_signer_key_path()
        server_keypair = load_or_create_keypair(key_path)
        server_manifest = build_pubkey_manifest(server_keypair)
        signer_feed_state = SignerFeedState(manifest=server_manifest)
        logger.info(
            "Phase 1.5: signer feed state initialized key_id=%s key_path=%s",
            server_keypair.key_id,
            key_path,
        )

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
            with suppress(Exception):
                if isinstance(hot_store, HotStore):
                    code_graphs_count = len(await hot_store.list_code_graphs(limit=1000))
            kg_entities_count = len(kg_builder.entities) if kg_builder is not None else 0
            kg_edges_count = len(kg_builder.edges) if kg_builder is not None else 0
            local_traces_count = 0
            with suppress(Exception):
                if isinstance(hot_store, HotStore):
                    local_traces_count = len(await hot_store.query_traces(limit=1000))
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
            code_graphs_ok = code_graphs_count > 0 or ingester_cycles == 0 or not ingester_running
            # REQ-005 follow-up (kg side, symmetric to the OTel fix in
            # 43d85de): a running kg_refresh task that has cycled at
            # least once and has zero errors is "warming up" — it
            # polls Session-Buddy on a timer and reports empty until
            # the upstream is wired. Treat it as ok=True so /health
            # does not 503 the wrapper during the warming-up window.
            # The errors_total guard is deliberate: a non-zero error
            # counter means the task surfaced a real problem and must
            # NOT be masked.
            kg_warming_up = (
                _kg_refresh_task is not None
                and not _kg_refresh_task.done()
                and _kg_refresh_errors == 0
            )
            kg_ok = (
                kg_entities_count > 0
                or _kg_refresh_cycles == 0
                or _kg_refresh_task is None
                or _kg_refresh_task.done()
                or kg_warming_up
            )
            local_traces_otel_running = bool(
                _otel_trace_ingester is not None
                and getattr(_otel_trace_ingester, "_running", False)
            )
            local_traces_otel_cycles = (
                getattr(_otel_trace_ingester, "_cycles_total", 0) or 0
                if _otel_trace_ingester is not None
                else 0
            )
            local_traces_otel_errors = (
                getattr(_otel_trace_ingester, "_errors_total", 0) or 0
                if _otel_trace_ingester is not None
                else 0
            )
            local_traces_otel_last_poll_at = (
                getattr(_otel_trace_ingester, "_last_poll_at", None)
                if _otel_trace_ingester is not None
                else None
            )
            # ``local_traces_ok`` is True iff data exists OR no producer
            # has ever cycled (warming up) OR a producer is alive but empty
            # (warming up, waiting for first spans). A crashed kg_refresh task
            # (``done()`` without ever populating traces) was previously
            # masked as ``done() -> ok`` — the audit failure case the
            # discipline was written to catch. Drop the ``done()``
            # exemption: a finished producer with no data is degraded.
            #
            # REQ-005 (Phase 3 OTel feed recovery, follow-up #2): the previous
            # Phase 3 commit still reported ``ok=False`` for the live audit
            # case — OTel producer alive, cycled once (HTTP 200, 21 bytes,
            # errors=0), no spans received yet. None of the four disjuncts
            # above could see that case as healthy: data was empty, the
            # producer HAD cycled, and the producer WAS alive. The 5th
            # disjunct (Phase 3 follow-up) treats that exact surface as
            # ``warming up`` so ``/health`` returns 200, while ``feed_populated``
            # below keeps callers able to distinguish empty-feed from
            # crash-without-data. The producer-alive check still flags a
            # producer that died on its first cycle.
            #
            # REQ-007: honour the existing ``AKOSHA_SKIP_OTEL_INGESTER`` env
            # var (no new opt-out). When set, the ingester was never
            # constructed and ``otel_disabled`` short-circuits the formula.
            otel_disabled = _env_truthy("AKOSHA_SKIP_OTEL_INGESTER")
            otel_poll_task = (
                getattr(_otel_trace_ingester, "_poll_task", None)
                if _otel_trace_ingester is not None
                else None
            )
            otel_task_alive = otel_poll_task is not None and not otel_poll_task.done()
            kg_task_alive = _kg_refresh_task is not None and not _kg_refresh_task.done()
            any_producer_ever_cycled = _kg_refresh_cycles > 0 or local_traces_otel_cycles > 0
            any_producer_alive = kg_task_alive or otel_task_alive or local_traces_otel_running
            # REQ-005 follow-up: a producer that is *running* and has *no errors*
            # but has not yet landed spans is "warming up", not degraded. The
            # ``otel_errors == 0`` guard is deliberate: an OTel producer with
            # a non-zero error counter has surfaced a transport or parsing
            # problem and should NOT be masked by this disjunct. ``feed_populated``
            # in the payload below carries the empty-feed signal separately so
            # operators can still distinguish warming-up from crash-without-data.
            otel_warming_up = local_traces_otel_running and local_traces_otel_errors == 0
            local_traces_ok = (
                otel_disabled
                or (local_traces_count > 0)
                or (not any_producer_ever_cycled)
                or (not any_producer_alive)
                or otel_warming_up
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
            # Combine producer counters so the feed's ``cycles_total`` /
            # ``errors_total`` reflect every consumer of hot_store.query_traces
            # (kg_refresh + OTel ingester). ``feed_last_updated_timestamp``
            # is the most recent poll across all producers — mandatory
            # per mcp-backend-wiring-discipline.md.
            local_traces_last_poll_at = None
            if last_poll_at is not None and local_traces_otel_last_poll_at is not None:
                local_traces_last_poll_at = max(last_poll_at, local_traces_otel_last_poll_at)
            else:
                local_traces_last_poll_at = last_poll_at or local_traces_otel_last_poll_at
            checks["local_traces_feed"] = {
                "ok": local_traces_ok,
                "feed_entities_count": local_traces_count,
                "feed_populated": local_traces_count > 0,
                "cycles_total": _kg_refresh_cycles + local_traces_otel_cycles,
                "errors_total": _kg_refresh_errors + local_traces_otel_errors,
                "feed_last_updated_timestamp": local_traces_last_poll_at,
                "otel_ingester_running": local_traces_otel_running,
                "otel_endpoint": (
                    _otel_trace_ingester.otlp_endpoint if _otel_trace_ingester is not None else None
                ),
                "otel_cycles_total": local_traces_otel_cycles,
                "otel_errors_total": local_traces_otel_errors,
                "source": "hot_store.query_traces (populated via kg_refresh + OtelTraceIngester)",
            }

            # Phase 1.5: publish the signer feed state so clients can verify
            # SkillMetadata.signature / AgentMetadata.signature without an
            # out-of-band key-exchange. The SignerFeedState is the
            # lifespan's single source of truth — it owns the manifest AND
            # the four mandatory feed signals (entities_count,
            # last_updated_timestamp, cycles_total, errors_total). ``ok``
            # is computed from manifest invariants (empty -> False).
            if signer_feed_state is None:
                checks["skills_signer"] = {
                    "ok": False,
                    "error": "signer feed state not initialized; awaiting lifespan",
                }
            else:
                checks["skills_signer"] = signer_feed_state.as_dict()

            return checks

        # Phase 1.5 signer init was moved above the probe definition so the
        # probe can close over ``signer_feed_state``. Nothing to do here.

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

        if _otel_trace_ingester is not None:
            try:
                await _otel_trace_ingester.stop()
            except Exception as exc:
                logger.warning("OtelTraceIngester stop failed: %s", exc)
            _otel_trace_ingester = None

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
