"""Tool profile registration groups for Akosha MCP server.

Maps ToolProfile levels to specific register_*() call lists, controlling
which tools are exposed at startup based on the AKOSHA_TOOL_PROFILE
environment variable.

Profile tiers:
    MINIMAL:  Health probes only.
    STANDARD: Adds core Akosha memory aggregation tools.
    FULL:     Everything including Session-Buddy and PyCharm integration.
"""

from __future__ import annotations

from mcp_common.tools import ToolProfile

MINIMAL_REGISTRATIONS: list[str] = [
    "register_health_tools_akosha",
]

STANDARD_REGISTRATIONS: list[str] = MINIMAL_REGISTRATIONS + [
    "register_akosha_tools",
]

FULL_REGISTRATIONS: list[str] = STANDARD_REGISTRATIONS + [
    "register_session_buddy_tools",
    "register_pycharm_tools",
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
}

REGISTRATION_TOOLS: dict[str, list[str]] = {
    "register_health_tools_akosha": [
        "get_liveness", "get_readiness", "health_check_service",
        "health_check_all", "wait_for_dependency", "wait_for_all_dependencies",
    ],
    "register_akosha_tools": [
        "generate_embedding", "generate_batch_embeddings", "search_all_systems",
        "detect_anomalies", "analyze_trends", "correlate_systems",
        "query_knowledge_graph", "get_system_metrics",
    ],
    "register_session_buddy_tools": ["ingest_session_memory", "get_cross_system_summary"],
    "register_pycharm_tools": [
        "get_ide_diagnostics", "search_code", "get_symbol_info", "find_usages", "pycharm_health",
    ],
}


def get_active_profile(env_var: str = "AKOSHA_TOOL_PROFILE") -> ToolProfile:
    """Read the active tool profile from the environment."""
    return ToolProfile.from_env(env_var)


__all__ = [
    "FULL_REGISTRATIONS", "MINIMAL_REGISTRATIONS", "PROFILE_REGISTRATIONS",
    "REGISTRATION_DESCRIPTIONS", "REGISTRATION_TOOLS", "STANDARD_REGISTRATIONS",
    "get_active_profile",
]
