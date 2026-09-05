"""Tests for the ``migrate`` Click group (``akosha.cli.commands.migrate``).

Audit found this module at 0% coverage. These tests exercise the
``data`` / ``status`` / ``version`` subcommands via Click's CliRunner
so the full CLI surface (option parsing + help text + behavior) is
covered end-to-end without spawning a subprocess.

Note: the module lives under ``akosha/cli/commands/`` but is not
importable via standard ``import`` because the parent ``akosha/cli``
exists as a module file (``akosha/cli.py``) rather than a package.
We load it by absolute path using ``importlib.util`` — the same
trick ``tests/unit/test_migrate_cli.py`` uses.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner


_MIGRATE_PATH = (
    Path(__file__).resolve().parents[4]
    / "akosha"
    / "cli"
    / "commands"
    / "migrate.py"
)
_spec = importlib.util.spec_from_file_location("akosha_cli_commands_migrate", _MIGRATE_PATH)
assert _spec is not None and _spec.loader is not None
_migrate_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_migrate_module)

migrate = _migrate_module.migrate
data = _migrate_module.data
status = _migrate_module.status
version = _migrate_module.version
_resolve_destination = _migrate_module._resolve_destination
_discover_subdirs = _migrate_module._discover_subdirs
_migrate_subdirs = _migrate_module._migrate_subdirs
_copy_dir_contents = _migrate_module._copy_dir_contents


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def populated_source(tmp_path: Path) -> Path:
    """Create a fake project-local data directory with two subdirs."""
    src = tmp_path / "data"
    sub_a = src / "conversations"
    sub_b = src / "summaries"
    sub_a.mkdir(parents=True)
    sub_b.mkdir(parents=True)
    (sub_a / "chat1.json").write_text('{"x": 1}')
    (sub_a / "chat2.json").write_text('{"x": 2}')
    (sub_b / "doc1.md").write_text("# title")
    return src


# ---------------------------------------------------------------------------
# migrate group + subcommand surface
# ---------------------------------------------------------------------------


def test_migrate_group_help_lists_subcommands(runner: CliRunner) -> None:
    """The group must advertise its three subcommands in --help output."""
    result = runner.invoke(migrate, ["--help"])
    assert result.exit_code == 0
    assert "data" in result.output
    assert "status" in result.output
    assert "version" in result.output


def test_migrate_data_help(runner: CliRunner) -> None:
    result = runner.invoke(migrate, ["data", "--help"])
    assert result.exit_code == 0
    assert "--dry-run" in result.output
    assert "--from-path" in result.output
    assert "--to-path" in result.output


# ---------------------------------------------------------------------------
# _resolve_destination
# ---------------------------------------------------------------------------


def test_resolve_destination_returns_passed_path(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit"
    assert _resolve_destination(explicit) == explicit


def test_resolve_destination_falls_back_to_default_resolver(tmp_path: Path) -> None:
    """When ``to_path`` is None, the default resolver's base_path wins."""
    fake_base = tmp_path / "default"
    fake_resolver = type("R", (), {"base_path": fake_base})()
    with patch.object(
        _migrate_module, "get_default_resolver", return_value=fake_resolver
    ):
        assert _resolve_destination(None) == fake_base


# ---------------------------------------------------------------------------
# _discover_subdirs
# ---------------------------------------------------------------------------


def test_discover_subdirs_returns_only_nonempty_directories(tmp_path: Path) -> None:
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "f.txt").write_text("data")
    (tmp_path / "empty").mkdir()
    (tmp_path / "lonely.txt").write_text("not a dir")
    found = _discover_subdirs(tmp_path)
    assert [d.name for d in found] == ["full"]


def test_discover_subdirs_on_missing_path_raises(tmp_path: Path) -> None:
    """Pin current behavior: missing source path raises ``FileNotFoundError``.

    The ``data`` subcommand guards against this by checking
    ``from_path.exists()`` before calling ``_discover_subdirs`` —
    the helper itself does not tolerate a missing directory. If
    that ever changes, the caller guard becomes redundant and this
    test should flip to ``== []``.
    """
    with pytest.raises(FileNotFoundError):
        _discover_subdirs(tmp_path / "does_not_exist")


# ---------------------------------------------------------------------------
# _copy_dir_contents
# ---------------------------------------------------------------------------


def test_copy_dir_contents_copies_files_and_trees(tmp_path: Path) -> None:
    """Helper counts top-level items only (files + dirs), not nested contents."""
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    (src / "a.txt").write_text("A")
    (src / "nested").mkdir()
    (src / "nested" / "b.txt").write_text("B")
    dest.mkdir()  # _copy_dir_contents does NOT create the destination

    migrated, skipped = _copy_dir_contents(src, dest)
    # a.txt (1 file) + nested/ (1 dir) = 2 top-level items.
    assert migrated == 2
    assert skipped == 0
    assert (dest / "a.txt").read_text() == "A"
    assert (dest / "nested" / "b.txt").read_text() == "B"


def test_copy_dir_contents_skips_existing_files(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    (src / "a.txt").write_text("new")
    dest.mkdir()
    (dest / "a.txt").write_text("existing")

    migrated, skipped = _copy_dir_contents(src, dest)
    assert migrated == 0
    assert skipped == 1
    assert (dest / "a.txt").read_text() == "existing"  # not overwritten


def test_copy_dir_contents_continues_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single shutil failure must not abort the whole copy."""
    import shutil as shutil_mod

    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    (src / "a.txt").write_text("A")
    (src / "b.txt").write_text("B")
    dest.mkdir()

    real_copy2 = shutil_mod.copy2
    calls = {"count": 0}

    def maybe_fail(src_path: Path, dst_path: Path, *args: object, **kw: object) -> object:
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("disk full")
        return real_copy2(src_path, dst_path, *args, **kw)

    monkeypatch.setattr(_migrate_module.shutil, "copy2", maybe_fail)

    migrated, skipped = _copy_dir_contents(src, dest)
    # First copy raises → counts as skipped; second copy succeeds → migrated.
    assert migrated == 1
    assert skipped == 1


# ---------------------------------------------------------------------------
# _migrate_subdirs
# ---------------------------------------------------------------------------


def test_migrate_subdirs_creates_destination_directories(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "alpha").mkdir()
    (src / "alpha" / "file.txt").write_text("data")
    dest = tmp_path / "dest"
    dest.mkdir()  # _migrate_subdirs requires dest to exist

    results = _migrate_subdirs([src / "alpha"], dest)
    assert (dest / "alpha" / "file.txt").exists()
    assert results["migrated"] == 1
    assert results["skipped"] == 0
    assert results["errors"] == 0


# ---------------------------------------------------------------------------
# data subcommand end-to-end
# ---------------------------------------------------------------------------


def test_data_dry_run_does_not_copy_files(
    runner: CliRunner, populated_source: Path, tmp_path: Path
) -> None:
    """--dry-run must report what would happen without touching the destination."""
    dest = tmp_path / "dest"
    result = runner.invoke(
        migrate,
        ["data", "--dry-run", "--from-path", str(populated_source), "--to-path", str(dest)],
    )
    assert result.exit_code == 0
    assert "[DRY RUN]" in result.output
    assert "Dry run complete" in result.output
    # Destination must NOT be created under dry-run.
    assert not dest.exists()


def test_data_missing_source_path_exits_nonzero(
    runner: CliRunner, tmp_path: Path
) -> None:
    """A non-existent ``--from-path`` exits non-zero.

    Click validates ``--from-path`` with ``exists=True`` and emits
    a BadParameter (exit code 2) before our handler runs. Either
    way: non-zero, which is what operators care about.
    """
    missing = tmp_path / "no_such_data"
    result = runner.invoke(
        migrate, ["data", "--from-path", str(missing), "--to-path", str(tmp_path / "dest")]
    )
    assert result.exit_code != 0
    assert "does not exist" in result.output.lower() or "not found" in result.output.lower()


def test_data_empty_source_returns_cleanly(
    runner: CliRunner, tmp_path: Path
) -> None:
    empty_src = tmp_path / "empty"
    empty_src.mkdir()
    result = runner.invoke(
        migrate, ["data", "--from-path", str(empty_src), "--to-path", str(tmp_path / "dest")]
    )
    assert result.exit_code == 0
    assert "No project-local data found" in result.output


def test_data_proceeds_when_user_confirms(
    runner: CliRunner, populated_source: Path, tmp_path: Path
) -> None:
    """With confirmation, data copies the source's subdirs to the destination."""
    dest = tmp_path / "dest"
    result = runner.invoke(
        migrate,
        ["data", "--from-path", str(populated_source), "--to-path", str(dest)],
        input="y\n",  # answer click.confirm() with yes
    )
    assert result.exit_code == 0
    assert (dest / "conversations" / "chat1.json").exists()
    assert (dest / "summaries" / "doc1.md").exists()
    assert "Migrated:" in result.output


def test_data_cancelled_when_user_declines(
    runner: CliRunner, populated_source: Path, tmp_path: Path
) -> None:
    """Declining the confirmation aborts before any copy."""
    dest = tmp_path / "dest"
    result = runner.invoke(
        migrate,
        ["data", "--from-path", str(populated_source), "--to-path", str(dest)],
        input="n\n",
    )
    assert result.exit_code == 0
    assert "Migration cancelled" in result.output
    assert not dest.exists()


# ---------------------------------------------------------------------------
# status subcommand
# ---------------------------------------------------------------------------


def test_status_prints_storage_paths(runner: CliRunner, tmp_path: Path) -> None:
    """``status`` reports env + base path + project-local data presence.

    We patch ``Path.cwd`` to a fresh tmp directory so the "no
    project-local data" branch doesn't accidentally fire on whatever
    data/ happens to exist in the real CWD (akelha's own project
    tree has a ``data/`` that would otherwise show "present").
    """
    fake_resolver = type(
        "R", (), {"env": "test", "base_path": tmp_path / "akosha-data"}
    )()
    with (
        patch.object(
            _migrate_module, "get_default_resolver", return_value=fake_resolver
        ),
        patch("pathlib.Path.cwd", return_value=tmp_path),
    ):
        result = runner.invoke(migrate, ["status"])
    assert result.exit_code == 0
    assert "Akosha Storage Paths:" in result.output
    assert "Environment: test" in result.output
    assert "Base path:" in result.output
    assert "Project-local data: none" in result.output


def test_status_detects_project_local_data(
    runner: CliRunner, populated_source: Path
) -> None:
    fake_resolver = type(
        "R", (), {"env": "test", "base_path": Path("/tmp/akosha-data")}
    )()
    with (
        patch.object(
            _migrate_module, "get_default_resolver", return_value=fake_resolver
        ),
        patch("pathlib.Path.cwd", return_value=populated_source.parent),
    ):
        result = runner.invoke(migrate, ["status"])
    assert "Project-local data: present" in result.output


# ---------------------------------------------------------------------------
# version subcommand
# ---------------------------------------------------------------------------


def test_version_prints_akosha_version(runner: CliRunner) -> None:
    result = runner.invoke(migrate, ["version"])
    assert result.exit_code == 0
    # Don't pin the literal version (changes per release); just check shape.
    assert "Akosha migration tools v" in result.output


# ---------------------------------------------------------------------------
# Direct subcommand function call (covers Click's decorator wrapping)
# ---------------------------------------------------------------------------


def test_data_subcommand_callable_directly(
    runner: CliRunner, populated_source: Path, tmp_path: Path
) -> None:
    """The ``data`` Click command can also be invoked as a plain function.

    Pinning this prevents accidental refactors that lose the
    ``@migrate.command()`` decorator and break CLI registration.
    """
    assert callable(data)
    dest = tmp_path / "dest"
    result = runner.invoke(
        data,
        ["--dry-run", "--from-path", str(populated_source), "--to-path", str(dest)],
    )
    assert result.exit_code == 0


def test_status_callable_directly(runner: CliRunner) -> None:
    assert callable(status)


def test_version_callable_directly(runner: CliRunner) -> None:
    assert callable(version)
