"""Akosha MCP tools - Universal memory aggregation."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP

from mcp_common.tools import ToolProfile

from akosha.mcp.tools.akosha_tools import register_akosha_tools, register_code_graph_tools
from akosha.mcp.tools.health_tools import register_health_tools_akosha
from akosha.mcp.tools.profiles import (
    FULL_REGISTRATIONS,
    PROFILE_REGISTRATIONS,
    REGISTRATION_DESCRIPTIONS,
    REGISTRATION_TOOLS,
    get_active_profile,
)
from akosha.mcp.tools.pycharm_tools import register_pycharm_tools
from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools

logger = logging.getLogger(__name__)

# Map registration names to callables for profile-driven dispatch
_ALL_REGISTERS: dict[str, Any] = {
    "register_health_tools_akosha": register_health_tools_akosha,
    "register_akosha_tools": register_akosha_tools,
    "register_session_buddy_tools": register_session_buddy_tools,
    "register_pycharm_tools": register_pycharm_tools,
}


def register_all_tools(
    app: FastMCP,
    embedding_service: Any = None,
    analytics_service: Any = None,
    graph_builder: Any = None,
    hot_store: Any = None,
) -> None:
    """Register Akosha MCP tools based on active profile.

    Tools are gated by the AKOSHA_TOOL_PROFILE environment variable.
    Defaults to FULL (all tools) for backward compatibility.

    Args:
        app: FastMCP application
        embedding_service: Embedding generation service (optional)
        analytics_service: Time-series analytics service (optional)
        graph_builder: Knowledge graph builder (optional)
        hot_store: Hot store for code graph storage (optional)
    """
    from akosha.mcp.tools.tool_registry import FastMCPToolRegistry

    profile = get_active_profile()
    allowed = set(PROFILE_REGISTRATIONS[profile])

    logger.info("Akosha tool profile=%s groups=%s", profile.value, sorted(allowed))

    # Always register health tools (mandatory for infrastructure)
    register_health_tools_akosha(app)
    logger.info("Registered health check tools")

    # Registry for tools that need service injection
    registry = FastMCPToolRegistry(app)

    # Profile-gated registrations
    if "register_akosha_tools" in allowed:
        register_akosha_tools(
            registry,
            embedding_service=embedding_service,
            analytics_service=analytics_service,
            graph_builder=graph_builder,
        )
        logger.info("Registered Akosha core tools")

    if "register_session_buddy_tools" in allowed and hot_store:
        register_session_buddy_tools(registry, hot_store)
        logger.info("Registered Session-Buddy integration tools")

    if "register_pycharm_tools" in allowed and hot_store:
        register_pycharm_tools(registry, hot_store)
        logger.info("Registered PyCharm integration tools")

    # Always register the discovery meta-tool
    _register_discovery_tool(app, profile)

    logger.info("Akosha MCP tools registration complete (profile=%s)", profile.value)


def _register_discovery_tool(app: FastMCP, profile: ToolProfile) -> None:
    """Register the discover_tools meta-tool."""

    @app.tool()
    async def discover_tools(query: str | None = None) -> dict[str, Any]:
        """Search for available Akosha tools by name or capability. Shows tools not loaded in current profile."""
        # Build full tool list from all groups
        all_tools: dict[str, str] = {}
        for group_name, tools in REGISTRATION_TOOLS.items():
            desc = REGISTRATION_DESCRIPTIONS.get(group_name, "")
            for tool_name in tools:
                all_tools[tool_name] = desc

        # Apply query filter
        if query:
            q = query.lower()
            all_tools = {n: d for n, d in all_tools.items() if q in n.lower() or q in d.lower()}

        # Determine loaded vs not-loaded based on profile
        profile_groups = PROFILE_REGISTRATIONS.get(profile, FULL_REGISTRATIONS)
        loaded_group_tools: set[str] = set()
        for group_name in profile_groups:
            loaded_group_tools.update(REGISTRATION_TOOLS.get(group_name, []))

        loaded = sorted(set(all_tools.keys()) & loaded_group_tools)
        not_loaded = sorted(set(all_tools.keys()) - loaded_group_tools)

        return {
            "status": "success",
            "profile": profile.value,
            "query": query,
            "loaded_tools": loaded,
            "loaded_count": len(loaded),
            "not_loaded_tools": not_loaded,
            "not_loaded_count": len(not_loaded),
            "hint": "Set AKOSHA_TOOL_PROFILE=full to enable all tools.",
        }


__all__ = ["register_all_tools"]
