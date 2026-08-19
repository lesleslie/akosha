"""Doc-drift CI guard tests for akosha.

These tests pin three classes of facts that have drifted in past releases:

1. The total number of MCP tools exposed by the server (matches README/CLAUDE.md claims).
2. Documented environment variables are actually read by the package code.
3. The HTTP ``User-Agent`` string interpolates from ``__version__`` rather than
   hardcoding a version literal.

If a test fails, fix the documentation to match the code *or* fix the code to
match the documentation. The pinned thresholds are deliberately loose (using
``>=`` rather than ``==``) so that adding new tools does not require updating
this file.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Tool count guard
# ---------------------------------------------------------------------------

# Tool count is measured against the canonical ``AKOSHA_TOOL_PROFILE=full``
# profile. The full profile tool count maps from REGISTRATION_TOOLS sums.
# Floor at 20 to permit additions without churn.
EXPECTED_MIN_TOOLS = 20


@pytest.mark.asyncio
async def test_mcp_tool_count_matches_documented() -> None:
    """Pin the canonical MCP tool count so README/CLAUDE.md claims stay in sync.

    The Akosha MCP server exposes tools via the W0 ``_apply_tool_profile``
    dispatch. The full profile adds:
        - register_health_tools_akosha (6)
        - register_akosha_tools (8)
        - register_session_buddy_tools (2)
        - register_pycharm_tools (6)
        - register_otel_query_tools (1)
        - register_fitness_tools (2)
        - register_eventbridge_tools (1)
    Sum is 26; floor at 20 to allow for additions/renames.
    """
    from akosha.mcp.tools.profiles import REGISTRATION_TOOLS

    total = sum(len(tools) for tools in REGISTRATION_TOOLS.values())
    assert total >= EXPECTED_MIN_TOOLS, (
        f"Expected >= {EXPECTED_MIN_TOOLS} tools across REGISTRATION_TOOLS, "
        f"got {total}. Update README.md / CLAUDE.md tool counts."
    )


# ---------------------------------------------------------------------------
# Env var wiring guard
# ---------------------------------------------------------------------------

# Documented env vars from README.md / CLAUDE.md / .env.example. Each entry
# is verified to be read via ``os.getenv`` (or ``os.environ.get``) somewhere
# in the akosha package source tree.
#
# Limitation: ``AKOSHA_TOOL_PROFILE`` is consumed indirectly via Pydantic
# Settings or string-literal forwarding to ``mcp-common``. It is not pinned
# here because the wiring is dispatched through a helper. Add new entries
# below whenever a new ``os.getenv``-backed env var is documented.
DOCUMENTED_ENV_VARS: tuple[str, ...] = (
    "AKOSHA_COLD_BACKEND",
    "AKOSHA_COLD_BUCKET",
    "AKOSHA_COLD_REGION",
    "AKOSHA_CACHE_BACKEND",
    "AKOSHA_REDIS_HOST",
    "AKOSHA_REDIS_PORT",
    "AKOSHA_EVENTBRIDGE_ENABLED",
    "AKOSHA_EVENTBRIDGE_DRY_RUN",
    "AKOSHA_EVENTBRIDGE_ENDPOINT",
    "AKOSHA_MODE",
    "AKOSHA_API_PORT",
    "AKOSHA_MCP_PORT",
    "AKOSHA_LOG_LEVEL",
    "AKOSHA_INGESTION_WORKERS",
    "AKOSHA_PROMETHEUS_PORT",
    "AKOSHA_ENVIRONMENT",
    "AKOSHA_API_TOKEN",
    "JWT_SECRET",
)


def _read_source_text() -> str:
    """Read every Python file under ``akosha/`` into a single string."""
    pkg_root = Path(__file__).resolve().parent.parent / "akosha"
    chunks: list[str] = []
    for py_file in pkg_root.rglob("*.py"):
        if "__pycache__" in py_file.parts:
            continue
        try:
            chunks.append(py_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
    return "\n".join(chunks)


def test_documented_env_vars_are_wired() -> None:
    """Every env var documented in README/CLAUDE.md must be read by package code."""
    src = _read_source_text()
    missing: list[str] = []
    for var in DOCUMENTED_ENV_VARS:
        pattern = re.compile(
            rf"os\.getenv\(\s*[\"']{re.escape(var)}[\"']|"
            rf"os\.environ\.get\(\s*[\"']{re.escape(var)}[\"']",
        )
        if not pattern.search(src):
            missing.append(var)
    assert not missing, (
        f"Documented env vars not read by package code: {missing}. "
        "Either remove them from docs or wire them via os.getenv."
    )


# ---------------------------------------------------------------------------
# Version stamp guard
# ---------------------------------------------------------------------------

# Heuristic: any User-Agent-looking string literal that contains a digit is
# considered a probable hardcoded version. Strings with an f-string prefix
# (``f"..."`` or ``f'...'``) or with literal ``{`` are accepted as dynamic.
_USER_AGENT_RE = re.compile(r"""User-Agent[\"'][^\"']{0,200}[\"']""")
_VERSION_LITERAL_RE = re.compile(r"\d+\.\d+")


def test_user_agent_matches_package_version() -> None:
    """Detect hardcoded User-Agent version strings that should interpolate from __version__."""
    pkg_root = Path(__file__).resolve().parent.parent / "akosha"
    hardcoded: list[tuple[str, str]] = []
    for py_file in pkg_root.rglob("*.py"):
        if "__pycache__" in py_file.parts:
            continue
        try:
            text = py_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for match in _USER_AGENT_RE.finditer(text):
            ua = match.group(0)
            # Skip dynamic strings (f-strings, .format, concatenation).
            if "{" in ua or "f\"" in ua or "f'" in ua or ".format(" in ua:
                continue
            if _VERSION_LITERAL_RE.search(ua):
                hardcoded.append((str(py_file), ua))
    assert not hardcoded, (
        f"Hardcoded User-Agent versions found (should interpolate from __version__): {hardcoded}"
    )
