"""Akasha MCP Server - Universal Memory Aggregation via MCP.

This MCP server exposes Akasha's cross-system memory intelligence capabilities
through the Model Context Protocol (MCP).

Usage:
    akasha-mcp
    akasha-mcp --port 3002
    python -m akasha_mcp
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Final

from fastmcp import FastMCP

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

# Check optional dependencies
try:
    import importlib.util
    MCP_COMMON_AVAILABLE = importlib.util.find_spec("mcp_common.server") is not None
    RATE_LIMITING_AVAILABLE = importlib.util.find_spec("fastmcp.server.middleware.rate_limiting") is not None
    SERVERPANELS_AVAILABLE = importlib.util.find_spec("mcp_common.ui") is not None
except Exception:
    MCP_COMMON_AVAILABLE = False
    RATE_LIMITING_AVAILABLE = False
    SERVERPANELS_AVAILABLE = False

import logging
logger = logging.getLogger(__name__)

APP_NAME: Final = "akasha-mcp"
APP_VERSION: Final = "0.1.0"


def create_app() -> FastMCP:
    """Create and configure the FastMCP application.

    Returns:
        Configured FastMCP application
    """
    app = FastMCP(
        name=APP_NAME,
        version=APP_VERSION,
    )

    # Custom lifespan for startup/shutdown
    @asynccontextmanager
    async def lifespan(server: Any) -> AsyncGenerator[dict[str, Any]]:
        """Custom lifespan manager for Akasha."""
        logger.info(f"{APP_NAME} v{APP_VERSION} starting up")

        # Initialize Phase 2 services
        from akasha.processing.embeddings import get_embedding_service
        from akasha.processing.analytics import TimeSeriesAnalytics
        from akasha.processing.knowledge_graph import KnowledgeGraphBuilder

        embedding_service = get_embedding_service()
        await embedding_service.initialize()
        logger.info(
            f"Embedding service initialized: "
            f"{'real' if embedding_service.is_available() else 'fallback'} mode"
        )

        analytics_service = TimeSeriesAnalytics()
        logger.info("Time-series analytics service initialized")

        graph_builder = KnowledgeGraphBuilder()
        logger.info("Knowledge graph builder initialized")

        # Register MCP tools with Phase 2 services
        from akasha_mcp.tools import register_all_tools
        register_all_tools(
            app,
            embedding_service=embedding_service,
            analytics_service=analytics_service,
            graph_builder=graph_builder,
        )

        yield {
            "akasha_ready": True,
            "embedding_service": embedding_service,
            "analytics_service": analytics_service,
        }

        logger.info(f"{APP_NAME} shutdown complete")

    app._mcp_server.lifespan = lifespan

    logger.debug("Akasha MCP server created")
    return app


# Lazy initialization pattern
def __getattr__(name: str) -> Any:
    """Lazy app initialization.

    Args:
        name: Attribute name

    Returns:
        App instance
    """
    if name == "app":
        return create_app()
    if name == "http_app":
        return create_app().http_app
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


__all__ = [
    "create_app",
    "APP_NAME",
    "APP_VERSION",
    "MCP_COMMON_AVAILABLE",
    "RATE_LIMITING_AVAILABLE",
    "SERVERPANELS_AVAILABLE",
]
