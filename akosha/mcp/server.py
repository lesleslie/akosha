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

import asyncio  # noqa: TC003 — runtime use (sleep, create_task), not just type annotations
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Final

from fastmcp import FastMCP

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
APP_VERSION: Final = "0.1.0"

DHARA_DEFAULT_URL = "http://localhost:8683"


def _get_mcp_url() -> str:
    """Get Akosha's MCP server URL from environment or config.

    Returns:
        MCP server URL string (e.g., "http://localhost:3002/mcp")
    """
    # Check env var first
    mcp_url = os.getenv("AKOSHA_MCP_URL", "")
    if mcp_url:
        return mcp_url

    # Fall back to host + port from config
    host = os.getenv("AKOSHA_HOST", "localhost")
    mcp_port = int(os.getenv("AKOSHA_MCP_PORT", "3002"))
    return f"http://{host}:{mcp_port}/mcp"


async def _register_to_dhara_once(dhara_url: str, key: str, mcp_url: str) -> bool:
    """Single attempt to write component_endpoint/{name} -> mcp_url to Dhara.

    Returns True on success, False on failure.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{dhara_url}/tools/call",
                json={"name": "put", "arguments": {"key": key, "value": mcp_url}},
            )
            response.raise_for_status()
            return True
    except Exception:
        return False


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
    import itertools

    dhara_url = os.getenv("DHARA_MCP_URL", DHARA_DEFAULT_URL)
    key = "component_endpoint/akosha"

    # Phase 1: exponential-backoff retry until registration succeeds
    for attempt in itertools.count():
        if await _register_to_dhara_once(dhara_url, key, mcp_url):
            logger.info("Phase 0: registered akosha endpoint to Dhara: %s -> %s", key, mcp_url)
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
            "Phase 0: exhausted retries for %s — heartbeat will continue",
            key,
        )

    # Phase 2: periodic heartbeat — cancels on server shutdown via _heartbeat_task
    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(300)  # 5 minutes
            if not await _register_to_dhara_once(dhara_url, key, mcp_url):
                logger.debug("Phase 0 heartbeat: failed to refresh %s", key)

    global _heartbeat_task
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
    app = FastMCP(
        name=APP_NAME,
        version=APP_VERSION,
    )

    # HTTP health endpoint for Claude Code compatibility
    @app.custom_route("/health", methods=["GET"])
    async def health_check(request: Any) -> Any:  # noqa: ARG001
        """HTTP health check endpoint for Claude Code `mcp list` compatibility."""
        from starlette.responses import JSONResponse

        return JSONResponse({"status": "ok", "service": "akosha", "version": APP_VERSION})

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

    # Capture mode for lifespan closure
    mode_instance = mode

    # Custom lifespan for startup/shutdown
    @asynccontextmanager
    async def lifespan(server: Any) -> AsyncGenerator[dict[str, Any]]:  # noqa: ARG001
        """Custom lifespan manager for Akosha.

        Manages the complete lifecycle of the Akosha MCP server, including
        service initialization, health checks, and graceful shutdown.

        Startup sequence:
        1. Validates authentication configuration
        2. Initializes OpenTelemetry tracing and metrics
        3. Initializes embedding service (with fallback mode)
        4. Initializes analytics service
        5. Initializes knowledge graph builder
        6. Registers all MCP tools
        7. Phase 0: registers MCP endpoint to Dhara

        Shutdown sequence:
        1. Flushes OpenTelemetry telemetry
        2. Logs shutdown completion

        Args:
            server: FastMCP server instance (unused, required by interface)

        Yields:
            dict[str, Any]: Context dictionary containing initialized services:
                - akosha_ready (bool): True if all services initialized
                - embedding_service (EmbeddingService): Embedding generation
                - analytics_service (TimeSeriesAnalytics): Analytics engine
                - tracer (Tracer): OpenTelemetry tracer
                - meter (Meter): OpenTelemetry meter

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
        from akosha.processing.knowledge_graph import KnowledgeGraphBuilder
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

        # In lite mode, skip analytics and knowledge graph
        if not is_lite_mode:
            analytics_service = TimeSeriesAnalytics()
            logger.info("Time-series analytics service initialized")

            graph_builder = KnowledgeGraphBuilder()
            logger.info("Knowledge graph builder initialized")
        else:
            analytics_service = None
            graph_builder = None

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

        # Register MCP tools with Phase 2 services
        from akosha.mcp.tools import register_all_tools

        register_all_tools(
            app,
            embedding_service=embedding_service,
            analytics_service=analytics_service,
            graph_builder=graph_builder,
            hot_store=hot_store,
        )

        # Phase 0: register this component's MCP endpoint to Dhara
        mcp_url = _get_mcp_url()
        await _register_component_to_dhara(mcp_url)

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

        # Shutdown telemetry (synchronous call, no await needed)
        shutdown_telemetry()
        logger.info(f"{APP_NAME} shutdown complete")

    app._mcp_server.lifespan = lifespan

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
