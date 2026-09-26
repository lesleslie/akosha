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
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, TypedDict, cast

from fastmcp import FastMCP

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
APP_VERSION: Final = "0.17.5"


# ---------------------------------------------------------------------------
# mcp_common.health.aggregator shape shim
# ---------------------------------------------------------------------------
# The source tree of ``mcp-common`` (see /Users/les/Projects/mcp-common
# ``mcp_common/health/aggregator.py``) returns a ``HealthSnapshot``
# TypedDict from ``aggregate_feed_states``. The currently-installed wheel
# (0.26.x) still annotates the return as ``dict[str, object]`` — making
# every ``snap["checks"][name]`` access an error to a precise type
# checker. We mirror the upstream TypedDict contract here so ty (and
# any future checker) sees the right shape, and cast the result at the
# single call site. When mcp-common ships the typed signature, this
# block + the cast can be deleted in one PR.
# ---------------------------------------------------------------------------
if TYPE_CHECKING:
    from mcp_common.health.feed import ReasonCode, StatusValue

    class FeedSnapshot(TypedDict):
        """Per-feed verdict inside :data:`HealthSnapshot.checks`."""

        status: StatusValue
        healthy: bool
        reason_codes: list[ReasonCode]

    class HealthSnapshot(TypedDict):
        """Top-level roll-up returned by :func:`aggregate_feed_states`."""

        status: StatusValue
        checks: dict[str, FeedSnapshot]
        reason_codes: list[ReasonCode]


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
# Phase 4: most-recent error timestamp for the aggregator's time-bounded
# decay predicate. Updated alongside every ``_kg_refresh_errors += 1`` so
# a fresh producer failure flips the aggregate to DEGRADED.
_kg_refresh_last_error_at: float | None = None


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
        7. Initializes the skills-signer feed state (Phase 1.5+1)

        Shutdown sequence:
        1. Flushes OpenTelemetry telemetry
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
            global _kg_refresh_cycles, _kg_refresh_errors, _kg_refresh_last_error_at
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
                            _kg_refresh_last_error_at = time.time()
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
                    _kg_refresh_last_error_at = time.time()
                    logger.warning("kg_refresh loop iteration failed (%s); will retry", exc)

        if not _env_truthy("AKOSHA_SKIP_KG_REFRESH"):
            global _kg_refresh_task
            _kg_refresh_task = asyncio.create_task(_kg_refresh_loop(), name="akosha.kg_refresh")
            logger.info(
                "kg_refresh task started (Wave 5, interval=%ss)",
                os.getenv("AKOSHA_KG_REFRESH_SECONDS", "60"),
            )

        # ------------------------------------------------------------------
        # Phase 1.5 + Phase 1: load (or generate + persist) the signing
        # keypair and build the lifespan-owned SignerFeedState (which
        # now bundles the SkillsSigner for Phase 1 get_skill responses).
        # This MUST happen BEFORE the health probe is defined — the probe
        # closes over ``signer_feed_state`` as its single source of truth
        # (no module globals; see the module-level comment above).
        # Without persistence, every restart produces a fresh
        # ``key_id`` and breaks all previously installed Skills (review
        # R2-H1). The parameterless init_signer_feed_state() helper
        # matches the other Bodai servers so Phase 2's installer and any
        # future Phase 3+ caller can use one helper API across the
        # ecosystem.
        # ------------------------------------------------------------------
        from akosha.mcp.signer_feed import init_signer_feed_state

        signer_feed_state = init_signer_feed_state()
        # ``SignerFeedState.signer`` is Optional so tests can build the
        # dataclass with just a manifest. ``init_signer_feed_state`` is
        # contract-bound to populate it (see ``signer_feed.py``); any
        # None at this call site is an internal invariant violation.
        if signer_feed_state.signer is None:
            msg = "init_signer_feed_state returned SignerFeedState without a signer"
            raise RuntimeError(msg)
        logger.info(
            "Phase 1.5+1: signer feed state initialized key_id=%s",
            signer_feed_state.signer.key_id,
        )

        # Register a default health probe that surfaces the state of every
        # in-process data feed. Phase 4: body delegates per-feed evaluation
        # to ``mcp_common.health.aggregator.aggregate_feed_states`` (plan §5).
        # The aggregator rolls the four data feeds up into a worst-case
        # ``status`` + per-feed verdicts (healthy/warming_up/degraded/failed);
        # the route handler uses that top-level status to decide 200 vs 503.
        # Infrastructure checks (hot_store, embeddings, cold_storage) sit
        # alongside as before — they're not "data feeds" in the aggregator
        # sense and don't fit the HealthFeedState shape.
        async def _default_health_probe() -> dict[str, dict[str, Any]]:
            """Probe every data feed the MCP server depends on.

            Phase 4: per-feed evaluation lives in
            :func:`mcp_common.health.aggregator.aggregate_feed_states`.
            Each data feed (code_graphs, knowledge_graph, local_traces,
            skills_signer) is reduced to a :class:`HealthFeedState`
            snapshot from live producer state; the aggregator rolls them
            up into a worst-case ``status`` + per-feed verdicts.
            Infrastructure checks (hot_store, embeddings, cold_storage)
            sit alongside as before — they don't fit the HealthFeedState
            shape so they stay inline.
            """
            from mcp_common.health.aggregator import aggregate_feed_states
            from mcp_common.health.feed import HealthFeedState

            checks: dict[str, dict[str, Any]] = {}

            # ----------------------------------------------------------
            # Phase 4: build HealthFeedState for each data feed.
            # Live producer state lives on the lifespan-owned singletons
            # (the code-graph ingester and the OTel trace ingester) and
            # the module-level kg_refresh counters.
            # ----------------------------------------------------------
            code_graphs_count = 0
            with suppress(Exception):
                if isinstance(hot_store, HotStore):
                    code_graphs_count = len(await hot_store.list_code_graphs(limit=1000))
            code_graphs_running = bool(
                _code_graph_ingester is not None
                and getattr(_code_graph_ingester, "_running", False)
            )
            code_graphs_state = HealthFeedState(
                entities_count=code_graphs_count,
                last_updated_timestamp=(
                    getattr(_code_graph_ingester, "_last_poll_at", None)
                    if _code_graph_ingester is not None
                    else None
                ),
                cycles_total=(
                    getattr(_code_graph_ingester, "_cycles_total", 0) or 0
                    if _code_graph_ingester is not None
                    else 0
                ),
                errors_total=(
                    getattr(_code_graph_ingester, "_errors_total", 0) or 0
                    if _code_graph_ingester is not None
                    else 0
                ),
                last_error_at=(
                    getattr(_code_graph_ingester, "_last_error_at", None)
                    if _code_graph_ingester is not None
                    else None
                ),
                ingester_running=code_graphs_running,
            )

            kg_entities_count = len(kg_builder.entities) if kg_builder is not None else 0
            kg_edges_count = len(kg_builder.edges) if kg_builder is not None else 0
            kg_running = _kg_refresh_task is not None and not _kg_refresh_task.done()
            knowledge_graph_state = HealthFeedState(
                entities_count=kg_entities_count,
                # kg_refresh doesn't currently record a per-cycle timestamp;
                # leave ``last_updated_timestamp`` as None. Future Phase 4+
                # work can wire a real one if operators need it.
                last_updated_timestamp=None,
                cycles_total=_kg_refresh_cycles,
                errors_total=_kg_refresh_errors,
                last_error_at=_kg_refresh_last_error_at,
                ingester_running=kg_running,
            )

            local_traces_count = 0
            with suppress(Exception):
                if isinstance(hot_store, HotStore):
                    local_traces_count = len(await hot_store.query_traces(limit=1000))
            otel_running = bool(
                _otel_trace_ingester is not None
                and getattr(_otel_trace_ingester, "_running", False)
            )
            otel_cycles = (
                getattr(_otel_trace_ingester, "_cycles_total", 0) or 0
                if _otel_trace_ingester is not None
                else 0
            )
            otel_errors = (
                getattr(_otel_trace_ingester, "_errors_total", 0) or 0
                if _otel_trace_ingester is not None
                else 0
            )
            otel_last_poll = (
                getattr(_otel_trace_ingester, "_last_poll_at", None)
                if _otel_trace_ingester is not None
                else None
            )
            otel_last_error = (
                getattr(_otel_trace_ingester, "_last_error_at", None)
                if _otel_trace_ingester is not None
                else None
            )
            code_graphs_last_poll = (
                getattr(_code_graph_ingester, "_last_poll_at", None)
                if _code_graph_ingester is not None
                else None
            )
            last_poll_candidates = [
                t for t in (code_graphs_last_poll, otel_last_poll) if t is not None
            ]
            local_traces_last_poll = max(last_poll_candidates) if last_poll_candidates else None
            last_error_candidates = [
                t for t in (_kg_refresh_last_error_at, otel_last_error) if t is not None
            ]
            local_traces_last_error = max(last_error_candidates) if last_error_candidates else None
            local_traces_state = HealthFeedState(
                entities_count=local_traces_count,
                last_updated_timestamp=local_traces_last_poll,
                cycles_total=_kg_refresh_cycles + otel_cycles,
                errors_total=_kg_refresh_errors + otel_errors,
                last_error_at=local_traces_last_error,
                ingester_running=(kg_running or otel_running),
            )

            if signer_feed_state is not None:
                manifest_dict = signer_feed_state.manifest.as_dict()
                skills_signer_state = HealthFeedState(
                    entities_count=manifest_dict["key_count"],
                    last_updated_timestamp=signer_feed_state.last_updated_timestamp,
                    cycles_total=signer_feed_state.cycles_total,
                    errors_total=signer_feed_state.errors_total,
                    last_error_at=None,
                    ingester_running=True,
                )
            else:
                skills_signer_state = HealthFeedState(
                    entities_count=0,
                    last_updated_timestamp=None,
                    cycles_total=0,
                    errors_total=0,
                    last_error_at=None,
                    ingester_running=False,
                )

            # Aggregate via mcp-common's canonical aggregator. The
            # halflife is operator-tunable via HEALTH_FEED_HALFLIFE_SECONDS;
            # the Phase 4 spec default is 300s.
            halflife_seconds = int(os.getenv("HEALTH_FEED_HALFLIFE_SECONDS", "300"))
            # Phase 4 observability: time the aggregator call so the
            # ``mcp_common_health_aggregate_duration_ms`` histogram
            # surfaces per-/health p50/p95/p99 latency to operators.
            aggregator_start = time.perf_counter()
            snap = aggregate_feed_states(
                {
                    "code_graphs_feed": code_graphs_state,
                    "knowledge_graph_feed": knowledge_graph_state,
                    "local_traces_feed": local_traces_state,
                    "skills_signer": skills_signer_state,
                },
                halflife_seconds=halflife_seconds,
            )
            aggregator_duration_ms = (time.perf_counter() - aggregator_start) * 1000.0
            # Phase 4 observability: emit the canonical health metrics
            # (plan §4 Observability + §11.4 PromQL alerts) into the
            # shared CollectorRegistry that the existing ``/metrics``
            # endpoint already exposes. The four metrics:
            # ``health_feed_status``, ``health_feed_errors_within_window``,
            # ``mcp_common_health_halflife_seconds``,
            # ``mcp_common_health_aggregate_duration_ms`` — are exactly
            # the names referenced by the PromQL alert rules.
            # Older mcp-common without the metrics module is a
            # forward-compat miss; the body still works without
            # emitting metrics. Operators see the alert rules
            # silently produce no data — the runbook's
            # forward-compat section documents this.
            with suppress(ImportError):
                from mcp_common.health.metrics import update_health_metrics

                from akosha.observability.prometheus_metrics import get_metrics_registry

                update_health_metrics(
                    registry=get_metrics_registry(),
                    snap=snap,
                    repo="akosha",
                    halflife_seconds=halflife_seconds,
                    duration_ms=aggregator_duration_ms,
                )

            # Translate the aggregator's per-feed verdict into the legacy
            # per-feed dict shape. Each entry carries ``ok`` (legacy
            # boolean — True iff status == healthy), ``status`` (the new
            # enum string), ``reason_codes`` (the operator-facing signal),
            # and the four mandatory wire fields. Domain-specific extras
            # are added below per feed.

            def _feed_dict(name: str, state: HealthFeedState) -> dict[str, Any]:
                verdict = snap["checks"][name]
                return {
                    "ok": verdict["healthy"],
                    "status": verdict["status"].value,
                    "reason_codes": [c.value for c in verdict["reason_codes"]],
                    "feed_entities_count": state.entities_count,
                    "feed_last_updated_timestamp": state.last_updated_timestamp,
                    "cycles_total": state.cycles_total,
                    "errors_total": state.errors_total,
                }

            checks["code_graphs_feed"] = _feed_dict("code_graphs_feed", code_graphs_state)
            # Preserve the domain-specific ingester_running boolean on
            # the wire (Phase 5/6 installer surfaces this in tooling).
            checks["code_graphs_feed"]["ingester_running"] = code_graphs_running

            kg_dict = _feed_dict("knowledge_graph_feed", knowledge_graph_state)
            kg_dict["edges_count"] = kg_edges_count
            kg_dict["refresh_task_running"] = kg_running
            checks["knowledge_graph_feed"] = kg_dict

            lt_dict = _feed_dict("local_traces_feed", local_traces_state)
            # Preserve domain-specific OTel visibility fields.
            lt_dict["feed_populated"] = local_traces_count > 0
            lt_dict["otel_ingester_running"] = otel_running
            lt_dict["otel_endpoint"] = (
                _otel_trace_ingester.otlp_endpoint if _otel_trace_ingester is not None else None
            )
            lt_dict["otel_cycles_total"] = otel_cycles
            lt_dict["otel_errors_total"] = otel_errors
            lt_dict["source"] = (
                "hot_store.query_traces (populated via kg_refresh + OtelTraceIngester)"
            )
            checks["local_traces_feed"] = lt_dict

            # Phase 1.5: keep the legacy manifest fields on the wire so
            # tooling that already parses key_count / pubkeys keeps working.
            ss_dict = _feed_dict("skills_signer", skills_signer_state)
            if signer_feed_state is not None:
                ss_dict["feed"] = "skills_signer"
                ss_dict["generation"] = signer_feed_state.generation
                manifest_dict = signer_feed_state.manifest.as_dict()
                ss_dict["key_count"] = manifest_dict["key_count"]
                ss_dict["pubkeys"] = manifest_dict["pubkeys"]
            else:
                ss_dict["error"] = "signer feed state not initialized; awaiting lifespan"
            checks["skills_signer"] = ss_dict

            # ----------------------------------------------------------
            # Infrastructure checks (not data feeds — kept inline).
            # ----------------------------------------------------------

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

            # ----------------------------------------------------------
            # Top-level aggregate verdict for the route handler.
            # ``ok`` is True iff every per-feed verdict is healthy.
            # ----------------------------------------------------------
            all_data_feeds_ok = all(
                snap["checks"][name]["healthy"]
                for name in (
                    "code_graphs_feed",
                    "knowledge_graph_feed",
                    "local_traces_feed",
                    "skills_signer",
                )
            )
            checks["_aggregate"] = {
                "status": snap["status"].value,
                "reason_codes": [c.value for c in snap["reason_codes"]],
                "data_feeds_ok": all_data_feeds_ok,
                "halflife_seconds": halflife_seconds,
            }

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
    # any data feed is degraded/failed or any infrastructure check fails.
    # Phase 4: the route uses the aggregator's top-level ``status`` rather
    # than ``all(c.get("ok"))`` — warming_up is intentionally NOT a 503 so
    # the launchd wrapper sees a 200 during normal startup (plan §5).
    @app.custom_route("/health", methods=["GET"])
    async def health_check(request: Any) -> Any:  # noqa: ARG001
        """HTTP readiness check — 200 only when all data feeds are healthy."""
        # Phase 4c: REQ-005 — every /health body includes the
        # ``launcher`` field so incident responders can grep the
        # canonical launcher version. Per cookbook Trap C, patch the
        # existing handler (do NOT register a duplicate route).
        import mcp_common
        from starlette.responses import JSONResponse

        launcher_field = f"mcp_common.server.launcher@{mcp_common.__version__}"

        if _health_probe_fn is None:
            body = {
                "status": "degraded",
                "service": APP_NAME,
                "version": APP_VERSION,
                "launcher": launcher_field,
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
                "launcher": launcher_field,
                "checks": {"probe": {"ok": False, "error": str(exc)}},
            }
            return JSONResponse(body, status_code=503)

        # Phase 4: derive the worst data-feed status from the aggregator's
        # top-level verdict (``checks["_aggregate"]["status"]``) and
        # separately gate on infrastructure checks (hot_store, embeddings,
        # cold_storage — these lack a ``status`` field and only carry
        # ``ok``). 200 iff every data feed is healthy-or-warming-up AND
        # every infrastructure check is ok.
        worst_status = checks.get("_aggregate", {}).get("status", "healthy")
        status_severity = {
            "healthy": 0,
            "warming_up": 1,
            "degraded": 2,
            "failed": 3,
        }
        worst_severity = status_severity.get(worst_status, 0)
        infra_failure = any(not bool(c.get("ok")) for c in checks.values() if "status" not in c)
        http_ok = (worst_severity < 2) and not infra_failure

        # Body ``status`` mirrors the aggregator's worst-case verdict so
        # callers reading ``/health`` see the same enum string operators
        # see in the per-feed detail (``healthy`` / ``warming_up`` /
        # ``degraded`` / ``failed``). The legacy binary "ok" / "degraded"
        # would lose nuance — a warming_up service is operationally
        # distinct from a degraded one even though both are 200.
        body = {
            "status": worst_status if not infra_failure else "degraded",
            "service": APP_NAME,
            "version": APP_VERSION,
            "launcher": launcher_field,
            "checks": checks,
        }
        return JSONResponse(body, status_code=200 if http_ok else 503)

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
