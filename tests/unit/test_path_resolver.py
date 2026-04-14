"""Tests for storage path resolution."""

from __future__ import annotations

from pathlib import Path

from akosha.storage.path_resolver import StoragePathResolver


def test_resolver_does_not_warn_on_legacy_path(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """Legacy paths should not spam startup logs.

    Migration remains available through the explicit CLI command.
    """
    legacy_path = tmp_path / "data"
    legacy_path.mkdir()
    (legacy_path / "warm").mkdir()

    monkeypatch.setenv("AKOSHA_ENV", "local")
    monkeypatch.chdir(tmp_path)

    caplog.clear()
    resolver = StoragePathResolver(project_dir=tmp_path)

    assert resolver.base_path.name == "akosha"
    assert resolver.base_path != legacy_path
    assert not [record for record in caplog.records if record.levelname == "WARNING"]
