"""Capture golden fixture of tool names at each ToolProfile level.

Modeled on W1.1 mahavishnu's capture script. Used BEFORE refactoring to lock
the current behavior of the legacy `register_all_tools()` dispatch loop.
Subsequent refactors must produce identical tool sets.

Usage:
    cd /Users/les/Projects/akosha
    uv run python scripts/capture_profile_fixtures.py [minimal|standard|full]

Default: capture all three profiles.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


async def _capture(profile: str) -> list[str]:
    """Import akosha.mcp.tools and dispatch register_all_tools() at the given profile."""
    # Force the env var BEFORE any profile resolution
    os.environ["AKOSHA_TOOL_PROFILE"] = profile

    # Drop any cached modules so the env var change is honored
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith("akosha"):
            del sys.modules[mod_name]

    # Disable actual Dhara registration during lifespan startup (best effort)
    os.environ.setdefault("DHARA_MCP_URL", "http://127.0.0.1:1")
    os.environ.setdefault("AKOSHA_MCP_URL", "http://127.0.0.1:1/mcp")

    from fastmcp import FastMCP

    from akosha.mcp.tools import register_all_tools

    app = FastMCP(name=f"akosha-fixture-capture-{profile}", version="0.0.0")

    # Legacy (pre-refactor) dispatch via the existing register_all_tools function.
    # It honors AKOSHA_TOOL_PROFILE via get_active_profile() internally.
    register_all_tools(app)

    tools = await app.list_tools()
    return sorted(t.name for t in tools)


async def main(profiles: list[str]) -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for profile in profiles:
        out_dir = FIXTURES / profile
        out_dir.mkdir(parents=True, exist_ok=True)
        names = await _capture(profile)
        (out_dir / "tool_names.json").write_text(json.dumps(names, indent=2) + "\n")
        print(f"{profile}: {len(names)} tools captured -> {out_dir}/tool_names.json")


if __name__ == "__main__":
    requested = sys.argv[1:] or ["minimal", "standard", "full"]
    asyncio.run(main(requested))
