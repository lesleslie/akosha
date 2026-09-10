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
    "akosha_pycharm_health",
    "akosha_query_local_traces",
    "akosha_run_fitness_analysis",
    "akosha_get_fitness_analyzer_status",
    "akosha_publish_to_eventbridge",
    "akosha_cross_repo_capability_search",
    "akosha_list_skills",
    "akosha_get_skill",
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
    """Total FULL-profile tool count must equal the README's claim (28).

    Phase 1 (bodai-skill-agent-distribution plan) added 2 tools
    (``akosha_list_skills`` + ``akosha_get_skill``) to the STANDARD
    profile; they are included in the FULL profile too. Updated to 28
    from the pre-Phase-1 count of 26.
    """
    registered = {t for tools in REGISTRATION_TOOLS.values() for t in tools}
    documented = _read_readme_tools()
    documented_tools = {name for name in documented if "_" in name}
    assert len(registered) == 28, f"Expected 28 FULL-profile tools, found {len(registered)}"
    assert len(documented_tools) == 28, (
        f"README documents {len(documented_tools)} tools but expected 28"
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
