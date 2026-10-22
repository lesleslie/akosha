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

# Tools explicitly listed in the README inventory block. Keep this list in
# sync with the bullets the README actually shows under "MCP Tools".
_DOC_TOOLS: set[str] = {
    "get_liveness",
    "get_readiness",
    "health_check_service",
    "health_check_all",
    "wait_for_dependency",
    "wait_for_all_dependencies",
    "generate_embedding",
    "generate_batch_embeddings",
    "search_all_systems",
    "detect_anomalies",
    "analyze_trends",
    "correlate_systems",
    "query_knowledge_graph",
    "get_system_metrics",
    "store_memory",
    "batch_store_memories",
    "search_code_patterns",
    "get_code_problems",
    "find_function_usage",
    "analyze_imports",
    "pycharm_health",
    "query_local_traces",
    "search_spans",
    "spans_summary",
    "run_fitness_analysis",
    "get_fitness_analyzer_status",
    "publish_to_eventbridge",
    "cross_repo_capability_search",
    "list_skills",
    "get_skill",
    "list_agents",
    "get_agent",
    "list_ecosystem_skills",
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
    """Total FULL-profile tool count must equal the README's claim (33).

    Phase 1 added 2 tools (``list_skills`` + ``get_skill``)
    → 28. Phase 3/4 added 3 more (``list_agents``,
    ``get_agent``, ``list_ecosystem_skills``) → 31. Read-path R-2
    added ``search_spans`` → 32; R-3 added ``spans_summary`` → 33.
    """
    registered = {t for tools in REGISTRATION_TOOLS.values() for t in tools}
    documented = _read_readme_tools()
    documented_tools = {name for name in documented if "_" in name}
    assert len(registered) == 33, f"Expected 33 FULL-profile tools, found {len(registered)}"
    assert len(documented_tools) == 33, (
        f"README documents {len(documented_tools)} tools but expected 33"
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
def test_no_akosha_prefix_in_registered_names() -> None:
    """REQ-001: no registered tool name begins with ``akosha_``.

    The MCP harness prepends the server name (``mcp__akosha__``) at call
    time; the underlying registered name must stay unprefixed. A tool
    named ``akosha_xyz`` would surface to clients as ``mcp__akosha__akosha_xyz``
    — a duplicate-prefix bug. This walks the registration map the same way
    the other inventory tests do and fails on any ``akosha_`` prefix.
    """
    registered = [name for tools in REGISTRATION_TOOLS.values() for name in tools]
    prefixed = [name for name in registered if name.startswith("akosha_")]
    assert not prefixed, (
        f"Found {len(prefixed)} tool name(s) starting with 'akosha_' in "
        f"REGISTRATION_TOOLS: {sorted(prefixed)}"
    )
