"""Tests for Akosha migration CLI messaging."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from click.testing import CliRunner


runner = CliRunner()
MIGRATE_PATH = Path(__file__).resolve().parents[2] / "akosha" / "cli" / "commands" / "migrate.py"

spec = importlib.util.spec_from_file_location("akosha_cli_commands_migrate", MIGRATE_PATH)
assert spec is not None and spec.loader is not None
migrate_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migrate_module)
migrate = migrate_module.migrate


def test_migrate_help_uses_neutral_language() -> None:
    """Migration help should avoid legacy-specific messaging."""
    result = runner.invoke(migrate, ["data", "--help"])

    assert result.exit_code == 0
    assert "project-local" in result.stdout
    assert "application storage location" in result.stdout
    assert "legacy" not in result.stdout.lower()


def test_migrate_status_uses_neutral_language(tmp_path: Path, monkeypatch) -> None:
    """Migration status should describe the current layout without legacy phrasing."""
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(migrate, ["status"])

    assert result.exit_code == 0
    assert "project-local data" in result.stdout.lower() or "no project-local data" in result.stdout.lower()
    assert "legacy" not in result.stdout.lower()
