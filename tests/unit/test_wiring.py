"""Verify akosha's mcp server is wired to the W0 apply_tool_profile() helper.

Three layers of coverage:

1. **AST guard** — ``akosha/mcp/server.py`` must call
   ``_apply_tool_profile`` (async entry) or ``apply_tool_profile``
   (sync wrapper) so all per-group registration goes through the
   single mcp-common dispatch surface.
2. **Golden fixture parity** — the tool set exposed at each
   ToolProfile level must match the golden fixture captured BEFORE
   the refactor (the contract this task wired in).
3. **Mandatory-groups invariant** — every registration_map key in
   ``AKOSHA_MANDATORY_GROUPS`` must be present in the map. The W0
   helper raises ``ValueError`` if a mandatory key is missing.

See ``scripts/capture_profile_fixtures.py`` for the pre-refactor
golden fixture capture flow.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _server_path() -> Path:
    return ROOT / "akosha" / "mcp" / "server.py"


def _fixtures_dir() -> Path:
    return ROOT / "tests" / "fixtures"


def test_server_calls_apply_tool_profile() -> None:
    """akosha/mcp/server.py must call apply_tool_profile() (sync or async)."""
    server = _server_path()
    tree = ast.parse(server.read_text())
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match either ``_apply_tool_profile(...)`` or
        # ``apply_tool_profile(...)`` as a bare-name call OR an
        # attribute access (``mcp_common.tools.dispatch._apply_tool_profile``).
        name = getattr(func, "id", None)
        attr = getattr(func, "attr", None) if hasattr(func, "attr") else None
        if name in {"_apply_tool_profile", "apply_tool_profile"}:
            found = True
            break
        if attr in {"_apply_tool_profile", "apply_tool_profile"}:
            found = True
            break
    assert found, (
        "akosha/mcp/server.py must call _apply_tool_profile() or apply_tool_profile()"
    )


@pytest.mark.parametrize(
    ("profile", "fixture_name"),
    [
        ("minimal", "minimal"),
        ("standard", "standard"),
        ("full", "full"),
    ],
)
@pytest.mark.asyncio
async def test_profile_matches_golden_fixture(
    profile: str, fixture_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tools at the given profile match the captured golden fixture.

    The capture script (``scripts/capture_profile_fixtures.py``) was
    run BEFORE the refactor to lock the legacy ``register_all_tools``
    output. After the W0 dispatch refactor, the same fixtures must
    hold — the refactor is byte-for-byte equivalent at the FastMCP
    ``list_tools()`` level.
    """
    monkeypatch.setenv("AKOSHA_TOOL_PROFILE", profile)

    from fastmcp import FastMCP

    from akosha.mcp.tools import register_all_tools

    app = FastMCP(name=f"akosha-fixture-{profile}", version="0.0.0")
    register_all_tools(app)

    actual = sorted(t.name for t in await app.list_tools())
    fixture = _fixtures_dir() / fixture_name / "tool_names.json"
    expected = json.loads(fixture.read_text())
    assert actual == expected, (
        f"profile={profile} mismatch: "
        f"missing={sorted(set(expected) - set(actual))} "
        f"unexpected={sorted(set(actual) - set(expected))}"
    )


def test_mandatory_groups_subset_of_registration_map() -> None:
    """AKOSHA_MANDATORY_GROUPS must be a subset of REGISTRATION_MAP keys.

    The W0 helper's mandatory_groups dispatch (Step 2a in
    ``_apply_tool_profile_async``) raises ``ValueError`` if a mandatory
    key is missing from the registration map. This invariant must hold
    at all three profile levels (the helper is uniform across them).
    """
    from akosha.mcp.tools.profiles import (
        AKOSHA_MANDATORY_GROUPS,
        REGISTRATION_MAP,
    )

    map_keys = set(REGISTRATION_MAP.keys())
    mandatory = AKOSHA_MANDATORY_GROUPS
    missing = mandatory - map_keys
    assert missing == set(), (
        f"Aakosha mandatory groups not in REGISTRATION_MAP: "
        f"{sorted(missing)}. Add them via REGISTRATION_MAP or remove "
        f"from AKOSHA_MANDATORY_GROUPS."
    )
