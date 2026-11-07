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

from akosha import __version__
from akosha.mcp.oneiric_runtime import report_runtime_to_oneiric
from akosha.observability.otlp_receiver import OTLPReceiver
from akosha.storage.hot_store import HotStore

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from akosha.storage.warm_store import WarmStore

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
APP_VERSION: Final[str] = __version__


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
    from mcp_common.health.feed import HealthFeedState, ReasonCode, StatusValue

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


# Process-start timestamp used to derive ``uptime_seconds`` in the
# canonical health body. Captured at module-import time, which is the
# closest observable proxy for process start; ``akosha.mcp.server``
# is imported during the first call to ``create_app()`` so the value
# is stable for the lifetime of the running MCP server.
_HEALTH_START_TIME: float = time.time()


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


async def _compute_health_body_and_status() -> tuple[dict[str, Any], int]:
    """Compute the canonical health body and HTTP status.

    Per ``mcp-backend-wiring-discipline.md`` the HTTP ``/health`` route
    AND the MCP ``get_health()`` tool MUST return identical verdicts.
    Routing both surfaces through this helper makes divergence
    structurally impossible — there is exactly one source of truth for
    ``status``, ``checks``, ``version``, ``launcher``, ``uptime_seconds``,
    and the HTTP status code.

    Returns:
        Tuple ``(body, http_status_code)`` where ``body`` is the canonical
        HealthSnapshot-shaped dict and ``http_status_code`` is the
        status the HTTP route should set (200 for healthy/warming_up,
        503 otherwise). MCP tools always return ``body`` — they discard
        the status (MCP wire is status-agnostic for tool results).
    """
    import mcp_common

    launcher_field = f"mcp_common.server.launcher@{mcp_common.__version__}"
    service_info = {"service": APP_NAME, "version": APP_VERSION}

    if _health_probe_fn is None:
        # Fail-loud default: a server that never had its lifespan run
        # must NOT silently report healthy. Mirrors the prior /health
        # behaviour; preserved verbatim so existing 503 callers keep
        # working.
        body = {
            "status": "degraded",
            **service_info,
            "launcher": launcher_field,
            "checks": {
                "probe": {
                    "ok": False,
                    "error": "no health probe registered (lifespan not run?)",
                }
            },
            "http_status": 503,
            "uptime_seconds": time.time() - _HEALTH_START_TIME,
        }
        return body, 503

    try:
        checks = await _health_probe_fn()
    except Exception as exc:
        # Probe contract is "return a dict" but tools raise; surface
        # the exception as a degraded verdict rather than 500ing the
        # /health route (the load balancer would mark every pod down
        # on an unhandled probe exception, which makes outages worse).
        body = {
            "status": "degraded",
            **service_info,
            "launcher": launcher_field,
            "checks": {"probe": {"ok": False, "error": str(exc)}},
            "http_status": 503,
            "uptime_seconds": time.time() - _HEALTH_START_TIME,
        }
        return body, 503

    # Phase 4 verdict: worst data-feed status from ``_aggregate`` +
    # independent infra-failure gate (hot_store, embeddings, cold_storage
    # carry ``ok`` not ``status``). 200 iff every data feed is
    # healthy-or-warming_up AND every infrastructure probe is ok.
    worst_status = checks.get("_aggregate", {}).get("status", "healthy")
    status_severity = {
        "healthy": 0,
        "warming_up": 1,
        "degraded": 2,
        "failed": 3,
    }
    worst_severity = status_severity.get(worst_status, 0)
    infra_failure = any(
        not bool(c.get("ok"))
        for c in checks.values()
        if "status" not in c  # only infrastructure checks lack ``status``
    )
    http_ok = (worst_severity < 2) and not infra_failure
    status_label = worst_status if not infra_failure else "degraded"
    http_status_code = 200 if http_ok else 503

    body = {
        "status": status_label,
        **service_info,
        "launcher": launcher_field,
        "checks": checks,
        "http_status": http_status_code,
        "uptime_seconds": time.time() - _HEALTH_START_TIME,
    }
    return body, http_status_code


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
# The lifespan-owned OTLP receiver. Untyped like its two siblings so importing
# this module never drags in gRPC and the proto descriptors — server.py is
# imported by every akosha test that touches the MCP surface.
_shared_otel_receiver: Any | None = None
# Background task handles for the lifespan-owned ingester + graph refresh.
# Storing them here so the shutdown path can cancel them deterministically.
# ``CodeGraphIngester`` owns its own polling task internally; we only hold
# a reference to the instance so we can call ``stop()`` on shutdown.
_code_graph_ingester: Any | None = None
_otel_trace_ingester: Any | None = None
# The lifespan-owned event-subscriber stack (Redis-Streams ``bodai:events``
# -> HotStore). This is the ONLY producer of the ``conversations`` table in
# the MCP process, and therefore the only source of rows for the
# knowledge-graph refresh loop. Held at module scope so the shutdown path
# can stop it deterministically and the /health probe can report whether a
# producer is actually running.
_event_subscriber: Any | None = None
_event_subscriber_running: bool = False
_event_subscriber_enabled: bool = False
_kg_refresh_task: asyncio.Task[None] | None = None
_kg_refresh_cycles: int = 0
_kg_refresh_errors: int = 0
# Phase 4: most-recent error timestamp for the aggregator's time-bounded
# decay predicate. Updated alongside every ``_kg_refresh_errors += 1`` so
# a fresh producer failure flips the aggregate to DEGRADED.
_kg_refresh_last_error_at: float | None = None
# Phase 2 (REQ-FEED-002): AgingService cron lifecycle. ``_aging_task``
# is the lifespan-owned background coroutine that periodically migrates
# aged records from HotStore to WarmStore so the mcp_tool_call feed
# doesn't grow the hot tier without bound. The module-level cycle/
# error/last_run_at counters mirror the kg_refresh pattern so the
# /health ``aging_feed`` can surface them via HealthFeedState.
_aging_task: asyncio.Task[None] | None = None
_aging_cycles: int = 0
_aging_errors: int = 0
_aging_last_run_at: float | None = None
# Cold tier (F5): the warm->cold export cron. ``_cold_export_task`` is the
# lifespan-owned background coroutine that periodically ages warm-tier
# records into the cold tier as Parquet batches. Without it the cold tier
# is reachable but not writable -- ``ColdStore.export_batch`` had no
# production caller at all.
_cold_export_task: asyncio.Task[None] | None = None
_cold_export_cycles: int = 0
_cold_export_errors: int = 0
_cold_export_last_run_at: float | None = None
# Phase 3 (REQ-RUNTIME-001): fire-and-forget Oneiric catalog push.
# The coroutine is fail-soft by contract, but we still hold the Task
# handle at module scope so the GC does not collect it before the
# push coroutine completes (RUF006).
_oneiric_push_task: asyncio.Task[None] | None = None


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


def set_shared_otel_receiver(receiver: Any) -> None:
    """Publish the lifespan-owned OTLPReceiver.

    A 1:1 mirror of :func:`set_shared_hot_store`. The receiver is not
    reachable from a tool otherwise: it borrows the lifespan's HotStore and
    has no reference back to the app, so a health probe or an MCP tool that
    wants to report ingest counters has nothing to read.

    Cleared by :func:`clear_shared_services` on shutdown. Leaving it set would
    hand the next ``create_app()`` a receiver whose listeners are closed —
    one that answers every status query and binds nothing.
    """
    global _shared_otel_receiver
    _shared_otel_receiver = receiver


def get_shared_otel_receiver() -> Any | None:
    """Return the lifespan-owned OTLPReceiver, or ``None`` if unset."""
    return _shared_otel_receiver


def clear_shared_services() -> None:
    """Clear all singletons. Called from the lifespan teardown."""
    global _shared_hot_store, _shared_kg_builder, _shared_otel_receiver
    _shared_hot_store = None
    _shared_kg_builder = None
    _shared_otel_receiver = None


def _receiver_port(kind: str) -> int:
    """Resolve the OTLP listener port from the lifespan's ``SpansSubstrateConfig``.

    Defaults to the canonical :4317 / :4318 the substrate owns per the v7
    port-ownership decision. Overridable because a second akosha on one host
    (tests, a staging box) cannot bind the canonical ports, and a hardcoded
    value would make that case fail with EADDRINUSE rather than with a
    message naming the setting to set.

    Reads the default off ``SpansSubstrateConfig`` rather than off
    ``OTLPReceiver.GRPC_PORT`` so this resolver does not depend on the
    receiver class existing — the config model is the single source of truth
    for ports, and a resolver that reached through the receiver would break
    the moment anything substitutes a double for it. A test pins that the two
    defaults agree, so they cannot drift.
    """
    from akosha.config import SpansSubstrateConfig

    config = _lifespan_spans_config
    if config is None:
        config = SpansSubstrateConfig()
    field = "receiver_grpc_port" if kind == "grpc" else "receiver_http_port"
    return int(getattr(config, field))


# Set by ``create_app`` from the loaded config; ``None`` until the lifespan
# runs, which is exactly when ``_receiver_port`` should return the defaults.
_lifespan_spans_config: Any | None = None


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


def _epoch_or_none(value: Any) -> float | None:
    """Return ``value`` if it is a real number, else ``None``.

    The health aggregator compares timestamps with ``max()``, so a
    non-numeric value is not merely ugly -- it raises, and the whole health
    payload loses its ``_aggregate``. That is how a mock attribute leaking
    into ``last_batch_at`` took the entire aggregate down: the exception was
    caught upstream and the aggregate key was simply absent from the
    response, which reads as "no aggregate computed" rather than "a
    timestamp was the wrong type".
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _count_or_zero(value: Any) -> int:
    """Return ``value`` as a non-negative int, or 0 if it is not a number.

    Same reasoning as :func:`_epoch_or_none`: a counter that is not an int
    must not take the health payload down with it. A negative count would be
    nonsense and would make the aggregator's rate maths wrong in a way no
    exception would flag.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


async def _metrics_feed_state(receiver: Any, hot_store: Any) -> HealthFeedState:
    """The metric path's /health feed state, read from live producer state.

    The metric path had NO feed of its own, and that was the gap: a
    metrics receiver that accepts exports, exposes them on /metrics and
    stores NOTHING left /health reporting green. The operator saw a live
    series on the Prometheus route and a healthy health check, and the
    durable substrate was empty.

    ``entities_count`` counts the DURABLE rows, not the exposed series: the
    substrate is the ground truth, and the Prometheus exposure is a
    separate path that can succeed while the substrate is broken. That is
    exactly the case this feed has to catch, so it must not read the
    exposure.

    The cycle count is the metric path's OWN, not the receiver's span
    cycle count: this feed reports the metric path, and a feed whose cycle
    count came from another path could look alive while the metric path
    had never run. The receiver counts its startup as its first cycle, so a
    receiver that has simply not been exported to yet reports
    ``cycles_total >= 1`` with ``entities_count == 0`` — WARMING UP, not
    DEGRADED. A degraded feed is a 503, and the launchd wrapper polls
    /health with ``curl -fsS``, so a feed that flapped to degraded on
    every cold start would make the wrapper kill the server it was
    supposed to start.
    """
    from mcp_common.health.feed import HealthFeedState

    entities_count = 0
    with suppress(Exception):
        if isinstance(hot_store, HotStore):
            from akosha.storage.metrics_query import count_metrics_points

            entities_count = await count_metrics_points(hot_store)
    running = receiver is not None
    cycles_total = _count_or_zero(getattr(receiver, "metrics_cycles_total", 0))
    errors_total = _count_or_zero(getattr(receiver, "metrics_rejected_total", 0)) + _count_or_zero(
        getattr(receiver, "metrics_insert_failed_total", 0)
    )
    last_updated = _epoch_or_none(getattr(receiver, "metrics_last_batch_at", None))
    last_error = _epoch_or_none(getattr(receiver, "metrics_last_error_at", None))
    # An unusable substrate is an ONGOING error, not a past one: the feed
    # has to stay degraded for as long as the substrate is broken, even
    # when the last failure was minutes ago and no export has arrived
    # since. The receiver's readiness flag is the live reading; a past
    # timestamp alone would decay out of the halflife and read as healthy
    # again.
    if receiver is not None and not bool(getattr(receiver, "metrics_substrate_ready", True)):
        last_error = time.time()
    return HealthFeedState(
        entities_count=entities_count,
        last_updated_timestamp=last_updated,
        cycles_total=cycles_total,
        errors_total=errors_total,
        last_error_at=last_error,
        ingester_running=running,
    )


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
        # Phase 5 (REQ-FEED-002): global declarations MUST come BEFORE
        # any other code in this function that references these names,
        # including the nested ``_kg_refresh_loop``, ``_aging_loop``,
        # and ``_default_health_probe`` definitions. Python's scope
        # rules require a ``global`` declaration to appear before any
        # assignment to that name in the enclosing function — even an
        # assignment in a nested function body that doesn't rebind
        # the name in the outer scope triggers the parser's
        # "assigned to before global declaration" SyntaxError. Hoisting
        # the declarations here is the cleanest fix; adding more
        # ``global`` lines mid-function only hides the real ordering
        # problem.
        global \
            _kg_refresh_cycles, \
            _kg_refresh_errors, \
            _kg_refresh_last_error_at, \
            _aging_cycles, \
            _aging_errors, \
            _aging_last_run_at, \
            _cold_export_cycles, \
            _cold_export_errors, \
            _cold_export_last_run_at

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

        # Console span export is a DEBUGGING aid, and it is opt-in.
        #
        # It used to be switched on whenever ENVIRONMENT was unset (which
        # is the normal case), but it never actually worked: the provider
        # carrying the console processor was discarded by the sampler
        # rebuild. Now that the provider bug is fixed it would work, and
        # it would write every span to stderr -- including the
        # asyncio-instrumented task spans, of which a server makes a great
        # many. The subprocess's stderr is the launchd log, so that would
        # be a 60MB-log problem, not a debugging one.
        enable_console_export = os.getenv("AKOSHA_CONSOLE_SPAN_EXPORT", "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        tracer, meter = setup_telemetry(
            # The name the receiver's heartbeat roster watches. It used to
            # be "akosha-mcp", which no roster entry matches, so akosha's
            # own spans could never have satisfied the watcher that exists
            # to watch them.
            service_name="akosha",
            environment=environment,
            otlp_endpoint=otlp_endpoint,
            enable_console_export=enable_console_export,
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
            # REQ-MS-002: repopulate the in-memory cache from the SQLite
            # backing so eval-metric history survives Akosha restarts.
            await analytics_service.initialize()
            logger.info("Time-series analytics service initialized")
        else:
            analytics_service = None

        # Initialize hot store using factory (Task 1.1a: wire PgvectorHotStore)
        #
        # The backend + database path come from the layered config
        # (``hot.backend`` / ``hot.path`` in settings/akosha.yaml, overlaid
        # by ~/.config/akosha/local.yaml). The previous call was
        # ``create_hot_store()`` with no arguments, which always produced
        # the DEFAULT in-memory DuckDB (``:memory:``) — so the
        # ``conversations`` table lost every row on restart, and the
        # configured backend never reached the runtime at all.
        from akosha.config import get_config

        _hot_cfg = get_config().hot
        hot_store = create_hot_store(
            backend=_hot_cfg.backend,
            database_path=_hot_cfg.path,
        )
        await hot_store.initialize()
        logger.info(
            "Hot store initialized (%s, backend=%s, path=%s)",
            type(hot_store).__name__,
            _hot_cfg.backend,
            _hot_cfg.path,
        )

        # ------------------------------------------------------------------
        # v7 PR-A3: bind the OTLP receiver immediately after the substrate
        # exists and before anything else touches the port.
        #
        # Order is not cosmetic:
        #   - after ``hot_store.initialize()`` because ``receiver.start()``
        #     asserts ``spans_substrate`` exists and raises
        #     ``LifecycleError("spans_substrate_missing")`` otherwise. Bind
        #     first and you get listeners that accept connections, return
        #     200s, and store nothing — indistinguishable from working.
        #   - before ``set_shared_hot_store`` below so a boot that cannot
        #     bind fails before publishing a substrate nothing is writing to.
        #   - before ``_apply_tool_profile`` so a tool that reports ingest
        #     health during registration sees a live receiver.
        #
        # Binding is unconditional and not feature-flagged: the receiver owns
        # the canonical :4317/:4318 per the v7 port-ownership decision in
        # docs/decisions/2026-10-21-otel-substrate-port-ownership.md. A
        # flag here would mean a config error silently un-owns the fleet's
        # OTLP endpoint.
        # ------------------------------------------------------------------
        stop_event = asyncio.Event()
        global _lifespan_spans_config

        _lifespan_spans_config = get_config().spans_substrate
        _receiver_validator_cfg = get_config().resource_validator
        from akosha.observability.resource_validator import ResourceAttributeValidator

        receiver = OTLPReceiver(
            hot_store,
            grpc_port=_receiver_port("grpc"),
            http_port=_receiver_port("http"),
            # PR-A6: the allowlist comes from config so a new MCP server can
            # be admitted without a code change. The validator owns its drift
            # history, so the receiver holds ONE instance for its lifetime --
            # a per-batch instance would detect nothing.
            resource_validator=ResourceAttributeValidator(
                allowed_namespaces=frozenset(_receiver_validator_cfg.allowed_namespaces),
                startup_grace_seconds=_receiver_validator_cfg.startup_grace_seconds,
                rolling_window_seconds=_receiver_validator_cfg.rolling_window_seconds,
                spike_threshold=_receiver_validator_cfg.spike_threshold,
                sustained_threshold_hr=(_receiver_validator_cfg.sustained_threshold_per_hour),
            ),
        )
        await receiver.start()
        set_shared_otel_receiver(receiver)

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

        # ------------------------------------------------------------------
        # Wave 5: publish the lifespan-owned HotStore BEFORE the tool
        # wrappers register so they reuse this instance instead of producing
        # a fresh in-memory DuckDB database per call. Without this ordering
        # (set_shared_hot_store AFTER _apply_tool_profile), the tool
        # wrappers fall through to per-call construction in
        # ``_try_create_hot_store()`` and see an empty database — the
        # OtelTraceIngester and CodeGraphIngester write to THIS instance
        # while ``query_local_traces`` / ``list_ingested_code_graphs``
        # read from a DIFFERENT empty instance. Symptom surfaced during the
        # trace-pipeline Phase 1.5 fix: ``local_traces_feed`` (kg_refresh)
        # showed 27 entities but the same-query MCP tool returned 0.
        # ------------------------------------------------------------------
        set_shared_hot_store(hot_store)

        # ------------------------------------------------------------------
        # Wave 5: publish the lifespan-owned KnowledgeGraphBuilder so the
        # ``register_akosha_group`` wrapper reuses it. Construct it once
        # here so the periodic-refresh task (below) can write into the same
        # instance the tools read from via ``get_graph_statistics``.
        #
        # ORDERING IS LOAD-BEARING — this MUST stay BEFORE
        # ``_apply_tool_profile``. The tool groups are registered (and
        # their closures capture the builder) during that call, and
        # ``akosha/mcp/tools/group_registers.py`` resolves the builder via
        # ``_get_shared_kg_builder() or KnowledgeGraphBuilder()``. When the
        # shared builder is published afterwards, the registration-time
        # lookup returns ``None`` and the OR-fallback captures an ORPHAN
        # instance; the refresh task then writes into the lifespan's
        # instance while ``get_graph_statistics`` / ``get_neighbors`` read
        # the orphan — which is why the tool returned
        # ``{"total_entities": 0, ...}`` forever even though the refresh
        # loop had rows.
        #
        # This mirrors the HotStore ordering above exactly: both singletons
        # must be published before the tool closures capture them.
        # ------------------------------------------------------------------
        from akosha.processing.knowledge_graph import KnowledgeGraphBuilder

        kg_builder = KnowledgeGraphBuilder()
        set_shared_kg_builder(kg_builder)
        logger.info("Knowledge graph builder initialised (Wave 5)")

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
                # docs/specs/2026-09-06-live-mcp-smoke-test-design.md
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
        # Start the event-subscriber stack: the ONLY producer of the
        # ``conversations`` table in this process.
        #
        # This mirrors the wiring in ``akosha/main.py`` (the standalone
        # FastAPI application) and uses the SAME helper module
        # (``akosha.ingestion.subscriber_wiring``) so the two entry points
        # cannot drift apart again.
        #
        # Before this, the subscriber existed ONLY in the standalone
        # FastAPI app. The only Akosha process that actually runs is
        # ``python -m akosha mcp start``, so the MCP process had NO
        # producer: the kg_refresh loop read an empty ``conversations``
        # table every cycle and the knowledge graph stayed empty forever.
        #
        # Config comes from the layered loader (``get_config()``), so the
        # operator's ``~/.config/akosha/local.yaml`` overlay is honoured.
        # The subscriber fails soft — an unreachable Redis leaves it
        # not-running rather than taking the process down.
        # ------------------------------------------------------------------
        global _event_subscriber, _event_subscriber_running, _event_subscriber_enabled
        try:
            from akosha.ingestion.subscriber_wiring import (
                build_event_subscribers,
                start_event_subscriber,
            )

            _app_cfg = get_config()
            _event_subscriber_enabled = _app_cfg.websocket_invocations_subscriber.enabled
            if is_lite_mode:
                # Lite mode means "zero external dependencies"; Redis is an
                # external dependency. Skip the producer, and say so — an
                # empty feed caused by the mode must never be presented as
                # a healthy empty feed.
                logger.warning(
                    "Lite mode disables external services: the Redis-backed "
                    "event subscriber is NOT started, so the conversations "
                    "table stays empty and the knowledge graph stays empty. "
                    "Run in standard mode if those feeds matter.",
                )
            else:
                _event_subscriber = build_event_subscribers(
                    hot_store,
                    websocket_cfg=_app_cfg.websocket_invocations_subscriber,
                    bodai_cfg=_app_cfg.bodai_tool_invocation_subscriber,
                )
                _event_subscriber_running = await start_event_subscriber(_event_subscriber)
        except Exception as exc:
            logger.warning("Event subscriber wiring failed (%s)", exc)
            _event_subscriber = None
            _event_subscriber_running = False

        # ------------------------------------------------------------------
        # Wave 6: start the OtelTraceIngester so spans from an OTLP/HTTP
        # collector land in the shared HotStore (independent of the
        # BodaiToolInvocationSubscriber's Redis feed). Mirrors the
        # CodeGraphIngester block above.
        #
        # PR-A8: retired. The poller POSTs an empty JSON envelope to an
        # OTLP/HTTP endpoint and expects spans back; no standard receiver
        # answers that, so it has ingested nothing since the OTLP receiver
        # took :4318. It is kept behind a settings flag rather than deleted so
        # rollback stays a flag flip -- see below.
        # ------------------------------------------------------------------
        global _otel_trace_ingester
        # PR-A8. The gate is the settings key, NOT an env var.
        #
        # The legacy skip env var for this ingester is retired: it gated a
        # poller that no longer does anything, and an operator who set it
        # would believe the poller was off when the settings key is what
        # actually controls it. Rollback is flipping
        # ``otel.receiver.trace_ingester_poller`` back to true and restarting.
        #
        # The class is retained behind the flag rather than deleted precisely
        # so that rollback stays a flag flip, and so the flag never becomes "a
        # settings key that maps to deleted code" -- a documentation lie that
        # outlives the code.
        if get_config().otel.receiver.trace_ingester_poller:
            try:
                from akosha.ingestion.otel_ingester import OtelTraceIngester

                otel_endpoint = os.getenv("AKOSHA_OTLP_ENDPOINT", "http://localhost:4318/v1/traces")
                poll_seconds = int(os.getenv("AKOSHA_OTEL_POLL_SECONDS", "60"))
                # Spec-listed knobs (see
                # docs/specs/2026-09-06-otel-trace-ingester-design.md).
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
            logger.info(
                "OtelTraceIngester not started: "
                "otel.receiver.trace_ingester_poller is false. The OTLP receiver "
                "is the span producer; this poller POSTs empty JSON envelopes to "
                "an OTLP/HTTP endpoint, which no receiver answers."
            )

        # ------------------------------------------------------------------
        # Wave 5: start the knowledge-graph periodic-refresh task. Every
        # ``AKOSHA_KG_REFRESH_SECONDS`` (default 60), pull the most recent
        # traces from HotStore and feed them to the kg_builder as
        # ``conversation`` records. opt-out via
        # ``AKOSHA_SKIP_KG_REFRESH=1``.
        # ------------------------------------------------------------------
        # ``_kg_refresh_*`` declared ``global`` at the top of this
        # function; we do not redeclare them here.

        async def _kg_refresh_loop() -> None:
            global _kg_refresh_cycles, _kg_refresh_errors, _kg_refresh_last_error_at
            try:
                interval = float(os.getenv("AKOSHA_KG_REFRESH_SECONDS", "60"))
            except ValueError:
                interval = 60.0
            while True:
                try:
                    # Phase 4 (2026-10-05 fix): bump cycles_total at the
                    # START of each cycle (mirrors code_graph_ingester.py
                    # and otel_ingester.py) so the aggregator's HNSW
                    # hardening stops flagging knowledge_graph_feed as
                    # ``broken-before-first-success`` during the first
                    # 60s of uptime. The previous ``sleep(interval)``
                    # before the increment meant fresh servers sat at
                    # cycles=0 (DEGRADED) for one full interval — and
                    # since mcp_tool_call_feed has its own per-task-class
                    # counter, the aggregate stayed ``degraded`` and
                    # /health returned 503 even after the AgingService
                    # init failure was repaired. The fix matches the
                    # OTel + code_graph processor pattern: count, sleep,
                    # work, repeat.
                    _kg_refresh_cycles += 1
                    # Work runs immediately after the count, then we
                    # sleep. This matches the OTel + code_graph processor
                    # pattern (otel_ingester.py:156,
                    # code_graph_ingester.py:129).
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
                            # WARNING, not DEBUG: this used to be swallowed
                            # at DEBUG, which is why a per-row AttributeError
                            # (metadata arriving as a JSON string) produced
                            # 4850 consecutive "entities=0 edges=0" cycles
                            # with nothing in the log at LOG_LEVEL=INFO.
                            # A row that cannot be extracted is a broken
                            # feed, not a debug detail.
                            logger.warning("kg_builder per-row extract failed (%s)", exc)
                    if rows:
                        logger.info(
                            "kg_refresh: cycle=%d rows=%d entities=%d edges=%d",
                            _kg_refresh_cycles,
                            len(rows),
                            len(kg_builder.entities),
                            len(kg_builder.edges),
                        )
                    # Phase 4 fix (2026-10-05): sleep at the END of the
                    # cycle so the first cycle fires immediately rather than
                    # after ``interval`` seconds. Same ordering as
                    # code_graph_ingester.py:165 (count, work, sleep).
                    await asyncio.sleep(interval)
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
        # Phase 2 (REQ-FEED-002/003/004): AgingService cron + pre-warm hook.
        # One background task (analogous to ``_kg_refresh_loop``) ticks
        # every ``AKOSHA_AGING_INTERVAL_SECONDS`` (default 3600s) and
        # migrates records older than ``AKOSHA_AGING_CUTOFF_DAYS``
        # (default 7) for the ``mcp_tool_call`` task_class only — the
        # conversation stream keeps its manual-opt-in retention
        # (REQ-FEED-001). Opt-out via ``AKOSHA_SKIP_AGING=1``.
        #
        # The AgingService needs a warm_store destination. The lifespan
        # doesn't currently publish one (the kg_refresh / OTel pipelines
        # write to hot_store only), so we instantiate a fresh warm_store
        # via ``create_warm_store`` here. On failure we fall back to
        # skip-aging rather than crash the lifespan — the warm tier is
        # optional in the same sense cold_storage is.
        # ------------------------------------------------------------------
        aging_service: Any | None = None
        # The warm store the aging service writes into. It is ALSO the
        # source the cold exporter reads from, so it is held here (not
        # scoped to the aging block) for the cold-export wiring below.
        aging_warm_store: Any | None = None
        # NB: ``_aging_cycles`` / ``_aging_errors`` / ``_aging_last_run_at``
        # are declared ``global`` at the top of this function. Python's
        # scope rules forbid a second ``global`` declaration after any
        # assignment in the same function body, so we cannot redeclare
        # them here even though it's the natural place.
        if not _env_truthy("AKOSHA_SKIP_AGING"):
            try:
                from akosha.storage import create_warm_store
                from akosha.storage.aging import AgingService

                # DuckDB-ssd backend always returns WarmStore; cast the
                # ``create_warm_store`` union to narrow the type for ty.
                aging_warm_store = cast("WarmStore", create_warm_store(backend="duckdb-ssd"))
                await aging_warm_store.initialize()
                aging_service = AgingService(hot_store=hot_store, warm_store=aging_warm_store)
                logger.info(
                    "AgingService initialized (warm_store=%s)", type(aging_warm_store).__name__
                )
            except Exception as exc:
                logger.warning(
                    "AgingService init failed (%s); mcp_tool_call feed "
                    "retention will not run until the next lifespan restart",
                    exc,
                )
                aging_service = None

        async def _aging_loop() -> None:
            global _aging_cycles, _aging_errors, _aging_last_run_at
            if aging_service is None:  # type guard for the type checker
                return
            try:
                interval = float(os.getenv("AKOSHA_AGING_INTERVAL_SECONDS", "3600"))
            except ValueError:
                interval = 3600.0
            try:
                cutoff_days = int(os.getenv("AKOSHA_AGING_CUTOFF_DAYS", "7"))
            except ValueError:
                cutoff_days = 7
            while True:
                try:
                    await asyncio.sleep(interval)
                    _aging_cycles += 1
                    stats = await aging_service.migrate_hot_to_warm(
                        cutoff_days=cutoff_days, task_class="mcp_tool_call"
                    )
                    _aging_last_run_at = time.time()
                    logger.info(
                        "aging cycle=%d cutoff=%dd records_migrated=%d errors=%d",
                        _aging_cycles,
                        cutoff_days,
                        stats.records_migrated,
                        stats.errors,
                    )
                except asyncio.CancelledError:
                    logger.info(
                        "aging loop cancelled after %d cycles (%d errors)",
                        _aging_cycles,
                        _aging_errors,
                    )
                    break
                except Exception as exc:
                    _aging_errors += 1
                    _aging_last_run_at = time.time()
                    logger.warning("aging loop iteration failed (%s); will retry", exc)

        if not _env_truthy("AKOSHA_SKIP_AGING") and aging_service is not None:
            global _aging_task
            _aging_task = asyncio.create_task(_aging_loop(), name="akosha.aging")
            logger.info(
                # %s for both: os.getenv returns str. The cutoff slot was %d,
                # which raises TypeError ("a real number is required, not str")
                # on every startup where AgingService initialises successfully.
                # It went unnoticed because this line was unreachable in tests --
                # aging_service was always None, since `akosha doctor` had left a
                # directory at the warm.db path and init failed first.
                "aging task started (Phase 2 REQ-FEED-002, interval=%ss, cutoff=%ss)",
                os.getenv("AKOSHA_AGING_INTERVAL_SECONDS", "3600"),
                os.getenv("AKOSHA_AGING_CUTOFF_DAYS", "7"),
            )
            # Pre-warm (REQ-FEED-004): run one migration cycle synchronously
            # before yielding so /health shows ``aging_feed.cycles_total >= 1``
            # within 60s of Akosha boot, regardless of the cron interval.
            # Without this the aggregator's warming_up predicate stays true
            # for up to ``AKOSHA_AGING_INTERVAL_SECONDS`` after boot,
            # which the launchd wrapper's 60s healthcheck window can't tolerate.
            # Use the lifespan-bound hot_store (NOT a fresh one) so the
            # pre-warm runs against the same DB the rest of the lifespan
            # uses — a fresh instance would see an empty table and the
            # pre-warm would no-op.
            try:
                cutoff_days = int(os.getenv("AKOSHA_AGING_CUTOFF_DAYS", "7"))
            except ValueError:
                cutoff_days = 7
            # ``_aging_cycles`` / ``_aging_errors`` / ``_aging_last_run_at``
            # are declared ``global`` at the top of this function; we
            # cannot redeclare them here (Python forbids a second
            # ``global`` declaration after assignments in the same
            # function body).
            try:
                stats = await aging_service.migrate_hot_to_warm(
                    cutoff_days=cutoff_days, task_class="mcp_tool_call"
                )
                _aging_cycles += 1
                _aging_last_run_at = time.time()
                logger.info(
                    "aging pre-warm cycle=1 cutoff=%dd records_migrated=%d",
                    cutoff_days,
                    stats.records_migrated,
                )
            except Exception as exc:
                _aging_errors += 1
                logger.warning("aging pre-warm failed (%s)", exc)

        # ------------------------------------------------------------------
        # Cold tier (F5): the warm->cold export cron.
        # The cold tier is Parquet batches in object storage. Until this
        # loop existed, ``ColdStore.export_batch`` -- the only producer of
        # a cold object -- had no production caller, so the tier was
        # reachable but not writable.
        #
        # The exporter reads the SAME warm store the aging service writes
        # into: a second ``duckdb.connect()`` on the same file from the
        # same process would fail on the file lock.
        #
        # It runs only when BOTH tiers exist. Lite mode has no cold store;
        # ``AKOSHA_SKIP_AGING=1`` leaves no warm store. Either way the
        # tier chain stops where the operator asked it to.
        # ------------------------------------------------------------------
        cold_exporter: Any | None = None
        if cold_storage is not None and aging_warm_store is not None:
            try:
                from akosha.storage.cold_exporter import ColdExporter

                cold_exporter = ColdExporter(
                    warm_store=aging_warm_store,
                    cold_store=cold_storage,
                    age_threshold_days=get_config().cold.age_threshold_days,
                )
                logger.info(
                    "ColdExporter initialized (threshold=%dd, backend=%s)",
                    get_config().cold.age_threshold_days,
                    type(cold_storage).__name__,
                )
            except Exception as exc:
                logger.warning(
                    "ColdExporter init failed (%s); the cold tier will not be written "
                    "until the next lifespan restart",
                    exc,
                )
                cold_exporter = None

        async def _cold_export_loop() -> None:
            global _cold_export_cycles, _cold_export_errors, _cold_export_last_run_at
            if cold_exporter is None:  # type guard for the type checker
                return
            try:
                interval = float(os.getenv("AKOSHA_COLD_EXPORT_INTERVAL_SECONDS", "86400"))
            except ValueError:
                interval = 86400.0
            while True:
                try:
                    _cold_export_cycles += 1
                    stats = await cold_exporter.export_expired()
                    _cold_export_last_run_at = time.time()
                    logger.info(
                        "cold export cycle=%d records=%d batches=%d bytes=%d errors=%d",
                        _cold_export_cycles,
                        stats.records_exported,
                        stats.batches_exported,
                        stats.bytes_exported,
                        stats.errors,
                    )
                except asyncio.CancelledError:
                    logger.info(
                        "cold export loop cancelled after %d cycles (%d errors)",
                        _cold_export_cycles,
                        _cold_export_errors,
                    )
                    break
                except Exception as exc:
                    _cold_export_errors += 1
                    _cold_export_last_run_at = time.time()
                    logger.warning("cold export loop iteration failed (%s); will retry", exc)
                await asyncio.sleep(interval)

        if (
            not _env_truthy("AKOSHA_SKIP_COLD_EXPORT")
            and cold_exporter is not None
            and not _env_truthy("AKOSHA_SKIP_AGING")
        ):
            global _cold_export_task
            _cold_export_task = asyncio.create_task(_cold_export_loop(), name="akosha.cold_export")
            logger.info(
                "cold export task started (interval=%ss)",
                os.getenv("AKOSHA_COLD_EXPORT_INTERVAL_SECONDS", "86400"),
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
            # The retired poller's counters are read only for the LEGACY wire
            # fields below; its running/cycles/errors values are gone, because
            # the feed is sourced from the OTLP receiver now.
            otel_last_poll = _epoch_or_none(
                getattr(_otel_trace_ingester, "_last_poll_at", None)
                if _otel_trace_ingester is not None
                else None
            )
            otel_last_error = _epoch_or_none(
                getattr(_otel_trace_ingester, "_last_error_at", None)
                if _otel_trace_ingester is not None
                else None
            )

            # PR-A8: the OTLP receiver is the span producer now.
            #
            # The legacy OtelTraceIngester poller is gated off, so its counters
            # above are permanently zero and its ``_running`` permanently
            # False. Reading them for the health verdict would report a dead
            # producer: ``mcp_tool_call_feed`` would get entities_count=0,
            # cycles_total=0, ingester_running=False, which
            # ``mcp_common.health.feed.is_healthy`` maps to FAILED -- and
            # severity 3 is a 503 on the /health route. The feed only reads
            # warming_up today because the poller's cycles_total kept
            # climbing while it ingested nothing at all.
            #
            # So the feed is re-pointed at the receiver, which IS alive and
            # IS the thing writing spans_substrate. The legacy values stay
            # above only because the retired wire fields still report them.
            _otel_receiver = get_shared_otel_receiver()
            receiver_running = _otel_receiver is not None
            # The feed's cycle count is the receiver's HEALTH cycle count, not
            # its batch count. The batch count is zero until a producer
            # actually exports, and a feed stuck at cycles_total=0 is
            # `feed_never_populated` -- DEGRADED, so a 503, and the launchd
            # wrapper's `curl -fsS` kills the server it just started. The
            # receiver counts its startup as its first cycle precisely so
            # this cannot happen; see OTLPReceiver.cycles_total.
            receiver_cycles = _count_or_zero(getattr(_otel_receiver, "cycles_total", 0))
            receiver_batches = _count_or_zero(getattr(_otel_receiver, "batches_total", 0))
            receiver_errors = _count_or_zero(getattr(_otel_receiver, "dlq_insert_failed_total", 0))
            receiver_last_batch = _epoch_or_none(getattr(_otel_receiver, "last_batch_at", None))
            receiver_last_error = _epoch_or_none(getattr(_otel_receiver, "last_error_at", None))
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
                cycles_total=_kg_refresh_cycles + receiver_cycles,
                errors_total=_kg_refresh_errors + receiver_errors,
                last_error_at=local_traces_last_error,
                ingester_running=(kg_running or receiver_running),
            )

            # REQ-TSQ-008 (docs/plans/2026-09-26-tool-surface-quality.md):
            # dedicated ``mcp_tool_call`` feed so operators can distinguish a
            # degraded tool-call trace feed from a healthy trace feed in
            # general. ``local_traces_feed`` above aggregates ALL OTel traces
            # generically; this feed filters by ``task_class == "mcp_tool_call"``
            # so the four-signal HealthFeedState surface (entities_count,
            # last_updated_timestamp, cycles_total, errors_total) reflects
            # the tool-call subset specifically. REQ-FEED-002 (Phase 2)
            # initially routed ``cycles_total`` and ``errors_total`` through
            # the ingester's per-task-class accessors so a healthy
            # mcp_tool_call stream wasn't masked by a noisy
            # ``conversation`` stream (the two share one ingester-level
            # counter, which used to roll both into this feed's cycles).
            #
            # 2026-10-05 fix: ``cycles_total`` and ``errors_total`` now source
            # from the OTel ingester's OVERALL counters (same pattern as
            # ``local_traces_feed``'s otel_cycles / otel_errors), not the
            # per-task-class ones. Reason: per-task-class counts stay at 0
            # on a fresh server until a ``mcp_tool_call``-class span flows
            # through (no upstream Bodai event producer yet), which made the
            # aggregator's HNSW hardening branch fire on every cold start
            # (cycles_total=0 + ingester_running=True → DEGRADED) and pushed
            # the aggregate ``status`` to ``degraded`` even after the
            # ``AgingService`` / ``kg_refresh`` fixes had made their feeds
            # healthy-or-warming-up. /health returned 503 for the lifetime
            # of every install. With the overall counter the feed goes
            # warming_up after the OTel ingester's first poll, matching the
            # pattern operators expect from a producer that exists but
            # has not yet seen data on this specific task class.
            # ``entities_count`` remains per-task-class so operators still
            # see "zero mcp_tool_call spans landed yet" as distinct from
            # "ingester dead". If a future operator wants the per-task-class
            # signal back for /health, the right move is to introduce a
            # dedicated Bodai-tool-invocation producer with its own
            # /health feed (e.g. ``bodai_tool_invocation_feed``); until then
            # coupling the mcp_tool_call_feed's health to the OTel ingester
            # is the lesser of two evils.
            mcp_tool_call_entities_count = 0
            with suppress(Exception):
                if isinstance(hot_store, HotStore):
                    # PR-A8: count from spans_substrate, where the receiver
                    # writes, rather than from conversations, where the
                    # retired poller wrote. Querying the old table would
                    # report zero spans arriving while spans are in fact
                    # landing -- a health feed that reports the opposite of
                    # the truth.
                    from akosha.storage.span_query import SpanQuery

                    mcp_tool_call_entities_count = await SpanQuery(hot_store).count_by_task_class(
                        "mcp_tool_call"
                    )
            mcp_tool_call_state = HealthFeedState(
                entities_count=mcp_tool_call_entities_count,
                last_updated_timestamp=receiver_last_batch,
                cycles_total=receiver_cycles,
                errors_total=receiver_errors,
                last_error_at=receiver_last_error,
                ingester_running=receiver_running,
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

            # The METRIC path's own feed. See ``_metrics_feed_state`` for
            # why it exists and what each of its fields means.
            metrics_receiver = get_shared_otel_receiver()
            metrics_state = await _metrics_feed_state(metrics_receiver, hot_store)

            # Phase 2 (REQ-FEED-003): the AgingService cron migrates
            # records from hot to warm; the feed's ``entities_count`` is
            # the count of mcp_tool_call rows still in the hot store
            # (aging itself doesn't grow the count — it migrates rows
            # OUT of hot_store and INTO warm_store). The cycle counter
            # is the meaningful signal here: operators want to know
            # "did the aging cron run?", not "how many rows did it
            # produce?" — the pre-warm hook bumps ``cycles_total`` to
            # 1 within 60s of boot so /health is not stuck in
            # ``warming_up`` longer than the launchd wrapper tolerates.
            aging_running = bool(_aging_task is not None and not _aging_task.done())
            aging_state = HealthFeedState(
                entities_count=mcp_tool_call_entities_count,
                last_updated_timestamp=_aging_last_run_at,
                cycles_total=_aging_cycles,
                errors_total=_aging_errors,
                last_error_at=None,
                ingester_running=aging_running,
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
                    "mcp_tool_call_feed": mcp_tool_call_state,
                    "aging_feed": aging_state,
                    "skills_signer": skills_signer_state,
                    "metrics_feed": metrics_state,
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
                    # Plan 2 Phase E2 §6.5 — 4 canonical fields per feed
                    # on the wire (mcp-backend-wiring-discipline.md §3).
                    # Renamed from the legacy ``feed_*`` prefix to match
                    # the canonical contract; pre-1.0 replace, not extend.
                    "entities_count": state.entities_count,
                    "last_updated_timestamp": state.last_updated_timestamp,
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
            # The graph has exactly one producer (kg_refresh) and exactly
            # one SOURCE of rows (the event subscriber writing the
            # ``conversations`` table). Surfacing the source producer's
            # state here is what stops an empty graph from reading as
            # "nothing to see" when the truth is "nothing is producing".
            kg_dict["source_producer_running"] = _event_subscriber_running
            kg_dict["source_producer"] = "event_subscriber (Redis bodai:events -> conversations)"
            checks["knowledge_graph_feed"] = kg_dict

            lt_dict = _feed_dict("local_traces_feed", local_traces_state)
            # Preserve domain-specific OTel visibility fields.
            lt_dict["feed_populated"] = local_traces_count > 0
            # PR-A8: these fields describe the OTLP RECEIVER now. The old
            # ``otel_ingester_*`` names described the legacy poller, which is
            # retired; keeping the names would make a permanently-dead
            # producer look like the live one in the health payload.
            lt_dict["otel_receiver_running"] = receiver_running
            lt_dict["otel_receiver_http_port"] = (
                _count_or_zero(getattr(_otel_receiver, "_http_port", 0)) or None
            )
            lt_dict["otel_receiver_batches_total"] = receiver_batches
            lt_dict["otel_receiver_errors_total"] = receiver_errors
            # The ``conversations`` table is written ONLY by the event
            # subscriber. When it is not running, the table is empty and
            # ``feed_populated`` is False — an empty feed, not a healthy
            # one. Report the producer so the difference is visible.
            lt_dict["event_subscriber_running"] = _event_subscriber_running
            lt_dict["event_subscriber_enabled"] = _event_subscriber_enabled
            lt_dict["operational_mode"] = "lite" if is_lite_mode else "standard"
            lt_dict["source"] = (
                "hot_store.query_traces (populated via kg_refresh) + "
                "spans_substrate (populated by the OTLP receiver)"
            )
            checks["local_traces_feed"] = lt_dict

            # REQ-TSQ-008 (docs/plans/2026-09-26-tool-surface-quality.md):
            # expose the mcp_tool_call sub-feed as its own per-feed dict so
            # operators can distinguish a degraded tool-call trace feed from
            # a healthy trace feed in general (the aggregator's worst-case
            # 503 contract uses this; the per-feed dict carries the four
            # mandatory signals: entities_count, last_updated_timestamp,
            # cycles_total, errors_total). ``source`` documents the filter
            # so operators can reproduce the count via mcp__akosha__query_local_traces.
            mt_dict = _feed_dict("mcp_tool_call_feed", mcp_tool_call_state)
            mt_dict["source"] = "spans_substrate(task_class='mcp_tool_call')"
            mt_dict["otel_receiver_running"] = receiver_running
            checks["mcp_tool_call_feed"] = mt_dict

            # Phase 2 (REQ-FEED-003): the aging cron runs over the same
            # mcp_tool_call rows (it doesn't grow a new collection). The
            # cycle counter and the aging_task boolean are the load-bearing
            # signals — ``entities_count`` mirrors the mcp_tool_call count
            # so operators don't see a confusing "aging processed 0 rows"
            # while the upstream trace feed is healthy.
            ag_dict = _feed_dict("aging_feed", aging_state)
            ag_dict["source"] = (
                "AgingService.migrate_hot_to_warm(task_class='mcp_tool_call') "
                "interval=AKOSHA_AGING_INTERVAL_SECONDS "
                "cutoff=AKOSHA_AGING_CUTOFF_DAYS"
            )
            ag_dict["aging_task_running"] = aging_running
            checks["aging_feed"] = ag_dict

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

            # The metric path's per-feed dict. ``substrate_ready`` is the
            # field an operator reads first: it says whether the durable
            # write is happening, which is the question the exposed series
            # cannot answer — a series on /metrics is visible whether or
            # not anything backs it.
            ms_dict = _feed_dict("metrics_feed", metrics_state)
            ms_dict["source"] = "metrics_substrate (written by the OTLP MetricsService receiver)"
            ms_dict["otel_receiver_running"] = metrics_state.ingester_running
            ms_dict["substrate_ready"] = bool(
                getattr(metrics_receiver, "metrics_substrate_ready", True)
            )
            ms_dict["batches_total"] = _count_or_zero(
                getattr(metrics_receiver, "metrics_batches_total", 0)
            )
            checks["metrics_feed"] = ms_dict

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
                    "mcp_tool_call_feed",
                    "aging_feed",
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

        # Phase 3 (REQ-RUNTIME-001 / Plan 1 Task 3.1): push the static
        # agent catalog to Oneiric as a fire-and-forget background task,
        # mirroring the registration pattern used by Session-Buddy. The coroutine
        # is fail-soft by contract (see ``oneiric_runtime.py``) so a
        # degraded Oneiric instance never blocks Akosha boot.
        global _oneiric_push_task
        _oneiric_push_task = asyncio.create_task(
            report_runtime_to_oneiric(app), name="akosha.oneiric_push"
        )

        # v7 PR-A3: the receiver's two lifespan tasks. Both ship as stubs at
        # PR-A3 (see OTLPReceiver.run_canary_emitter / run_retention_sweep)
        # and run in parallel with the MCP server.
        canary_task = asyncio.create_task(
            receiver.run_canary_emitter(stop_event), name="receiver_canary"
        )
        retention_task = asyncio.create_task(
            receiver.run_retention_sweep(stop_event), name="spans_retention_sweep"
        )

        # v7 PR-A5: the heartbeat watcher. It reads spans_substrate through
        # SpanQuery -- the SAME store the receiver writes -- and reports MCP
        # servers that have stopped emitting spans. The roster is injected
        # rather than read from a module global so the watcher stays
        # independently testable and free of import-order coupling.
        #
        # The roster comes from config. An EMPTY roster is a P0 in its own
        # right: a watcher with nothing to watch is a misconfiguration, not
        # an all-clear, and the watcher reports that rather than silently
        # reporting healthy.
        from akosha.observability.heartbeat_watcher import HeartbeatWatcher
        from akosha.storage.span_query import SpanQuery

        heartbeat_watcher = HeartbeatWatcher(
            SpanQuery(hot_store),
            known_mcp_servers=get_config().otel.receiver.known_mcp_servers,
        )
        heartbeat_task = asyncio.create_task(
            heartbeat_watcher.run(stop_event), name="heartbeat_watcher"
        )

        yield {
            "akosha_ready": True,
            "embedding_service": embedding_service,
            "analytics_service": analytics_service,
            "tracer": tracer,
            "meter": meter,
            "mode": "lite" if is_lite_mode else "standard",
            "cache_client": cache_client,
            "cold_storage": cold_storage,
            "otel_receiver": receiver,
        }

        # v7 PR-A3: reverse-order shutdown for the receiver.
        #
        # ``stop_event.set()`` BEFORE the cancel so a task parked on
        # ``stop_event.wait()`` wakes through the event and returns cleanly,
        # rather than being torn down mid-await by ``Task.cancel()``. The
        # receiver stops AFTER its tasks so a canary can never write into a
        # substrate whose listeners are already closed.
        stop_event.set()
        retention_task.cancel()
        canary_task.cancel()
        heartbeat_task.cancel()
        await asyncio.gather(retention_task, canary_task, heartbeat_task, return_exceptions=True)
        try:
            await receiver.stop()
        except Exception as exc:
            logger.warning("OTLPReceiver stop failed: %s", exc)
        set_shared_otel_receiver(None)

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

        # Stop the event subscriber AFTER the readers (kg_refresh) and the
        # other writers, so it cannot write into a hot store that is
        # already being torn down.
        if _event_subscriber is not None:
            try:
                from akosha.ingestion.subscriber_wiring import stop_event_subscriber

                await stop_event_subscriber(_event_subscriber)
            except Exception as exc:
                logger.warning("Event subscriber stop failed: %s", exc)
            _event_subscriber = None
        _event_subscriber_running = False

        # REQ-MS-002: close the analytics SQLite connection so the
        # write-through cache's durable backing flushes cleanly. Order
        # matters only relative to the telemetry shutdown below — the
        # SQLite write is synchronous so no data is in flight.
        if analytics_service is not None:
            try:
                await analytics_service.aclose()
            except Exception as exc:
                logger.warning("TimeSeriesAnalytics aclose failed: %s", exc)
        # Phase 2 (REQ-FEED-002): cancel the aging cron before clearing
        # state so the task gets a clean CancelledError instead of being
        # garbage-collected mid-await. Mirror the kg_refresh shutdown.
        if _aging_task is not None and not _aging_task.done():
            _aging_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await _aging_task
            _aging_task = None

        # Cold tier (F5): cancel the warm->cold export cron before clearing
        # state, mirroring the aging cron above.
        if _cold_export_task is not None and not _cold_export_task.done():
            _cold_export_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await _cold_export_task
            _cold_export_task = None

        # Reset the per-feed cycle/error counters so the next create_app()
        # call starts from zero. Without this, the probe would carry stale
        # cycle counts across lifespans in tests. ``_kg_refresh_*`` and
        # ``_aging_*`` are declared ``global`` at the top of this function
        # so we do not redeclare them here.
        _kg_refresh_cycles = 0
        _kg_refresh_errors = 0
        _kg_refresh_last_error_at = None
        _aging_cycles = 0
        _aging_errors = 0
        _aging_last_run_at = None
        _cold_export_cycles = 0
        _cold_export_errors = 0
        _cold_export_last_run_at = None

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
    #
    # Both surfaces (HTTP /health + MCP get_health) route through the
    # single ``_compute_health_body_and_status()`` helper below so the
    # ``mcp-backend-wiring-discipline.md`` parity invariant — "MCP and
    # /health must agree" — holds by construction, not by convention.
    @app.custom_route("/health", methods=["GET"])
    async def health_check(request: Any) -> Any:  # noqa: ARG001
        """HTTP readiness check — 200 only when all data feeds are healthy."""
        from starlette.responses import JSONResponse

        body, status_code = await _compute_health_body_and_status()
        return JSONResponse(body, status_code=status_code)

    # MCP ``get_health()`` tool — the canonical surface name every other
    # Bodai component ships (mahavishnu, session-buddy, crackerjack,
    # oneiric). Akosha exposes the same shape via HTTP /health but was
    # missing the MCP tool itself; bodai-radar expects parity so
    # akosha's primary health signal could not be polled through MCP.
    # Body is computed by the same helper as /health → guaranteed
    # parity.
    @app.tool()
    async def get_health() -> dict[str, object]:
        """Get the canonical Akosha health snapshot.

        Per ``mcp-backend-wiring-discipline.md`` this returns the same
        verdict as the HTTP ``GET /health`` route — they share one
        implementation (:func:`_compute_health_body_and_status`) so
        the two surfaces cannot drift.

        Body fields:
            status: One of ``healthy`` / ``warming_up`` / ``degraded`` /
                ``failed`` (canonical 4-field status string).
            service: Akosha service name (``akosha-mcp``).
            version: Akosha package version (from ``akosha.__version__``).
            launcher: ``mcp_common.server.launcher@<version>`` for
                incident-response grep.
            checks: Per-feed verdict dict (``hot_store``, ``embeddings``,
                ``cold_storage``, ``_aggregate``, plus any registered
                ``mcp_tool_call`` feed). Each entry carries ``ok`` for
                infrastructure checks and ``status``/``healthy``/
                ``reason_codes`` for data feeds.
            http_status: Mirror of the HTTP route's status code (200/503)
                for callers that want the verdict in band-status terms.
            uptime_seconds: Seconds since Akosha MCP import (process
                start), rounded to wall-clock.
        """
        body, _status_code = await _compute_health_body_and_status()
        # MCP tools always return 200 on the wire — the ``status`` field
        # in the body is what callers consult. Discard ``status_code``
        # intentionally; if a future caller wants the HTTP-style code
        # they can read ``body["http_status"]``.
        return body

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
