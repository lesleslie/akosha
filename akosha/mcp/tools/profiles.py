"""Tool profile registration groups for Akosha MCP server.

Maps ToolProfile levels to specific register_*() call lists, controlling
which tools are exposed at startup based on the AKOSHA_TOOL_PROFILE
environment variable.

Profile tiers:
    MINIMAL:  Health probes only.
    STANDARD: Adds core Akosha memory aggregation tools.
    FULL:     Everything including Session-Buddy and PyCharm integration.

The dispatch surface (REGISTRATION_MAP + AKOSHA_MANDATORY_GROUPS) is
consumed by :func:`mcp_common.tools.dispatch._apply_tool_profile_async`
when called from :func:`akosha.mcp.server.create_app`'s lifespan.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mcp_common.tools import ToolProfile

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastmcp import FastMCP


MINIMAL_REGISTRATIONS: list[str] = [
    "register_health_tools_akosha",
]

STANDARD_REGISTRATIONS: list[str] = [*MINIMAL_REGISTRATIONS, "register_akosha_tools"]

FULL_REGISTRATIONS: list[str] = [
    *STANDARD_REGISTRATIONS,
    "register_session_buddy_tools",
    "register_pycharm_tools",
    "register_otel_query_tools",
    "register_fitness_tools",
    "register_eventbridge_tools",
]

PROFILE_REGISTRATIONS: dict[ToolProfile, list[str]] = {
    ToolProfile.MINIMAL: MINIMAL_REGISTRATIONS,
    ToolProfile.STANDARD: STANDARD_REGISTRATIONS,
    ToolProfile.FULL: FULL_REGISTRATIONS,
}

REGISTRATION_DESCRIPTIONS: dict[str, str] = {
    "register_health_tools_akosha": "Liveness, readiness, and dependency health probes (always loaded)",
    "register_akosha_tools": "Core memory aggregation: embeddings, search, analytics, anomaly detection, knowledge graph",
    "register_session_buddy_tools": "Session-Buddy integration: direct HTTP memory ingestion and cross-system sync",
    "register_pycharm_tools": "IDE diagnostics, code search, symbol info, and find usages via PyCharm",
    "register_otel_query_tools": "OTel trace queries by system_id and attribute filters (Bodai feedback loop)",
    "register_fitness_tools": "Fitness analysis for Bodai routing feedback loop (failure rate, p99 latency per task class)",
    "register_eventbridge_tools": "EventBridge publisher: emit Akosha analytics events to the unified Bodai queue",
}

REGISTRATION_TOOLS: dict[str, list[str]] = {
    "register_health_tools_akosha": [
        "get_liveness",
        "get_readiness",
        "health_check_service",
        "health_check_all",
        "wait_for_dependency",
        "wait_for_all_dependencies",
    ],
    "register_akosha_tools": [
        "generate_embedding",
        "generate_batch_embeddings",
        "search_all_systems",
        "detect_anomalies",
        "analyze_trends",
        "correlate_systems",
        "query_knowledge_graph",
        "get_system_metrics",
        "analyze_changepoints",
        "find_path",
        "get_graph_statistics",
    ],
    "register_session_buddy_tools": ["store_memory", "batch_store_memories"],
    "register_pycharm_tools": [
        "search_code_patterns",
        "get_code_problems",
        "find_function_usage",
        "analyze_imports",
        "pycharm_health",
    ],
    "register_otel_query_tools": ["query_local_traces"],
    "register_fitness_tools": [
        "run_fitness_analysis",
        "get_fitness_analyzer_status",
    ],
    "register_eventbridge_tools": ["publish_to_eventbridge"],
}


def get_active_profile(env_var: str = "AKOSHA_TOOL_PROFILE") -> ToolProfile:
    """Read the active tool profile from the environment."""
    return ToolProfile.from_env(env_var)


# ---------------------------------------------------------------------------
# W0 apply_tool_profile dispatch surface.
#
# REGISTRATION_MAP routes each group key from PROFILE_REGISTRATIONS to a
# per-group registration callable (taking the FastMCP app). AKOSHA_MANDATORY_GROUPS
# is a set of registration_map keys whose registrars run AFTER per-profile
# dispatch at every profile (always-on). Set to a subset of
# REGISTRATION_MAP.keys(); the W0 helper raises if a mandatory key is missing
# from the map.
# ---------------------------------------------------------------------------

# Lazy import to avoid pulling group_registers at module load — the per-group
# wrappers themselves lazy-import their inner modules.
def _build_registration_map() -> dict[str, Callable[[FastMCP], Awaitable[None] | None]]:
    """Build the {group_key: register_fn(app)} map.

    Local import keeps ``akosha.mcp.tools.profiles`` importable without the
    per-group register modules being fully resolved (avoids circular imports
    via the legacy ``register_all_tools`` path).
    """
    from akosha.mcp.tools.group_registers import (
        register_akosha_group,
        register_eventbridge_group,
        register_fitness_group,
        register_health_akosha_group,
        register_otel_query_group,
        register_pycharm_group,
        register_session_buddy_group,
    )

    return {
        "register_health_tools_akosha": register_health_akosha_group,
        "register_akosha_tools": register_akosha_group,
        "register_session_buddy_tools": register_session_buddy_group,
        "register_pycharm_tools": register_pycharm_group,
        "register_otel_query_tools": register_otel_query_group,
        "register_fitness_tools": register_fitness_group,
        "register_eventbridge_tools": register_eventbridge_group,
    }


REGISTRATION_MAP: dict[str, Callable[[FastMCP], Awaitable[None] | None]] = (
    _build_registration_map()
)

# Always-on groups: registered at every profile level in addition to the
# per-profile list. Health checks must be reachable from any profile tier
# (load balancers / orchestrators depend on them).
AKOSHA_MANDATORY_GROUPS: set[str] = {"register_health_tools_akosha"}


__all__ = [
    "AKOSHA_MANDATORY_GROUPS",
    "FULL_REGISTRATIONS",
    "MINIMAL_REGISTRATIONS",
    "PROFILE_REGISTRATIONS",
    "REGISTRATION_DESCRIPTIONS",
    "REGISTRATION_MAP",
    "REGISTRATION_TOOLS",
    "STANDARD_REGISTRATIONS",
    "get_active_profile",
]
