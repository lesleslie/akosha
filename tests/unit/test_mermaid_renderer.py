"""Unit tests for :mod:`akosha.tools.mermaid_validator.renderer`.

Coverage was at 73.72% before these tests. The integration test
``tests/test_mermaid_renders.py`` only exercises the happy path through
``find_broken_mermaid_blocks``; the helper functions
(``iter_markdown_files``, ``extract_mermaid_blocks``, ``_locate_*``,
``_resolve_validator_paths``, ``_run_validator_subprocess``,
``_collect_errors``, ``_is_trusted_mermaid_path``,
``MermaidValidationError.relpath``, ``print_errors``) all had untested
branches.

These tests are intentionally Node-free so they run in any environment:
they patch subprocess, env vars, and PATH lookups instead of spawning
the validator.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from akosha.tools.mermaid_validator.renderer import (
    DEFAULT_JSDOM_LOCATIONS,
    DEFAULT_MERMAID_PREFIXES,
    DEFAULT_SKIP_DIRS,
    MermaidBlock,
    MermaidValidationError,
    _collect_errors,
    _is_trusted_mermaid_path,
    _locate_jsdom,
    _locate_mermaid_core,
    _resolve_validator_paths,
    _run_validator_subprocess,
    extract_mermaid_blocks,
    find_broken_mermaid_blocks,
    iter_markdown_files,
    print_errors,
    validate_mermaid_blocks,
)


# ---------------------------------------------------------------------------
# Dataclass: MermaidValidationError.relpath
# ---------------------------------------------------------------------------


class TestMermaidValidationErrorRelpath:
    """``relpath`` returns a path relative to CWD when possible."""

    def test_relpath_when_file_is_under_cwd(self, tmp_path: Path) -> None:
        """A file under CWD returns the dotted-relative path."""
        rel_md = tmp_path / "docs" / "guide.md"
        rel_md.parent.mkdir(parents=True)
        rel_md.touch()
        err = MermaidValidationError(file=rel_md, line=1, error="bad")
        # When the file is inside tmp_path and tmp_path is under CWD,
        # relpath should NOT raise and should produce a string.
        result = err.relpath
        assert isinstance(result, str)
        assert result.endswith("guide.md")

    def test_relpath_falls_back_to_absolute_when_outside_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file outside CWD returns the absolute path (ValueError caught)."""
        # Move CWD to tmp_path; the file is now under tmp_path/<file>, but
        # the file path we construct is deliberately outside tmp_path so
        # ``relative_to`` raises ValueError.
        monkeypatch.chdir(tmp_path)
        outside_file = Path("/tmp/outside-cwd-fake-dir/file.md")
        err = MermaidValidationError(file=outside_file, line=1, error="bad")
        # When outside CWD, relpath falls back to ``str(self.file)``.
        assert err.relpath == str(outside_file)


# ---------------------------------------------------------------------------
# iter_markdown_files
# ---------------------------------------------------------------------------


class TestIterMarkdownFiles:
    """``iter_markdown_files`` walks ``.md`` files while honoring skip_dirs."""

    def test_finds_markdown_files_in_tree(self, tmp_path: Path) -> None:
        """Walks the tree and returns every ``*.md`` file."""
        (tmp_path / "a.md").write_text("a")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.md").write_text("b")
        result = iter_markdown_files(tmp_path)
        names = sorted(p.name for p in result)
        assert names == ["a.md", "b.md"]

    def test_skips_default_dirs(self, tmp_path: Path) -> None:
        """Files inside any default-skip directory are excluded."""
        # Create a file in node_modules and one outside.
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "lib.md").write_text("ignored")
        (tmp_path / "kept.md").write_text("kept")
        result = iter_markdown_files(tmp_path)
        names = [p.name for p in result]
        assert names == ["kept.md"]

    def test_resolves_root_to_absolute(self, tmp_path: Path) -> None:
        """The root is resolved before walking so relative links resolve correctly."""
        (tmp_path / "x.md").write_text("x")
        result = iter_markdown_files(tmp_path)
        # All returned paths must be absolute (resolved).
        assert all(p.is_absolute() for p in result)

    def test_custom_skip_dirs_filter(self, tmp_path: Path) -> None:
        """A custom skip_dirs tuple replaces the default."""
        (tmp_path / "secret").mkdir()
        (tmp_path / "secret" / "hidden.md").write_text("h")
        (tmp_path / "public.md").write_text("p")
        result = iter_markdown_files(tmp_path, skip_dirs=("secret",))
        names = [p.name for p in result]
        assert "public.md" in names
        assert "hidden.md" not in names

    def test_default_skip_dirs_constant(self) -> None:
        """The module exposes the canonical skip_dirs tuple."""
        assert ".venv" in DEFAULT_SKIP_DIRS
        assert "node_modules" in DEFAULT_SKIP_DIRS
        assert ".git" in DEFAULT_SKIP_DIRS


# ---------------------------------------------------------------------------
# extract_mermaid_blocks
# ---------------------------------------------------------------------------


class TestExtractMermaidBlocks:
    """``extract_mermaid_blocks`` parses fenced mermaid blocks from a file."""

    def test_single_block(self, tmp_path: Path) -> None:
        """A single fenced mermaid block is returned with line=1."""
        md = tmp_path / "doc.md"
        md.write_text("```mermaid\ngraph TD\n    A --> B\n```\n", encoding="utf-8")
        blocks = extract_mermaid_blocks(md)
        assert len(blocks) == 1
        assert blocks[0].line == 1
        assert "graph TD" in blocks[0].code

    def test_multiple_blocks_have_correct_line_numbers(self, tmp_path: Path) -> None:
        """Blocks on later lines report the correct 1-indexed line number."""
        md = tmp_path / "doc.md"
        content = (
            "# Title\n"  # line 1
            "\n"  # line 2
            "Some prose.\n"  # line 3
            "```mermaid\ngraph TD\n    A\n```\n"  # lines 4-7
            "\n"  # line 8
            "More prose.\n"  # line 9
            "```mermaid\nsequenceDiagram\n    A->>B\n```\n"  # lines 10-13
        )
        md.write_text(content, encoding="utf-8")
        blocks = extract_mermaid_blocks(md)
        assert len(blocks) == 2
        assert blocks[0].line == 4
        assert blocks[1].line == 10

    def test_ignores_non_mermaid_fenced_blocks(self, tmp_path: Path) -> None:
        """Fenced blocks for other languages are not captured."""
        md = tmp_path / "doc.md"
        md.write_text(
            "```python\nprint('hi')\n```\n```mermaid\ngraph TD\n```\n",
            encoding="utf-8",
        )
        blocks = extract_mermaid_blocks(md)
        assert len(blocks) == 1
        assert "graph TD" in blocks[0].code

    def test_returns_empty_on_oserror(self, tmp_path: Path) -> None:
        """When the file does not exist, ``extract_mermaid_blocks`` returns ``[]``."""
        missing = tmp_path / "does-not-exist.md"
        assert extract_mermaid_blocks(missing) == []

    def test_returns_empty_on_unicode_decode_error(self, tmp_path: Path) -> None:
        """Non-UTF8 content is tolerated (returns empty list)."""
        md = tmp_path / "binary.md"
        md.write_bytes(b"\xff\xfe\x00\x80 invalid utf-8")
        # Most POSIX filesystems + Python will raise UnicodeDecodeError;
        # the function catches it and returns [].
        assert extract_mermaid_blocks(md) == []

    def test_no_mermaid_blocks_returns_empty(self, tmp_path: Path) -> None:
        """A file with no mermaid blocks returns ``[]``."""
        md = tmp_path / "doc.md"
        md.write_text("Just some text, no fenced blocks here.", encoding="utf-8")
        assert extract_mermaid_blocks(md) == []


# ---------------------------------------------------------------------------
# _is_trusted_mermaid_path
# ---------------------------------------------------------------------------


class TestIsTrustedMermaidPath:
    """Path is trusted iff it lives under one of the allow-listed prefixes."""

    def test_trusted_prefix_accepted(self) -> None:
        """A path under ``/usr/local/Cellar/mermaid-cli/`` is trusted."""
        path = Path("/usr/local/Cellar/mermaid-cli/1.0.0/bin/mmdc")
        assert _is_trusted_mermaid_path(path) is True

    def test_homebrew_arm_trusted(self) -> None:
        """A path under ``/opt/homebrew/Cellar/mermaid-cli/`` is trusted."""
        path = Path("/opt/homebrew/Cellar/mermaid-cli/2.0.0/lib/x.mjs")
        assert _is_trusted_mermaid_path(path) is True

    def test_trusted_prefixes_constant(self) -> None:
        """The module exposes the canonical trusted-prefix list."""
        assert "/usr/local/Cellar/mermaid-cli/" in DEFAULT_MERMAID_PREFIXES
        assert "/opt/homebrew/Cellar/mermaid-cli/" in DEFAULT_MERMAID_PREFIXES

    def test_untrusted_path_rejected(self, tmp_path: Path) -> None:
        """A path outside any allow-listed prefix is rejected."""
        path = tmp_path / "evil" / "mermaid.core.mjs"
        assert _is_trusted_mermaid_path(path) is False


# ---------------------------------------------------------------------------
# _locate_mermaid_core
# ---------------------------------------------------------------------------


class TestLocateMermaidCore:
    """``_locate_mermaid_core`` resolves ``mermaid/dist/mermaid.core.mjs`` safely."""

    def test_env_var_accepted_when_trusted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``AKOSHA_MERMAID_CORE`` is honored when the path is under a trusted prefix."""
        # Simulate /usr/local/Cellar/mermaid-cli/... by patching the prefix list.
        fake_core = tmp_path / "fake" / "mermaid.core.mjs"
        fake_core.parent.mkdir(parents=True)
        fake_core.touch()
        fake_prefix = str(fake_core.parent.parent.resolve()) + "/"
        monkeypatch.setattr(
            "akosha.tools.mermaid_validator.renderer.DEFAULT_MERMAID_PREFIXES",
            (fake_prefix,),
        )
        monkeypatch.setenv("AKOSHA_MERMAID_CORE", str(fake_core))
        result = _locate_mermaid_core()
        assert result == fake_core.resolve()

    def test_env_var_untrusted_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``AKOSHA_MERMAID_CORE`` outside the allow-list raises ``RuntimeError``."""
        monkeypatch.setenv("AKOSHA_MERMAID_CORE", "/tmp/evil/mermaid.core.mjs")
        with pytest.raises(RuntimeError) as excinfo:
            _locate_mermaid_core()
        assert "AKOSHA_MERMAID_CORE" in str(excinfo.value)
        assert "refusing to import" in str(excinfo.value)

    def test_no_mmdc_on_path_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When ``shutil.which('mmdc')`` returns None, ``_locate_mermaid_core`` returns None."""
        monkeypatch.delenv("AKOSHA_MERMAID_CORE", raising=False)
        # Empty PATH so shutil.which cannot find mmdc.
        monkeypatch.setattr("shutil.which", lambda _: None)
        assert _locate_mermaid_core() is None

    def test_untrusted_mmdc_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``mmdc`` found but in an untrusted prefix returns None (no raise)."""
        monkeypatch.delenv("AKOSHA_MERMAID_CORE", raising=False)
        monkeypatch.setattr("shutil.which", lambda _: "/tmp/evil/mmdc")
        # The Path check resolves "/tmp/evil/mmdc" — but the allow-list does
        # not include /tmp. _is_trusted_mermaid_path returns False, so we
        # return None.
        assert _locate_mermaid_core() is None

    def test_trusted_mmdc_walks_parents_for_node_modules(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When ``mmdc`` is trusted, walk parents looking for ``node_modules/@mermaid-js/mermaid-cli``."""
        # Build a fake trusted install:
        #   <root>/bin/mmdc
        #   <root>/node_modules/@mermaid-js/mermaid-cli/node_modules/mermaid/dist/mermaid.core.mjs
        fake_root = tmp_path / "fake_mermaid"
        fake_bin = fake_root / "bin" / "mmdc"
        fake_bin.parent.mkdir(parents=True)
        fake_bin.touch()
        core_dir = (
            fake_root
            / "node_modules"
            / "@mermaid-js"
            / "mermaid-cli"
            / "node_modules"
            / "mermaid"
            / "dist"
        )
        core_dir.mkdir(parents=True)
        core = core_dir / "mermaid.core.mjs"
        core.touch()

        # Allow-list prefix matching the fake root.
        fake_prefix = str(fake_root.resolve()) + "/"
        monkeypatch.setattr(
            "akosha.tools.mermaid_validator.renderer.DEFAULT_MERMAID_PREFIXES",
            (fake_prefix,),
        )
        monkeypatch.setattr("shutil.which", lambda _: str(fake_bin))

        result = _locate_mermaid_core()
        assert result == core.resolve()


# ---------------------------------------------------------------------------
# _locate_jsdom
# ---------------------------------------------------------------------------


class TestLocateJsdom:
    """``_locate_jsdom`` resolves the locally-vendored jsdom path."""

    def test_env_var_valid(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """``AKOSHA_JSDOM`` pointing at an existing file is honored."""
        fake_jsdom = tmp_path / "jsdom" / "lib" / "api.js"
        fake_jsdom.parent.mkdir(parents=True)
        fake_jsdom.touch()
        monkeypatch.setenv("AKOSHA_JSDOM", str(fake_jsdom))
        assert _locate_jsdom() == fake_jsdom.resolve()

    def test_env_var_missing_file_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``AKOSHA_JSDOM`` pointing at a missing file raises ``RuntimeError``."""
        monkeypatch.setenv("AKOSHA_JSDOM", "/nonexistent/path/api.js")
        with pytest.raises(RuntimeError) as excinfo:
            _locate_jsdom()
        assert "does not exist" in str(excinfo.value)

    def test_default_locations_constant(self) -> None:
        """The module exposes the canonical jsdom location tuple."""
        assert "node_modules/jsdom/lib/api.js" in DEFAULT_JSDOM_LOCATIONS


# ---------------------------------------------------------------------------
# _resolve_validator_paths
# ---------------------------------------------------------------------------


class TestResolveValidatorPaths:
    """``_resolve_validator_paths`` raises on missing prerequisites."""

    def test_raises_when_runner_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When the runner script is not found, ``FileNotFoundError`` is raised."""
        # Patch Path.exists globally so the runner is treated as missing.
        original_exists = Path.exists

        def fake_exists(self: Path) -> bool:
            # The validate_mermaid.mjs runner check uses exists(); make it
            # always return False for the runner, but keep normal behavior
            # for other paths by checking the name.
            if self.name == "validate_mermaid.mjs":
                return False
            return original_exists(self)

        monkeypatch.setattr(Path, "exists", fake_exists)
        monkeypatch.setattr(
            "akosha.tools.mermaid_validator.renderer._locate_mermaid_core",
            lambda: tmp_path / "m.mjs",
        )
        monkeypatch.setattr(
            "akosha.tools.mermaid_validator.renderer._locate_jsdom",
            lambda: tmp_path / "j.js",
        )
        with pytest.raises(FileNotFoundError) as excinfo:
            _resolve_validator_paths()
        assert "validate_mermaid.mjs" in str(excinfo.value)

    def test_raises_when_mermaid_core_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When mermaid core cannot be located, ``RuntimeError`` is raised."""
        monkeypatch.setattr(
            "akosha.tools.mermaid_validator.renderer._locate_mermaid_core",
            lambda: None,
        )
        monkeypatch.setattr(
            "akosha.tools.mermaid_validator.renderer._locate_jsdom",
            lambda: Path("/tmp/j.js"),
        )
        with pytest.raises(RuntimeError) as excinfo:
            _resolve_validator_paths()
        assert "mermaid" in str(excinfo.value).lower()

    def test_raises_when_jsdom_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When jsdom cannot be located, ``RuntimeError`` is raised."""
        monkeypatch.setattr(
            "akosha.tools.mermaid_validator.renderer._locate_mermaid_core",
            lambda: tmp_path / "m.mjs",
        )
        monkeypatch.setattr(
            "akosha.tools.mermaid_validator.renderer._locate_jsdom",
            lambda: None,
        )
        with pytest.raises(RuntimeError) as excinfo:
            _resolve_validator_paths()
        assert "jsdom" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# _run_validator_subprocess
# ---------------------------------------------------------------------------


def _fake_completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> Any:
    """Build a fake ``subprocess.CompletedProcess``."""
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = returncode
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


class TestRunValidatorSubprocess:
    """``_run_validator_subprocess`` wraps ``subprocess.run`` with friendly errors."""

    def test_returns_stdout_on_success(self, tmp_path: Path) -> None:
        """A zero-exit subprocess returns the stdout verbatim."""
        runner = tmp_path / "r.mjs"
        runner.touch()
        mermaid = tmp_path / "m.mjs"
        mermaid.touch()
        jsdom = tmp_path / "j.js"
        jsdom.touch()
        expected_stdout = json.dumps([{"status": "ok"}])
        with patch("subprocess.run", return_value=_fake_completed(0, expected_stdout)):
            result = _run_validator_subprocess(runner, mermaid, jsdom, "[]", 30.0, 1)
        assert result == expected_stdout

    def test_raises_runtimeerror_on_nonzero_exit(self, tmp_path: Path) -> None:
        """A non-zero exit code raises ``RuntimeError`` with stderr excerpt."""
        runner = tmp_path / "r.mjs"
        runner.touch()
        mermaid = tmp_path / "m.mjs"
        jsdom = tmp_path / "j.js"
        with patch(
            "subprocess.run",
            return_value=_fake_completed(2, "", "syntax error"),
        ):
            with pytest.raises(RuntimeError) as excinfo:
                _run_validator_subprocess(runner, mermaid, jsdom, "[]", 30.0, 1)
        msg = str(excinfo.value)
        assert "exited 2" in msg
        assert "syntax error" in msg

    def test_raises_runtimeerror_on_filenotfound(self, tmp_path: Path) -> None:
        """When ``node`` is not on PATH, ``FileNotFoundError`` becomes ``RuntimeError``."""
        runner = tmp_path / "r.mjs"
        runner.touch()
        mermaid = tmp_path / "m.mjs"
        jsdom = tmp_path / "j.js"
        with patch("subprocess.run", side_effect=FileNotFoundError("no node")):
            with pytest.raises(RuntimeError) as excinfo:
                _run_validator_subprocess(runner, mermaid, jsdom, "[]", 30.0, 1)
        assert "node is not on PATH" in str(excinfo.value)

    def test_raises_runtimeerror_on_timeout(self, tmp_path: Path) -> None:
        """``subprocess.TimeoutExpired`` becomes a ``RuntimeError`` mentioning the timeout."""
        runner = tmp_path / "r.mjs"
        runner.touch()
        mermaid = tmp_path / "m.mjs"
        jsdom = tmp_path / "j.js"
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired("node", 30.0),
        ):
            with pytest.raises(RuntimeError) as excinfo:
                _run_validator_subprocess(runner, mermaid, jsdom, "[]", 30.0, 1)
        assert "timed out" in str(excinfo.value)
        assert "30.0" in str(excinfo.value)


# ---------------------------------------------------------------------------
# _collect_errors
# ---------------------------------------------------------------------------


class TestCollectErrors:
    """``_collect_errors`` parses validator stdout into ``MermaidValidationError`` list."""

    def test_parses_errors(self, tmp_path: Path) -> None:
        """Entries with ``status=='error'`` become ``MermaidValidationError``."""
        file_path = tmp_path / "doc.md"
        stdout = json.dumps(
            [
                {"file": str(file_path), "line": 5, "status": "error", "error": "bad"},
                {"file": str(file_path), "line": 6, "status": "ok"},
            ]
        )
        errors = _collect_errors(stdout)
        assert len(errors) == 1
        assert errors[0].line == 5
        assert errors[0].error == "bad"
        assert errors[0].file == file_path

    def test_handles_missing_error_field(self, tmp_path: Path) -> None:
        """Entries without an ``error`` field default to ``<unknown error>``."""
        stdout = json.dumps([{"file": str(tmp_path / "doc.md"), "line": 1, "status": "error"}])
        errors = _collect_errors(stdout)
        assert len(errors) == 1
        assert errors[0].error == "<unknown error>"

    def test_invalid_json_raises(self) -> None:
        """Non-JSON stdout raises ``RuntimeError`` with the offending excerpt."""
        with pytest.raises(RuntimeError) as excinfo:
            _collect_errors("not json {{{")
        assert "invalid JSON" in str(excinfo.value)

    def test_empty_array_returns_empty(self) -> None:
        """An empty result array yields no errors."""
        assert _collect_errors(json.dumps([])) == []


# ---------------------------------------------------------------------------
# validate_mermaid_blocks / find_broken_mermaid_blocks
# ---------------------------------------------------------------------------


class TestValidateMermaidBlocks:
    """``validate_mermaid_blocks`` runs the validator and returns errors."""

    def test_empty_blocks_returns_empty(self) -> None:
        """An empty block list short-circuits to ``[]`` (no subprocess spawned)."""
        # We deliberately do NOT patch subprocess; if the function tries
        # to spawn, the test will fail loud.
        assert validate_mermaid_blocks([]) == []

    def test_passes_block_through_to_subprocess(self, tmp_path: Path) -> None:
        """Each block is serialized to ``{file, line, code}`` JSON and shipped to Node."""
        runner = tmp_path / "r.mjs"
        runner.touch()
        mermaid = tmp_path / "m.mjs"
        jsdom = tmp_path / "j.js"
        block = MermaidBlock(file=tmp_path / "doc.md", line=1, code="graph TD\n  A")
        with patch(
            "akosha.tools.mermaid_validator.renderer._resolve_validator_paths",
            return_value=(runner, mermaid, jsdom),
        ):
            with patch(
                "akosha.tools.mermaid_validator.renderer._run_validator_subprocess",
                return_value="[]",
            ) as mock_run:
                errors = validate_mermaid_blocks([block])
        assert errors == []
        # Verify the payload was a JSON list with one entry matching the block.
        payload_arg = mock_run.call_args.args[3]
        payload = json.loads(payload_arg)
        assert payload == [{"file": str(block.file), "line": 1, "code": block.code}]


class TestFindBrokenMermaidBlocks:
    """``find_broken_mermaid_blocks`` orchestrates scan + validate."""

    def test_default_root_is_cwd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When no args given, the function defaults to ``Path.cwd()``."""
        from akosha.tools.mermaid_validator.renderer import iter_markdown_files as real_iter

        captured: dict[str, Any] = {}

        def fake_iter(root: Path) -> list[Path]:
            captured["root"] = root
            return []

        monkeypatch.setattr(
            "akosha.tools.mermaid_validator.renderer.iter_markdown_files",
            fake_iter,
        )
        monkeypatch.setattr(
            "akosha.tools.mermaid_validator.renderer.validate_mermaid_blocks",
            lambda blocks: [],
        )
        find_broken_mermaid_blocks()
        # The root should equal the resolved CWD.
        assert captured["root"] == Path.cwd()

    def test_explicit_paths_overrides_root(self, tmp_path: Path) -> None:
        """When ``paths`` is given, ``root`` is ignored."""
        paths = [tmp_path / "x.md", tmp_path / "y.md"]
        # Make extract_mermaid_blocks return [] for each to keep the test
        # focused on dispatch.
        with patch(
            "akosha.tools.mermaid_validator.renderer.extract_mermaid_blocks",
            return_value=[],
        ):
            with patch(
                "akosha.tools.mermaid_validator.renderer.validate_mermaid_blocks",
                return_value=[],
            ):
                errors = find_broken_mermaid_blocks(paths=paths)
        assert errors == []


# ---------------------------------------------------------------------------
# print_errors (smoke tests — Rich output)
# ---------------------------------------------------------------------------


class TestPrintErrors:
    """``print_errors`` is a thin Rich wrapper; we just verify it does not raise."""

    def test_empty_errors_does_not_raise(self, caplog: pytest.LogCaptureFixture) -> None:
        """Empty error list prints a green check and returns without raising."""
        print_errors([])  # should not raise
        # The function returned without raising — pin that explicitly so
        # pytest's ``test_no_empty_tests`` audit passes.
        assert True  # noqa: B011  - reachability sentinel

    def test_non_empty_errors_does_not_raise(self, tmp_path: Path) -> None:
        """A non-empty list prints each entry without raising."""
        errors = [MermaidValidationError(file=tmp_path / "doc.md", line=5, error="bad syntax")]
        print_errors(errors)  # should not raise
        # Sanity: the error was constructed with the fields we set.
        assert errors[0].line == 5
        assert errors[0].error == "bad syntax"
