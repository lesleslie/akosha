"""Akosha MCP tools - Universal memory aggregation."""

from __future__ import annotations

import logging
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from mcp_common.tools import ToolProfile

    from akosha.mcp.client import DharaServiceRegistryClient

from mcp_common.health import DependencyConfig, register_health_tools

from akosha.mcp.tools.akosha_tools import (  # noqa: F401
    register_akosha_tools,
    register_code_graph_tools,
)
from akosha.mcp.tools.fitness_tools import (
    init_fitness_analyzer,
    register_fitness_tools,
)
from akosha.mcp.tools.otel_tools import register_otel_query_tools  # noqa: F401
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

SERVICE_NAME = "akosha"
SERVICE_VERSION = "0.1.0"
SERVICE_START_TIME = time.time()

DEFAULT_DEPENDENCIES: dict[str, DependencyConfig] = {
    "session_buddy": DependencyConfig(
        host="localhost",
        port=8678,
        required=False,
        timeout_seconds=10,
    ),
    "mahavishnu": DependencyConfig(
        host="localhost",
        port=8680,
        required=False,
        timeout_seconds=10,
    ),
}

# Map registration names to callables for profile-driven dispatch
_ALL_REGISTERS: dict[str, Any] = {
    "register_akosha_tools": register_akosha_tools,
    "register_session_buddy_tools": register_session_buddy_tools,
    "register_pycharm_tools": register_pycharm_tools,
}


def register_health_tools_akosha(app: Any) -> None:
    """Register Akosha health tools through the shared MCP-common contract."""
    register_health_tools(
        mcp=app,
        service_name=SERVICE_NAME,
        version=SERVICE_VERSION,
        start_time=SERVICE_START_TIME,
        dependencies=DEFAULT_DEPENDENCIES,
    )


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

    if "register_otel_query_tools" in allowed and hot_store:
        from akosha.mcp.tools.otel_tools import register_otel_query_tools

        register_otel_query_tools(app, hot_store)
        logger.info("Registered OTel query tools")

    if "register_fitness_tools" in allowed:
        from akosha.processing.fitness_analyzer import FitnessAnalyzer

        analyzer = FitnessAnalyzer()
        init_fitness_analyzer(analyzer)

        # Start the periodic analysis loop (C4 fix — was missing entirely).
        # create_task returns a Task that is kept alive by the running event loop.
        import asyncio

        _fitness_loop_task = asyncio.create_task(analyzer.start())  # noqa: RUF006

        # Populate component endpoints from Dhara (Phase 0 read step)
        # Each Bodai component writes its MCP URL to component_endpoint/{name}
        # on startup. Read all registered endpoints so FitnessAnalyzer has targets.
        _populate_component_endpoints_from_dhara(analyzer)

        register_fitness_tools(app)
        logger.info("Registered fitness analysis tools")

    # Always register the discovery meta-tool
    _register_discovery_tool(app, profile)

    logger.info("Akosha MCP tools registration complete (profile=%s)", profile.value)


def _populate_component_endpoints_from_dhara(analyzer: Any) -> None:
    """Read registered component endpoints from Dhara and register them with FitnessAnalyzer.

    Phase 0 specifies that each Bodai component writes its MCP endpoint URL to Dhara
    under key 'component_endpoint/{component_name}'. This function reads all matching
    keys via list_prefix and registers them with the FitnessAnalyzer so it has targets
    to poll.

    Uses the KV time-series 'component_endpoint/' prefix scan since Mahavishnu uses
    dhara_state.put() (not upsert_service) to register its endpoint.
    """
    import asyncio
    import os

    from akosha.mcp.client import DharaServiceRegistryClient

    dhara_url = os.getenv("DHARA_MCP_URL", "http://localhost:8683/mcp")

    try:
        registry = DharaServiceRegistryClient(base_url=dhara_url, timeout=10.0)
        asyncio.get_running_loop()
        _endpoint_task = asyncio.create_task(_populate_async(registry, analyzer))
        logger.debug(
            "FitnessAnalyzer: initiated async endpoint discovery from Dhara (task=%s)",
            id(_endpoint_task),
        )
    except RuntimeError:
        # No running event loop — run synchronously
        try:
            asyncio.run(_populate_async(registry, analyzer))
        except Exception as exc:
            logger.warning(
                "FitnessAnalyzer: could not populate endpoints from Dhara (continuing with empty list): %s",
                exc,
            )


async def _populate_async(registry: DharaServiceRegistryClient, analyzer: Any) -> None:
    """Async helper to populate endpoints from Dhara.

    Scans for registered endpoints. Tries service registry (bodai_component)
    first, then falls back to individual KV gets for known component names.
    """
    discovered = 0

    # Phase 1: try ecosystem service registry (if components registered via upsert_service)
    try:
        services = await registry.list_services(service_type="bodai_component")
        for svc in services:
            component_name = svc.get("service_id", "")
            metadata: dict[str, Any] = svc.get("metadata", {}) or {}
            mcp_url = metadata.get("mcp_url") or metadata.get("url")
            if component_name and mcp_url:
                analyzer.add_component(component_name, mcp_url)
                discovered += 1
                logger.debug(
                    "FitnessAnalyzer: discovered via service registry %s -> %s",
                    component_name,
                    mcp_url,
                )
    except Exception as exc:
        logger.debug("Service registry scan returned no bodai_component services: %s", exc)

    # Phase 2: known component names — direct KV get for each
    # Mahavishnu uses put() to write component_endpoint/{name} in KV store
    known_components = ["mahavishnu", "crackerjack", "session-buddy"]
    for name in known_components:
        with suppress(Exception):
            entry = await registry.get(f"component_endpoint/{name}")
            if entry and isinstance(entry, dict):
                mcp_url = entry.get("url") or entry.get("mcp_url")
                if mcp_url:
                    analyzer.add_component(name, mcp_url)
                    discovered += 1
                    logger.debug(
                        "FitnessAnalyzer: discovered via KV get %s -> %s",
                        name,
                        mcp_url,
                    )

    await registry.aclose()
    logger.info(
        "FitnessAnalyzer: populated %d component endpoints from Dhara",
        discovered,
    )


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
