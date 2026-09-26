"""Akosha MCP tools - Universal memory aggregation."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from mcp_common.clients.common_mcp_client import CommonMCPClient
    from mcp_common.tools import ToolProfile

from mcp_common.health import DependencyConfig, register_health_tools
from mcp_common.tools import ToolProfile  # runtime: used in discover_tools hint

from akosha.mcp.tools.agents_tools import register_agents_tools  # Phase 3
from akosha.mcp.tools.akosha_tools import (  # noqa: F401
    register_akosha_tools,
    register_code_graph_tools,
)
from akosha.mcp.tools.ecosystem_skills import (
    register_ecosystem_skills,
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
from akosha.mcp.tools.skill_tools import register_skill_tools

logger = logging.getLogger(__name__)

SERVICE_NAME = "akosha"
SERVICE_VERSION = "0.17.5"
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
    "register_skill_tools": register_skill_tools,
    "register_agents_tools": register_agents_tools,
    "register_ecosystem_skills": register_ecosystem_skills,
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
            hot_store=hot_store,
        )
        logger.info("Registered Akosha core tools")

    if "register_session_buddy_tools" in allowed and hot_store:
        register_session_buddy_tools(registry, hot_store)
        logger.info("Registered Session-Buddy integration tools")

    if "register_pycharm_tools" in allowed and hot_store:
        register_pycharm_tools(registry, hot_store)
        logger.info("Registered PyCharm integration tools")

    if "register_skill_tools" in allowed:
        # Phase 1: list_skills + get_skill. No service dependency —
        # the lifespan-owned SkillsSigner is read via the module-level
        # singleton populated at startup. Both tools return informative
        # error envelopes if the signer has not been initialized yet.
        register_skill_tools(app)
        logger.info("Registered skill_tools (list_skills + get_skill)")

    if "register_agents_tools" in allowed:
        # Phase 3: list_agents + get_agent. Same pattern as Phase 1 —
        # no service dependency, signer singleton. Per plan §11 B-6,
        # ``get_agent`` returns ``body == system_prompt`` so the
        # installer writes a fully-functional agent file.
        register_agents_tools(app)
        logger.info("Registered agents_tools (list_agents + get_agent)")

    if "register_ecosystem_skills" in allowed:
        # Phase 4: akosha_list_ecosystem_skills — federates across all 5
        # Bodai servers. No lifespan dependency; the file cache and
        # circuit-breaker registry are constructed on demand. Registered
        # at every profile tier per AKOSHA_MANDATORY_GROUPS.
        register_ecosystem_skills(app)
        logger.info("Registered ecosystem_skills federation tool")

    # OTel query tools are wired through the W0 profile path
    # (akosha.mcp.tools.profiles.register_otel_query_group) via the
    # lifespan's _apply_tool_profile call. The legacy register_all_tools
    # path here is dead code — see
    # https://memory/mcp-tool-registration-dual-track-drift-pattern.md
    # for the dual-track audit that identified this drift.

    if "register_fitness_tools" in allowed:
        from akosha.processing.fitness_analyzer import FitnessAnalyzer

        analyzer = FitnessAnalyzer()
        init_fitness_analyzer(analyzer)

        # Start the periodic analysis loop (C4 fix — was missing entirely).
        # create_task returns a Task that is kept alive by the running event loop.
        import asyncio

        _fitness_loop_task = asyncio.create_task(analyzer.start())  # noqa: RUF006

        register_fitness_tools(app)
        logger.info("Registered fitness analysis tools")

    if "register_eventbridge_tools" in allowed:
        from akosha.config import AkoshaConfig
        from akosha.mcp.tools.eventbridge_tools import register_eventbridge_tools

        # Per-call re-read: each tool invocation calls the lambda, which
        # constructs a fresh AkoshaConfig. Pydantic v1 reads the env vars
        # in EventBridgeConfig.__init__, so operators can flip
        # AKOSHA_EVENTBRIDGE_ENABLED without restarting the MCP server.
        # The cost of constructing a Pydantic model (~ms) per call is
        # acceptable for an MCP tool invocation.
        def _eventbridge_enabled_fn() -> bool:
            return AkoshaConfig().eventbridge.enabled

        register_eventbridge_tools(app, enabled_fn=_eventbridge_enabled_fn)
        logger.info(
            "Registered EventBridge publisher tools (per-call re-read of "
            "AkoshaConfig().eventbridge.enabled)"
        )

    if "register_cross_repo_tools" in allowed:
        from akosha.mcp.tools.cross_repo_tools import register_cross_repo_tools
        from akosha.mcp.tools.tool_registry import FastMCPToolRegistry

        cross_repo_registry = FastMCPToolRegistry(app)
        register_cross_repo_tools(cross_repo_registry)
        logger.info("Registered cross-repo capability search tools")

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

        # The hint is profile-aware: ``full`` is the default, so the
        # static "Set AKOSHA_TOOL_PROFILE=full to enable all tools" was
        # misleading at every profile level. Show a downgrade hint only
        # when the operator has explicitly opted into a restricted
        # profile, and an upgrade hint when they could see more.
        if profile == ToolProfile.FULL:
            hint = (
                "All tool groups loaded. To restrict, set "
                "AKOSHA_TOOL_PROFILE=standard (or minimal)."
            )
        elif profile == ToolProfile.STANDARD:
            hint = (
                "Standard profile active. To enable Session-Buddy, PyCharm, "
                "OTel, Fitness, EventBridge, and cross-repo tools, set "
                "AKOSHA_TOOL_PROFILE=full (or unset the env var to use the full default)."
            )
        else:  # MINIMAL
            hint = (
                "Minimal profile active (health probes only). Set "
                "AKOSHA_TOOL_PROFILE=standard or =full (default) to load "
                "core Akosha tools."
            )

        return {
            "status": "success",
            "profile": profile.value,
            "query": query,
            "loaded_tools": loaded,
            "loaded_count": len(loaded),
            "not_loaded_tools": not_loaded,
            "not_loaded_count": len(not_loaded),
            "hint": hint,
        }


__all__ = [
    "register_all_tools",
    "register_ecosystem_skills",
]
