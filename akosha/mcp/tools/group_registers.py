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

import contextlib
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)


def register_health_akosha_group(app: FastMCP) -> None:
    """Register always-on health probes."""
    from akosha.mcp.tools import register_health_tools_akosha

    register_health_tools_akosha(app)
    logger.info("Registered health check tools")


async def register_akosha_group(app: FastMCP) -> None:
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

    Hot-store wiring (Sub-plan C): best-effort ``HotStore`` creation is
    threaded through to ``register_akosha_tools`` so
    ``search_all_systems`` reads real data instead of returning the
    legacy hard-coded mock. When creation fails (lite mode / DuckDB
    missing), ``hot_store`` is ``None`` and the tool falls back to an
    informational result.

    Wave 5: when the lifespan has published its HotStore and
    KnowledgeGraphBuilder via ``set_shared_*``, reuse those instances so
    the data the CodeGraphIngester writes and the kg_refresh task
    populates is visible to the tool handlers (they would otherwise read
    from a different in-memory DuckDB).
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
    # Wave 5: prefer the lifespan-owned KnowledgeGraphBuilder so the
    # periodic-refresh task populates the same instance the tools read.
    graph_builder = _get_shared_kg_builder() or KnowledgeGraphBuilder()

    # Best-effort HotStore creation via the shared async helper. Mirrors
    # the pattern used by ``register_session_buddy_group`` /
    # ``register_pycharm_group`` — when creation fails (lite mode /
    # DuckDB missing), ``hot_store`` ends up ``None`` and the tool falls
    # back to the informational branch. Wave 5: prefer the lifespan-
    # owned instance so the CodeGraphIngester's writes are visible here.
    hot_store = await _try_create_hot_store()

    registry = FastMCPToolRegistry(app)
    register_akosha_tools(
        registry,
        embedding_service=embedding_service,
        analytics_service=analytics_service,
        graph_builder=graph_builder,
        hot_store=hot_store,
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


def register_skill_tools_group(app: FastMCP) -> None:
    """Register Phase 1 ``list_skills`` / ``get_skill`` MCP tools.

    No service dependencies — the tools read the static catalog from
    ``akosha/mcp/skills_catalog/`` and access the :class:`SkillsSigner`
    via the module-level singleton populated at lifespan startup
    (``akosha.mcp.signer_feed.init_signer_feed_state``). Both tools
    return informative error envelopes if the signer has not been
    initialized yet (pre-lifespan / lite mode).
    """
    from akosha.mcp.tools.skill_tools import register_skill_tools

    register_skill_tools(app)
    logger.info("Registered skill_tools (list_skills + get_skill)")


def register_agents_tools_group(app: FastMCP) -> None:
    """Register Phase 3 ``list_agents`` / ``get_agent`` MCP tools.

    Mirrors :func:`register_skill_tools_group`'s shape — no service
    dependencies, reads the static catalog from
    ``akosha/mcp/tools/agents/``, accesses the lifespan-owned
    :class:`SkillsSigner` via the module-level singleton. Both tools
    return informative error envelopes if the signer has not been
    initialized yet (pre-lifespan / lite mode).

    **B-6 critical contract**: the ``akosha_get_agent`` tool returns
    ``body == system_prompt``. Without this, the installer would ship
    non-functional agents. The end-to-end test
    ``tests/integration/test_get_agent_e2e.py`` asserts this invariant.
    """
    from akosha.mcp.tools.agents_tools import register_agents_tools

    register_agents_tools(app)
    logger.info("Registered agents_tools (list_agents + get_agent)")


def register_ecosystem_skills_group(app: FastMCP) -> None:
    """Register Phase 4 federation tool ``akosha_list_ecosystem_skills``.

    Aggregates ``mcp__<server>__list_skills`` responses from all 5 Bodai
    servers with a 1-second per-server timeout, per-server circuit
    breaker (3 failures in 30s → 60s skip), atomic file-cache at
    ``~/.akosha/cache/ecosystem_skills.json``, and cursor-based
    pagination. No service dependencies on lifespan-owned singletons;
    the cache and circuit-breaker registry are constructed inline.

    Federation is registered through the lifecycle via
    ``register_ecosystem_skills`` (kept distinct from the W0 wrapper for
    direct test access — the same callable is invoked here).
    """
    from akosha.mcp.tools.ecosystem_skills import register_ecosystem_skills

    register_ecosystem_skills(app)
    logger.info("Registered ecosystem_skills federation tool")


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

    Wave 5: when the lifespan has published a shared HotStore via
    ``akosha.mcp.server.set_shared_hot_store``, return that instance
    directly so the tool handlers read from the same database the
    CodeGraphIngester writes to. Falls back to per-call construction
    when the lifespan hasn't run (e.g. tests bypassing the full
    lifespan path) — preserves the pre-Wave-5 behaviour.
    """
    shared: Any = None
    with contextlib.suppress(Exception):
        from akosha.mcp.server import get_shared_hot_store  # type: ignore[attr-defined]

        shared = get_shared_hot_store()
    # ``get_shared_hot_store`` may not be importable in some test
    # contexts (suppress catches the ImportError); fall through to
    # per-call construction when ``shared`` is still None.
    if shared is not None:
        return shared

    try:
        from akosha.storage import create_hot_store

        store = create_hot_store()
        await store.initialize()
        return store
    except Exception as exc:
        logger.debug("create_hot_store() failed: %s", exc)
        return None


def _get_shared_kg_builder():
    """Return the lifespan-owned KnowledgeGraphBuilder, or ``None``.

    Mirrors :func:`_try_create_hot_store`'s shared-instance preference.
    Used by :func:`register_akosha_group` so the graph the periodic-refresh
    task populates is the same instance the ``get_graph_statistics``
    tool reads.
    """
    try:
        from akosha.mcp.server import get_shared_kg_builder

        return get_shared_kg_builder()
    except Exception:
        return None


__all__ = [
    "register_agents_tools_group",
    "register_akosha_group",
    "register_cross_repo_group",
    "register_ecosystem_skills_group",
    "register_eventbridge_group",
    "register_fitness_group",
    "register_health_akosha_group",
    "register_otel_query_group",
    "register_pycharm_group",
    "register_session_buddy_group",
    "register_skill_tools_group",
]
