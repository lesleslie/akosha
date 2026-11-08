"""Guard test: README's MCP tool list must match ``REGISTRATION_TOOLS``.

This pins the documented FULL-profile inventory to the canonical registration
table in :mod:`akosha.mcp.tools.profiles`. Drift between the two surfaces
fails the suite so the README cannot silently list phantom tools or omit
real ones again.

The README is parsed with a simple regex (not full Markdown) because the
section uses a stable one-tool-per-line bullet pattern; we deliberately
avoid a Markdown parser to keep the test dependency-free.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from akosha.mcp.tools.profiles import REGISTRATION_TOOLS

README_PATH = Path(__file__).resolve().parents[2] / "README.md"

# Tool names that mean the SAME thing on every server that serves them, so
# they stay shared and bare (naming convention, rule 3 — two tools that
# share a name must mean the same thing, or be renamed).
#
# Two groups:
#
# * The mcp-common baseline — one implementation fanned out across servers.
#   Prefixing them per server would register the same function under four
#   different names and break the fleet guard's allowlist.
# * The same-meaning set — each server's own health (``get_health``), the
#   shared PyCharm proxy all three servers point at (``pycharm_health``),
#   and the fleet-wide trace contract (``query_local_traces``). A caller
#   asking "the same call on every server" must not have to know which
#   server to ask.
#
# The fleet-wide shared set also contains ``track_channel_session`` and
# ``get_channel_sessions``, which only session-buddy serves; they are not
# part of akosha's surface.
SHARED_BASELINE_TOOLS: frozenset[str] = frozenset(
    {
        "get_liveness",
        "get_readiness",
        "health_check_all",
        "health_check_service",
        "wait_for_dependency",
        "wait_for_all_dependencies",
        "discover_tools",
        "get_health",
        "pycharm_health",
        "query_local_traces",
    }
)

# The three same-meaning names akosha serves. They are asserted on the LIVE
# tool surface because ``get_health`` is registered in ``create_app()`` and
# ``discover_tools`` by the W0 helper — neither is in ``REGISTRATION_TOOLS``.
SAME_MEANING_TOOLS: tuple[str, ...] = (
    "get_health",
    "pycharm_health",
    "query_local_traces",
)

# Tools explicitly listed in the README inventory block. Keep this list in
# sync with the bullets the README actually shows under "MCP Tools".
_DOC_TOOLS: set[str] = {
    "get_liveness",
    "get_readiness",
    "health_check_service",
    "health_check_all",
    "wait_for_dependency",
    "wait_for_all_dependencies",
    "akosha_generate_embedding",
    "akosha_generate_batch_embeddings",
    "akosha_search_all_systems",
    "akosha_detect_anomalies",
    "akosha_analyze_trends",
    "akosha_correlate_systems",
    "akosha_query_knowledge_graph",
    "akosha_get_system_metrics",
    "akosha_store_memory",
    "akosha_batch_store_memories",
    "akosha_search_code_patterns",
    "akosha_get_code_problems",
    "akosha_find_function_usage",
    "akosha_analyze_imports",
    "pycharm_health",
    "query_local_traces",
    "akosha_search_spans",
    "akosha_spans_summary",
    "akosha_force_retention_sweep",
    "akosha_run_fitness_analysis",
    "akosha_get_fitness_analyzer_status",
    "akosha_publish_to_eventbridge",
    "akosha_cross_repo_capability_search",
    "akosha_list_skills",
    "akosha_get_skill",
    "akosha_list_agents",
    "akosha_get_agent",
    "akosha_list_ecosystem_skills",
}


def _read_readme_tools() -> set[str]:
    """Extract tool names from the README's MCP Tools inventory block."""
    text = README_PATH.read_text(encoding="utf-8")
    block = re.search(
        r"### MCP Tools.*?(?=^##\s|\Z)",
        text,
        re.DOTALL | re.MULTILINE,
    )
    if block is None:
        pytest.fail("README.md missing '### MCP Tools' inventory section")
    return set(re.findall(r"`([a-z_][a-z0-9_]*)`", block.group(0)))


@pytest.mark.unit
def test_readme_lists_every_registered_full_profile_tool() -> None:
    """Every FULL-profile tool must appear in the README inventory."""
    registered = {t for tools in REGISTRATION_TOOLS.values() for t in tools}
    documented = _read_readme_tools()
    missing = registered - documented
    assert not missing, (
        f"README.md MCP Tools section is missing FULL-profile tools: {sorted(missing)}"
    )


@pytest.mark.unit
def test_readme_inventory_has_no_phantom_tools() -> None:
    """The README inventory must not list tools that aren't registered."""
    registered = {t for tools in REGISTRATION_TOOLS.values() for t in tools}
    documented = _read_readme_tools()
    # Restrict to bullets that look like MCP tool names (snake_case) so we
    # don't flag unrelated identifiers the regex happens to catch.
    documented_tools = {name for name in documented if "_" in name}
    phantom = documented_tools - registered
    assert not phantom, f"README.md MCP Tools section lists unregistered tools: {sorted(phantom)}"


@pytest.mark.unit
def test_full_profile_count_matches_documented_count() -> None:
    """Total FULL-profile tool count must equal the README's claim (34).

    Phase 1 added 2 tools (``akosha_list_skills`` + ``akosha_get_skill``)
    → 28. Phase 3/4 added 3 more (``akosha_list_agents``,
    ``akosha_get_agent``, ``akosha_list_ecosystem_skills``) → 31. Read-path R-2
    added ``akosha_search_spans`` → 32; R-3 added ``akosha_spans_summary`` → 33.
    The retention sweep trigger added ``akosha_force_retention_sweep`` → 34.
    """
    registered = {t for tools in REGISTRATION_TOOLS.values() for t in tools}
    documented = _read_readme_tools()
    documented_tools = {name for name in documented if "_" in name}
    assert len(registered) == 34, f"Expected 34 FULL-profile tools, found {len(registered)}"
    assert len(documented_tools) == 34, (
        f"README documents {len(documented_tools)} tools but expected 34"
    )


@pytest.mark.unit
def test_doc_tools_set_equals_readme_bullets() -> None:
    """The hard-coded doc set must equal what the README actually lists.

    This protects against the test drifting silently out of sync with the
    README; either fix the README or update ``_DOC_TOOLS`` here.
    """
    documented = _read_readme_tools()
    documented_tools = {name for name in documented if "_" in name}
    assert documented_tools == _DOC_TOOLS, (
        f"README tools ({sorted(documented_tools)}) disagree with "
        f"_DOC_TOOLS ({sorted(_DOC_TOOLS - documented_tools)} missing, "
        f"{sorted(_DOC_TOOLS & documented_tools)} unexpected)"
    )


@pytest.mark.unit
def test_component_specific_tools_carry_the_akosha_prefix() -> None:
    """Every component-specific tool name begins with ``akosha_``.

    The fleet-wide naming convention
    (``docs/decisions/2026-11-08-mcp-tool-component-prefix-convention.md``)
    requires every Bodai-maintained MCP server to prefix its tool names with
    the component name, so the Claude Code TUI picker can filter by
    component.

    The names in :data:`SHARED_BASELINE_TOOLS` are the documented exception:
    they mean the same thing on every server that exposes them, so they stay
    shared and bare.
    """
    registered = [name for tools in REGISTRATION_TOOLS.values() for name in tools]
    unprefixed = [
        name
        for name in registered
        if not name.startswith("akosha_") and name not in SHARED_BASELINE_TOOLS
    ]
    assert not unprefixed, (
        f"Found {len(unprefixed)} tool name(s) without the 'akosha_' prefix in "
        f"REGISTRATION_TOOLS: {sorted(unprefixed)}. Every component-specific "
        f"tool must carry the component prefix."
    )


@pytest.mark.unit
def test_shared_baseline_names_stay_bare() -> None:
    """The shared-baseline names are registered unprefixed.

    Two rules from the naming convention put them here:

    * The seven mcp-common baseline names are one implementation fanned out
      across servers. Prefixing them per server would register the same
      function under a different name on each server.
    * The same-meaning set (``get_health``, ``pycharm_health``,
      ``query_local_traces``) must mean the same thing wherever it is
      served, so a caller asking "the same call on every server" does not
      have to know which server to ask. Renaming one of them would break
      that contract.

    Both sets must stay bare, and must not acquire an ``akosha_`` prefix.
    """
    registered = {name for tools in REGISTRATION_TOOLS.values() for name in tools}
    shared_registered = SHARED_BASELINE_TOOLS & registered
    assert shared_registered, (
        "expected the shared baseline in REGISTRATION_TOOLS: "
        f"{sorted(SHARED_BASELINE_TOOLS)}"
    )
    prefixed = sorted(name for name in shared_registered if name.startswith("akosha_"))
    assert not prefixed, f"Shared-baseline tools must stay bare: {prefixed}"
    # The same-meaning names akosha puts in the profile table must be
    # present, unprefixed.
    for name in ("pycharm_health", "query_local_traces"):
        assert name in registered, f"{name} must be registered bare"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_same_meaning_tools_are_served_bare() -> None:
    """``get_health``, ``pycharm_health`` and ``query_local_traces`` are served bare.

    These three mean the same thing on every server that serves them, so
    they stay shared and unprefixed (naming convention, rule 3). The
    assertion runs against the LIVE tool surface because ``get_health`` is
    registered in ``create_app()`` rather than in the profile table.

    A future sweep that prefixes one of them would break the contract for
    callers asking "the same call on every server" — this is the guard that
    catches it.

    Only the groups that serve the three tools are registered (the same
    targeted-dispatch shape ``test_wiring.py`` uses), so the test does not
    start the fitness analyzer's poll loop or the EventBridge publisher.
    """
    from mcp_common.tools import ToolProfile
    from mcp_common.tools.dispatch import _apply_tool_profile

    from akosha.mcp.server import create_app
    from akosha.mcp.tools.profiles import REGISTRATION_MAP

    app = create_app()
    await _apply_tool_profile(
        app,
        profile_env_var="AKOSHA_TOOL_PROFILE",
        registrations={
            ToolProfile.MINIMAL: ["register_health_tools_akosha"],
            ToolProfile.STANDARD: [],
            ToolProfile.FULL: [
                "register_health_tools_akosha",
                "register_pycharm_tools",
                "register_otel_query_tools",
            ],
        },
        registration_map=REGISTRATION_MAP,
        register_all_fn=None,
        mandatory_groups={"register_health_tools_akosha"},
        essential_tool_names=set(),
        discovery_fn=None,
        yaml_loader=None,
    )

    tool_names = {t.name for t in await app.list_tools()}
    for name in SAME_MEANING_TOOLS:
        assert name in tool_names, (
            f"{name} must be served unprefixed. Shared-meaning tools stay bare. "
            f"Served: {sorted(tool_names)}"
        )
        prefixed = f"akosha_{name}"
        assert prefixed not in tool_names, (
            f"{prefixed} must NOT be served — {name} means the same thing on "
            f"every server, so it stays shared and bare."
        )
