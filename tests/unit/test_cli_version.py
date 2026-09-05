"""Tests for the CLI ``version`` command (audit M2).

Audit found the version command swallowed every exception via
``except Exception: typer.echo("unknown")``. Operators running
``akosha version`` against a broken install got a silently wrong
"unknown" — no remediation hint, no non-zero exit, no traceback.

The fix narrows the except to ``PackageNotFoundError`` and surfaces
a remediation hint via ``typer.BadParameter`` (which Typer turns
into exit code 2 with the message on stderr).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from akosha.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_version_prints_installed_version(runner: CliRunner) -> None:
    """Happy path: version string ends up on stdout."""
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    # The version string follows "Akosha version: X.Y.Z"; pin the prefix.
    assert "Akosha version:" in result.stdout
    assert "unknown" not in result.stdout.lower()


def test_version_surfaces_install_error_on_package_not_found(
    runner: CliRunner,
) -> None:
    """When akosha isn't installed (PackageNotFoundError), exit non-zero
    with a remediation hint — not a silent 'unknown'."""
    import importlib.metadata

    def fake_version(_pkg: str) -> str:
        raise importlib.metadata.PackageNotFoundError("not installed")

    with patch("importlib.metadata.version", fake_version):
        result = runner.invoke(app, ["version"])

    assert result.exit_code != 0, (
        f"version should exit non-zero on PackageNotFoundError; "
        f"got {result.exit_code} with stdout={result.stdout!r}"
    )
    combined = (result.stdout or "") + (result.stderr or "")
    assert "not installed" in combined.lower() or "reinstall" in combined.lower()


def test_version_does_not_swallow_unexpected_exceptions(
    runner: CliRunner,
) -> None:
    """A non-PackageNotFoundError exception must NOT be hidden behind 'unknown'.

    The bug was that ``except Exception:`` swallowed every kind of
    error. After the fix, only PackageNotFoundError is caught; any
    other exception (e.g. a broken metadata DB) surfaces with a
    non-zero exit so operators can diagnose. (Typer's CliRunner
    captures the traceback in ``result.exception`` rather than
    ``stderr``, so we assert on the exception attribute.)
    """
    def explode(_pkg: str) -> str:
        raise RuntimeError("metadata backend crashed")

    with patch("importlib.metadata.version", explode):
        result = runner.invoke(app, ["version"])

    assert result.exit_code != 0, (
        f"unexpected exception must NOT be silently swallowed; "
        f"got exit {result.exit_code} stdout={result.stdout!r}"
    )
    # The exception (or its message) must reach the operator via
    # either stderr output or the captured exception attribute.
    combined = (result.stdout or "") + (result.stderr or "")
    assert (
        "metadata backend crashed" in combined
        or (result.exception is not None and "metadata backend crashed" in str(result.exception))
    )
