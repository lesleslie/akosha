"""Per-group registration wrappers for the W0 apply_tool_profile helper.

Each wrapper takes a single FastMCP app and registers its tool group,
creating any required services inline (lite-mode aware). The
PROFILE_REGISTRATIONS / REGISTRATION_MAP dispatch in
:mod:`akosha.mcp.tools.profiles` routes per-profile group lists to
these wrappers via the W0 mcp_common helper.

Groups that depend on services which are not initialized (lite mode)
log and skip — preserving the legacy ``register_all_tools`` semantics
where missing services dropped the affected groups rather than failing
the whole lifespan.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)


def register_health_akosha_group(app: FastMCP) -> None:
    """Register always-on health probes."""
    from akosha.mcp.tools import register_health_tools_akosha

    register_health_tools_akosha(app)
    logger.info("Registered health check tools")


def register_akosha_group(app: FastMCP) -> None:
    """Register core Akosha memory-aggregation tools.

    Resolves services via the same factories the lifespan uses (see
    ``akosha.mcp.server`` ``lifespan``). This keeps registration aligned
    with what the lifespan already initialises — without this, every
    embedding / search / analytics / graph tool group is silently dropped
    because the W0 dispatch path passes only ``app`` to each group.

    Lite-mode handling is preserved: callers that explicitly want to skip
    the analytics subset can pass ``analytics_service=None`` via
    ``register_akosha_tools`` directly — this wrapper mirrors the
    pre-refactor ``register_all_tools`` behaviour of always wiring up
    services that the lifespan has initialised.
    """
    from akosha.mcp.tools.akosha_tools import register_akosha_tools
    from akosha.mcp.tools.tool_registry import FastMCPToolRegistry
    from akosha.processing.analytics import TimeSeriesAnalytics
    from akosha.processing.embeddings import get_embedding_service
    from akosha.processing.knowledge_graph import KnowledgeGraphBuilder

    # ``get_embedding_service`` returns a process-wide singleton that the
    # lifespan has already initialised — same instance as ``server.py``
    # passes through the lifespan context dict.
    embedding_service = get_embedding_service()
    analytics_service = TimeSeriesAnalytics()
    graph_builder = KnowledgeGraphBuilder()

    registry = FastMCPToolRegistry(app)
    register_akosha_tools(
        registry,
        embedding_service=embedding_service,
        analytics_service=analytics_service,
        graph_builder=graph_builder,
    )
    logger.info("Registered Akosha core tools")


def register_cross_repo_group(app: FastMCP) -> None:
    """Register Phase 1 cross-repo capability search tools.

    The capability catalog is seeded at module import time and lives in
    ``akosha.mcp.tools.cross_repo_tools``. No service dependencies, no
    embedding-service required — registration is safe to run at any
    profile tier including lite mode.
    """
    from akosha.mcp.tools.cross_repo_tools import register_cross_repo_tools
    from akosha.mcp.tools.tool_registry import FastMCPToolRegistry

    registry = FastMCPToolRegistry(app)
    register_cross_repo_tools(registry)
    logger.info("Registered cross-repo capability search tools")


async def register_session_buddy_group(app: FastMCP) -> None:
    """Register Session-Buddy integration tools. Skipped if hot_store cannot be built."""
    from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools
    from akosha.mcp.tools.tool_registry import FastMCPToolRegistry

    hot_store = await _try_create_hot_store()
    if hot_store is None:
        logger.info("Skipping Session-Buddy tools: hot_store unavailable")
        return
    registry = FastMCPToolRegistry(app)
    register_session_buddy_tools(registry, hot_store)
    logger.info("Registered Session-Buddy integration tools")


async def register_pycharm_group(app: FastMCP) -> None:
    """Register PyCharm integration tools. Skipped if hot_store cannot be built."""
    from akosha.mcp.tools.pycharm_tools import register_pycharm_tools
    from akosha.mcp.tools.tool_registry import FastMCPToolRegistry

    hot_store = await _try_create_hot_store()
    if hot_store is None:
        logger.info("Skipping PyCharm tools: hot_store unavailable")
        return
    registry = FastMCPToolRegistry(app)
    register_pycharm_tools(registry, hot_store)
    logger.info("Registered PyCharm integration tools")


async def register_otel_query_group(app: FastMCP) -> None:
    """Register OTel trace query tools. Skipped if hot_store cannot be built."""
    from akosha.mcp.tools.otel_tools import register_otel_query_tools

    hot_store = await _try_create_hot_store()
    if hot_store is None:
        logger.info("Skipping OTel query tools: hot_store unavailable")
        return
    register_otel_query_tools(app, hot_store)
    logger.info("Registered OTel query tools")


def register_fitness_group(app: FastMCP) -> None:
    """Register FitnessAnalyzer tools (failure-rate / p99 latency signals).

    Creates a standalone FitnessAnalyzer instance, populates it with
    Bodai component endpoints from Dhara, starts its periodic poll loop,
    and registers its tools. The Dhara-populate + poll-start is best-
    effort: failures are logged (matches legacy ``register_all_tools``
    behavior).

    Critical W1.3 fix: ``_populate_component_endpoints_from_dhara`` MUST
    run before ``register_fitness_tools``, otherwise the analyzer ships
    with an empty target list and ``run_fitness_analysis`` silently
    no-ops or returns empty results. The helper is imported from the
    legacy ``akosha.mcp.tools.__init__`` so the W0 and legacy
    registration paths share a single source of truth for fitness
    bootstrap (no divergence).
    """
    import asyncio

    from akosha.mcp.tools import _populate_component_endpoints_from_dhara
    from akosha.mcp.tools.fitness_tools import init_fitness_analyzer, register_fitness_tools
    from akosha.processing.fitness_analyzer import FitnessAnalyzer

    analyzer = FitnessAnalyzer()
    init_fitness_analyzer(analyzer)

    try:
        loop = asyncio.get_running_loop()
        _fitness_loop_task = loop.create_task(analyzer.start())  # noqa: RUF006
    except RuntimeError:
        logger.debug("No running event loop; fitness analyzer poll loop not started")

    _populate_component_endpoints_from_dhara(analyzer)

    register_fitness_tools(app)
    logger.info("Registered fitness analysis tools")


def register_eventbridge_group(app: FastMCP) -> None:
    """Register EventBridge publisher tool (always-on, disabled by config)."""
    from akosha.config import AkoshaConfig
    from akosha.mcp.tools.eventbridge_tools import register_eventbridge_tools

    # Per-call re-read: each tool invocation calls the lambda, which
    # constructs a fresh AkoshaConfig so operators can flip
    # AKOSHA_EVENTBRIDGE_ENABLED without restarting the MCP server.
    def _enabled_fn() -> bool:
        return AkoshaConfig().eventbridge.enabled

    register_eventbridge_tools(app, enabled_fn=_enabled_fn)
    logger.info("Registered EventBridge publisher tools")


async def _try_create_hot_store():
    """Best-effort hot-store creation + initialization matching the legacy skip-if-missing pattern.

    Returns an *initialised* ``HotStore`` (DuckDB connection opened, schema
    created). The pre-fix version constructed the store but never called
    ``.initialize()``, so every dependent tool (``search_code_patterns``,
    ``find_function_usage``, etc.) crashed at runtime with
    ``RuntimeError("Hot store not initialized")``.
    """
    try:
        from akosha.storage import create_hot_store

        store = create_hot_store()
        await store.initialize()
        return store
    except Exception as exc:
        logger.debug("create_hot_store() failed: %s", exc)
        return None


__all__ = [
    "register_akosha_group",
    "register_cross_repo_group",
    "register_eventbridge_group",
    "register_fitness_group",
    "register_health_akosha_group",
    "register_otel_query_group",
    "register_pycharm_group",
    "register_session_buddy_group",
]
