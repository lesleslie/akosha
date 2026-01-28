"""Akasha MCP tools - Universal memory aggregation."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP

from akasha_mcp.tools.akasha_tools import register_akasha_tools

logger = logging.getLogger(__name__)


def register_all_tools(
    app: "FastMCP",
    embedding_service=None,
    analytics_service=None,
    graph_builder=None,
) -> None:
    """Register all Akasha MCP tools.

    Args:
        app: FastMCP application
        embedding_service: Embedding generation service (optional)
        analytics_service: Time-series analytics service (optional)
        graph_builder: Knowledge graph builder (optional)
    """
    from akasha_mcp.tools.tool_registry import FastMCPToolRegistry

    registry = FastMCPToolRegistry(app)

    # Register Akasha tools with Phase 2 services
    register_akasha_tools(
        registry,
        embedding_service=embedding_service,
        analytics_service=analytics_service,
        graph_builder=graph_builder,
    )

    logger.info(f"Registered {len(registry.tools)} Akasha MCP tools")


__all__ = ["register_all_tools"]
