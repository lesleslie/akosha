"""Verify akosha's mcp server is wired to the W0 apply_tool_profile() helper.

Five layers of coverage:

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
4. **Profile-registrations invariant** — every group key referenced
   in any ``PROFILE_REGISTRATIONS[profile]`` list must also be in
   ``REGISTRATION_MAP``. The W0 helper raises ``ValueError`` at
   server startup if such a key is referenced (catchable in unit
   tests by checking this invariant directly).
5. **W0 discover_tools schema** — the ``discover_tools`` meta-tool
   registered by the W0 helper returns a ``list[dict]`` payload
   (NOT the legacy dict{status, profile, loaded_count, ...} shape).
   Any caller hitting production routes through this schema, so
   it must be documented in test form here.

See ``scripts/capture_profile_fixtures.py`` for the pre-refactor
golden fixture capture flow.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from mcp_common.tools import ToolProfile

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


@pytest.mark.parametrize(
    "profile",
    [ToolProfile.MINIMAL, ToolProfile.STANDARD, ToolProfile.FULL],
)
def test_profile_registrations_subset_of_map(profile: ToolProfile) -> None:
    """Every name referenced in PROFILE_REGISTRATIONS[profile] must be
    a key in REGISTRATION_MAP.

    The W0 helper raises ``ValueError: Group <name> in registrations
    but not in registration_map`` at server startup if such a key is
    missing. Without this test, that regression is only catchable in
    production — not in unit tests. Caught before W1.3 review round 1
    where a future commit could have added a group to
    ``FULL_REGISTRATIONS`` without registering it.
    """
    from akosha.mcp.tools.profiles import (
        PROFILE_REGISTRATIONS,
        REGISTRATION_MAP,
    )

    map_keys = set(REGISTRATION_MAP.keys())
    referenced = set(PROFILE_REGISTRATIONS[profile])
    missing = referenced - map_keys
    assert missing == set(), (
        f"profile={profile.value}: group keys referenced in "
        f"PROFILE_REGISTRATIONS but missing from REGISTRATION_MAP: "
        f"{sorted(missing)}. Add a per-group wrapper to "
        f"akosha/mcp/tools/group_registers.py and register it in "
        f"REGISTRATION_MAP."
    )


@pytest.mark.asyncio
async def test_w0_discover_tools_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """discover_tools registered by the W0 helper returns ``list[dict]``
    with ``name``/``description``/``inputSchema``/``group`` keys.

    Production routes through the W0 helper (``_apply_tool_profile``
    in ``akosha/mcp/server.py``), which overrides any pre-existing
    ``discover_tools`` registered via the legacy
    ``_register_discovery_tool``. The legacy dict{status, profile,
    loaded_count, ...} schema is preserved only in the legacy
    ``register_all_tools``-backed tests (see
    ``tests/unit/test_mcp_tools_profiles.py``); production callers
    see the W0 schema documented here.
    """
    monkeypatch.setenv("AKOSHA_TOOL_PROFILE", "minimal")

    from fastmcp import FastMCP
    from mcp_common.tools.dispatch import _apply_tool_profile

    from akosha.mcp.tools.profiles import REGISTRATION_MAP

    app = FastMCP(name="akosha-w0-schema", version="0.0.0")
    await _apply_tool_profile(
        app,
        profile_env_var="AKOSHA_TOOL_PROFILE",
        registrations={
            ToolProfile.MINIMAL: ["register_health_tools_akosha"],
            ToolProfile.STANDARD: [],
            ToolProfile.FULL: [],
        },
        registration_map=REGISTRATION_MAP,
        register_all_fn=None,
        mandatory_groups={"register_health_tools_akosha"},
        essential_tool_names=set(),
        discovery_fn=None,
        yaml_loader=None,
    )

    # Find the discover_tools Tool object and invoke its handler directly.
    tools = await app.list_tools()
    discover = next(t for t in tools if t.name == "discover_tools")

    # The handler signature is (query: str | None) -> list[dict].
    # FastMCP Tool exposes ``fn`` for direct invocation.
    result = await discover.fn(query=None)

    assert isinstance(result, list), (
        f"discover_tools must return a list[dict]; got {type(result).__name__}. "
        f"If you see a dict payload here, the W0 helper did NOT register "
        f"the canonical discover_tools — check ``_local_provider.remove_tool`` "
        f"in mcp_common/tools/dispatch.py."
    )
    assert result, "discover_tools returned an empty list"
    first = result[0]
    assert isinstance(first, dict)
    for key in ("name", "description", "inputSchema", "group"):
        assert key in first, (
            f"discover_tools dict entries must include key '{key}' "
            f"(W0 schema); got keys: {sorted(first.keys())}"
        )
