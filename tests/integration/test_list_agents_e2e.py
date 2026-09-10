"""End-to-end test: akosha_list_agents returns non-empty catalog with non-empty ``system_prompt``.

Per plan §5 Phase 3 task #2 and the Phase 3 exit criteria:

> ``mcp__<server>__list_agents()`` returns ≥1 entry with non-empty
> ``system_prompt`` field.

This test seeds a stand-in MCP server shim, registers
``akosha_list_agents`` against it via ``register_agents_tools``, and
invokes the inner function so the assertion path mirrors what
production dispatchers do. We also assert the
``AKOSHA_MANDATORY_GROUPS`` invariant — agents must be reachable at
the MINIMAL profile tier (B-6 install path).
"""

from __future__ import annotations

from typing import Any

import pytest

from akosha.mcp.tools.agents_tools import register_agents_tools


class _ToolRegistry:
    """Minimal MCP-server stand-in that captures ``app.tool()`` calls.

    Mirrors FastMCP's ``add_tool`` mechanism well enough for the
    tool-function body to be exercised end-to-end. Production code
    registers via ``@app.tool(name="...")`` decorating an async function
    inside :func:`akosha.mcp.tools.agents_tools.register_agents_tools`;
    here we capture the inner function so we can invoke it directly.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Any] = {}

    def tool(self, name: str | None = None) -> Any:
        def decorator(fn: Any) -> Any:
            self._tools[name or fn.__name__] = fn
            return fn

        return decorator

    def get(self, name: str) -> Any:
        return self._tools[name]


@pytest.fixture
def app() -> _ToolRegistry:
    """Return a stand-in MCP app with the agents tools registered."""
    app = _ToolRegistry()
    register_agents_tools(app)
    return app


@pytest.mark.asyncio
async def test_list_agents_returns_non_empty_list(app: _ToolRegistry) -> None:
    """Exit criterion: ``list_agents()`` returns ≥1 entry."""
    fn = app.get("akosha_list_agents")
    assert fn is not None, "akosha_list_agents was not registered"

    result = await fn()

    assert isinstance(result, list)
    assert len(result) >= 1, "list_agents must return at least one agent"


@pytest.mark.asyncio
async def test_list_agents_returns_three_entries(app: _ToolRegistry) -> None:
    """Three starter agents are expected: akosha-specialist, search-agent, pattern-agent."""
    fn = app.get("akosha_list_agents")
    result = await fn()

    assert len(result) == 3
    names = sorted(entry["name"] for entry in result)
    assert names == ["akosha-specialist", "pattern-agent", "search-agent"]


@pytest.mark.asyncio
async def test_list_agents_entries_have_non_empty_system_prompt(app: _ToolRegistry) -> None:
    """B-6 exit criterion: every entry must carry a non-empty ``system_prompt``.

    The body is the FULL system prompt — Claude Code reads it directly.
    Without this, the installer would ship non-functional agents.
    """
    fn = app.get("akosha_list_agents")
    result = await fn()

    for entry in result:
        assert "system_prompt" in entry, f"entry {entry['name']!r} missing system_prompt field"
        sp = entry["system_prompt"]
        assert isinstance(sp, str), f"system_prompt for {entry['name']!r} is not a string"
        assert sp.strip(), f"system_prompt for {entry['name']!r} is empty"
        assert len(sp) >= 200, (
            f"system_prompt for {entry['name']!r} is too short "
            f"({len(sp)} chars); per plan §11 B-6 installer needs a "
            f"real non-trivial body"
        )


@pytest.mark.asyncio
async def test_list_agents_entries_have_required_fields(app: _ToolRegistry) -> None:
    """Each entry must carry the canonical Phase 3 metadata fields."""
    fn = app.get("akosha_list_agents")
    result = await fn()

    required_fields = {
        "schema_version",
        "id",
        "server_key",
        "name",
        "description",
        "version",
        "model",
        "tools",
        "system_prompt",
        "content_hash",
    }
    for entry in result:
        missing = required_fields - set(entry.keys())
        assert not missing, f"entry {entry['name']!r} missing fields: {missing}"
        assert entry["server_key"] == "akosha"
        assert entry["content_hash"] == _expected_hash(entry["system_prompt"])
        assert entry["schema_version"] == 1


@pytest.mark.asyncio
async def test_list_agents_id_format(app: _ToolRegistry) -> None:
    """``id`` must be ``{server_key}:{name}:{version}``."""
    fn = app.get("akosha_list_agents")
    result = await fn()

    for entry in result:
        assert entry["id"] == f"{entry['server_key']}:{entry['name']}:{entry['version']}"


@pytest.mark.asyncio
async def test_list_agents_names_allowlist_compliant(app: _ToolRegistry) -> None:
    """Every ``name`` must satisfy the B-4 path-traversal allowlist."""
    import re

    pattern = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
    fn = app.get("akosha_list_agents")
    result = await fn()

    for entry in result:
        assert pattern.fullmatch(entry["name"]), (
            f"name {entry['name']!r} violates B-4 allowlist"
        )
        assert ".." not in entry["name"]


@pytest.mark.asyncio
async def test_list_agents_in_akosha_mandatory_groups() -> None:
    """B-6 invariant: agents MUST be available at the MINIMAL tier.

    The install path reaches for ``list_agents`` even when running in
    a stripped-down profile. The Phase 3 plan calls for adding
    ``register_agents_tools`` to ``AKOSHA_MANDATORY_GROUPS``.
    """
    from akosha.mcp.tools.profiles import AKOSHA_MANDATORY_GROUPS

    assert "register_agents_tools" in AKOSHA_MANDATORY_GROUPS, (
        "register_agents_tools must be a mandatory group "
        "so list_agents is reachable at MINIMAL tier"
    )


def _expected_hash(system_prompt: str) -> str:
    """Compute lowercase hex SHA-256 of ``system_prompt`` bytes."""
    import hashlib

    return hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
