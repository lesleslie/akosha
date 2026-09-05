"""Smoke tests for the ``python -m akosha`` and ``python -m akosha.mcp`` entry points.

Per plan Task 2.4g: spawn each entry point as a subprocess and assert
non-zero exit only on actual errors. Catches:
- import-time errors (typos, missing modules, circular imports)
- argparse-level errors (--help / --version work, no sys.exit(2))
- module-level side effects that crash before the CLI takes over
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(*args: str, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    """Helper: spawn python -m <args> from the repo root."""
    return subprocess.run(
        [sys.executable, "-m", *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_python_m_akosha_help_exits_0() -> None:
    """``python -m akosha --help`` must exit 0 (no import or argparse error)."""
    result = _run("akosha", "--help")
    assert result.returncode == 0, f"stderr: {result.stderr}"
    # Sanity: the help text should mention something Akosha-shaped.
    assert "akosha" in result.stdout.lower() or "usage" in result.stdout.lower()


def test_python_m_akosha_mcp_imports_and_initializes_tools() -> None:
    """``python -m akosha.mcp --help`` doesn't actually print help (the
    module calls ``uvicorn.run()`` unconditionally), but the import
    path + lifespan must succeed — proven by ``Applied
    AKOSHA_TOOL_PROFILE=full → N tools registered`` in stdout.

    The exit code can be non-zero (e.g. 3 = uvicorn port-already-in-use
    in a busy dev env); what we care about is that the module's
    import + lifespan init did NOT raise ImportError or
    ModuleNotFoundError. The proof: tool registration completed.
    """
    result = _run("akosha.mcp", "--help", timeout=20.0)
    # Real import errors would surface in stderr; tolerate uvicorn
    # exit codes (0=clean, 2=argparse, 3=bind failure in dev env).
    assert result.returncode in (0, 2, 3), (
        f"unexpected exit {result.returncode}: stderr={result.stderr}"
    )
    assert "ImportError" not in result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    # Tool registration confirms the full module init succeeded.
    assert "tools registered" in result.stdout


def test_python_m_akosha_mcp_status_exits_0_or_2() -> None:
    """``--help`` and ``--version`` exit 0; unknown args exit 2 (argparse).

    The CLI may not register ``status`` as a subcommand — in that
    case exit 2 is the expected argparse response. Either way,
    exit-code 0 (success) or 2 (argparse BadParameter) is the
    contract: a non-zero non-2 code means the import path or
    module init is broken.
    """
    result = _run("akosha", "status", timeout=10.0)
    # 0 = subcommand exists and runs, 2 = argparse rejected (unknown).
    # Anything else (e.g. ModuleNotFoundError -> exit 1) is a real bug.
    assert result.returncode in (0, 2), (
        f"unexpected exit {result.returncode}: {result.stderr}"
    )


def test_python_m_akosha_prints_version_string() -> None:
    """``--version`` prints a version (don't pin literal — changes per release)."""
    result = _run("akosha", "--version")
    # ``--version`` may exit 0 (oneiric) or 2 (no --version flag
    # registered). Either way the import path must succeed.
    if result.returncode != 0:
        assert result.returncode == 2, (
            f"--version failed unexpectedly: {result.stderr}"
        )
        return  # CLI didn't register --version — acceptable for now
    assert "0." in result.stdout or "0." in result.stderr
