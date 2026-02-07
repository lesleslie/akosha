"""Akosha MCP tools - Universal memory aggregation."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP

from akosha.mcp.tools.akosha_tools import register_akosha_tools, register_code_graph_tools

logger = logging.getLogger(__name__)


def register_all_tools(
    app: FastMCP,
    embedding_service: Any = None,
    analytics_service: Any = None,
    graph_builder: Any = None,
    hot_store: Any = None,
) -> None:
    """Register all Akosha MCP tools.

    Args:
        app: FastMCP application
        embedding_service: Embedding generation service (optional)
        analytics_service: Time-series analytics service (optional)
        graph_builder: Knowledge graph builder (optional)
        hot_store: Hot store for code graph storage (optional)
    """
    from akosha.mcp.tools.tool_registry import FastMCPToolRegistry

    registry = FastMCPToolRegistry(app)

    # Register Akosha tools with Phase 2 services
    register_akosha_tools(
        registry,
        embedding_service=embedding_service,
        analytics_service=analytics_service,
        graph_builder=graph_builder,
    )

    # Register code graph analysis tools if hot_store is available
    if hot_store:
        register_code_graph_tools(registry, hot_store)
        logger.info("Registered code graph analysis tools")
    else:
        logger.info("Hot store not provided, skipping code graph tools")

    logger.info(f"Registered {len(registry.tools)} Akosha MCP tools")


__all__ = ["register_all_tools"]
