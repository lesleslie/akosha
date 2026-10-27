"""Tests for the lazy application imports in :mod:`akosha.cli`.

``akosha/cli.py`` imported ``akosha.main``, ``akosha.shell`` and
``akosha.mcp`` at module scope. Between them they pull in the storage stack
(numpy, the oneiric adapter registry) and the config module -- so ``akosha
--help`` paid for the whole application before Typer could look at ``argv``.

The names stay reachable as module attributes (the suite patches
``akosha.cli.AkoshaApplication``), but nothing is imported until something
asks for it.

These tests pin the IMPORT GRAPH rather than a wall-clock budget. A timing
assertion would be flaky for exactly the reason this change exists -- the
cost swings from ~4s warm to ~10-32s cold -- so it would reintroduce the
coin-flip this work removes. If someone re-adds a module-scope import, the
structural assertions below fail immediately and deterministically.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_python(code: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter rooted at the repo."""
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


class TestImportGraph:
    """Importing the CLI must not pull in the application."""

    def test_import_does_not_pull_in_akosha_config(self) -> None:
        """The CLI needs ``DEFAULT_MCP_PORT`` for its Typer defaults.

        That constant must come from a module with no heavy imports, so
        importing the CLI does not drag in the config (and through it the
        oneiric settings chain and the storage stack).
        """
        result = _run_python(
            "import akosha.cli, sys; "
            "assert 'akosha.config' not in sys.modules, sorted(sys.modules); "
            "print('OK')"
        )
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

    def test_import_does_not_pull_in_the_application(self) -> None:
        """``akosha.main`` / ``akosha.shell`` / ``akosha.mcp`` stay unloaded.

        The command bodies already import them locally where they are
        actually used; the module-scope copies were redundant.
        """
        result = _run_python(
            "import akosha.cli, sys; "
            "leaked = [m for m in ('akosha.main', 'akosha.shell', 'akosha.mcp') "
            "if m in sys.modules]; "
            "assert not leaked, leaked; "
            "print('OK')"
        )
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout


class TestPatchableSymbols:
    """The lazily-resolved names stay reachable as module attributes."""

    def test_the_application_symbols_resolve(self) -> None:
        """``mock.patch("akosha.cli.AkoshaApplication")`` must keep working.

        The patcher reads the attribute off the module; the PEP 562 hook
        serves it on first access.
        """
        result = _run_python(
            "import akosha.cli as c; "
            "assert c.AkoshaApplication.__name__ == 'AkoshaApplication'; "
            "assert c.AkoshaShell.__name__ == 'AkoshaShell'; "
            "assert callable(c.create_app); "
            "print('OK')"
        )
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

    def test_the_cli_still_builds_its_app(self) -> None:
        """The Typer app must exist and expose the documented commands.

        The names are read off the Click group: ``CommandInfo.name`` is
        ``None`` for commands registered without an explicit name, so
        ``registered_commands`` is not a reliable source of truth.
        """
        import typer
        from typer.main import get_command

        from akosha.cli import app

        assert isinstance(app, typer.Typer)
        assert set(get_command(app).commands) >= {
            "shell",
            "start",
            "info",
            "modes",
            "doctor",
            "health",
            "version",
        }


class TestPortConstant:
    """``DEFAULT_MCP_PORT`` keeps one source of truth across both homes."""

    def test_both_import_paths_agree(self) -> None:
        """``akosha.constants`` and ``akosha.config`` must not drift."""
        from akosha.config import DEFAULT_MCP_PORT as from_config
        from akosha.constants import DEFAULT_MCP_PORT as from_constants

        assert from_config == from_constants == 8682
