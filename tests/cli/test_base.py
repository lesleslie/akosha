"""Tests for the Akosha BodaiCLIBase adoption.

Verifies the Phase 3 Task 4.4 wiring:

- The ``AkoshaCLI`` subclass instantiates with ``component_name="akosha"``
  and inherits from ``BodaiCLIBase``.
- ``_doctor_checks()`` returns a non-empty dict with REAL check entries
  (not a stub returning ``{}``).
- ``_health_probe()`` returns a real snapshot dict (not an
  ``UNAVAILABLE`` stub).
- ``BodaiCLIBase.run()`` (via ``app.run()`` / Typer wiring) exposes the
  expected global subcommands: ``version``, ``doctor``, ``health``.

These tests are the contract the gates assert before merge; stubs would
silently regress into the old single-typer pattern, which is exactly
what Phase 3 Task 4.4 is replacing.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from akosha.cli import AkoshaCLI, app


def _instantiate_cli() -> AkoshaCLI:
    """Build a fresh CLI instance — BodaiCLIBase registers globals in __init__."""
    return AkoshaCLI()


def _extract_json(stdout: str) -> dict[str, Any]:
    """Pull the first JSON object out of the runner stdout.

    CliRunner captures log messages to stdout before the actual command
    output, so the JSON payload may follow a few log lines. Find the
    first ``{`` and decode from there.
    """
    start = stdout.find("{")
    if start == -1:
        raise AssertionError(f"No JSON object in stdout: {stdout!r}")
    payload: dict[str, Any] = json.loads(stdout[start:])
    return payload


# ----------------------------------------------------------------------
# Component identity
# ----------------------------------------------------------------------
def test_akosha_cli_inherits_from_bodai_cli_base() -> None:
    """AkoshaCLI must subclass BodaiCLIBase to inherit the global commands."""
    from oneiric.cli.base import BodaiCLIBase

    cli = _instantiate_cli()
    assert isinstance(cli, BodaiCLIBase)
    # BodaiCLIBase extends typer.Typer; the CLI is therefore a real Typer app.
    import typer

    assert isinstance(cli, typer.Typer)


def test_akosha_cli_component_name_is_akosha() -> None:
    """``component_name`` drives version detection + global flags."""
    cli = _instantiate_cli()
    assert cli.component_name == "akosha"


def test_akosha_cli_component_version_is_string() -> None:
    """Version is resolved from importlib.metadata at construction time."""
    cli = _instantiate_cli()
    assert isinstance(cli.component_version, str)
    assert cli.component_version  # non-empty


# ----------------------------------------------------------------------
# Module-level `app` singleton
# ----------------------------------------------------------------------
def test_module_level_app_is_akosha_cli_instance() -> None:
    """The package-level ``app`` must be an AkoshaCLI so all subcommands see it."""
    assert isinstance(app, AkoshaCLI)


# ----------------------------------------------------------------------
# _doctor_checks — REAL checks (not a stub returning [])
# ----------------------------------------------------------------------
def test_doctor_checks_returns_non_empty_dict() -> None:
    cli = _instantiate_cli()
    checks = cli._doctor_checks()
    assert isinstance(checks, dict)
    assert checks, "_doctor_checks() must return real check entries, not {}"


def test_doctor_checks_includes_expected_check_names() -> None:
    """Each check probes a real Akosha surface (storage, deps, modes, config)."""
    cli = _instantiate_cli()
    checks = cli._doctor_checks()
    expected_keys = {
        "package_version",
        "oneiric_dep",
        "storage_paths",
        "mode_registry",
        "config_load",
    }
    assert expected_keys.issubset(
        checks.keys()
    ), f"Missing checks: {expected_keys - set(checks.keys())}"


def test_doctor_checks_entries_have_status_and_detail() -> None:
    """Every check entry must carry a status (ok/warn/fail) and a detail string."""
    cli = _instantiate_cli()
    checks = cli._doctor_checks()
    valid_statuses = {"ok", "warn", "fail", "unknown"}
    for name, info in checks.items():
        assert isinstance(info, dict), f"{name} must be a dict"
        assert "status" in info, f"{name} missing 'status' key"
        assert "detail" in info, f"{name} missing 'detail' key"
        assert info["status"] in valid_statuses, (
            f"{name} has unexpected status {info['status']!r}"
        )
        assert isinstance(info["detail"], str)


def test_doctor_checks_oneiric_dep_at_0_19_or_above() -> None:
    """Phase 3 Task 4.4 contract: oneiric dep must be >= 0.19."""
    cli = _instantiate_cli()
    checks = cli._doctor_checks()
    assert "oneiric_dep" in checks
    assert checks["oneiric_dep"]["status"] == "ok"


# ----------------------------------------------------------------------
# _health_probe — REAL probe (not an UNAVAILABLE stub)
# ----------------------------------------------------------------------
def test_health_probe_returns_real_snapshot() -> None:
    cli = _instantiate_cli()
    snapshot = cli._health_probe()
    assert isinstance(snapshot, dict)
    # Not the UN-AVAILABLE stub shape — must include real probe data.
    assert "status" in snapshot
    assert snapshot["status"] in {"ok", "degraded", "error"}


def test_health_probe_includes_real_fields() -> None:
    """Snapshot carries concrete Akosha metadata (version, mode, port, modes)."""
    cli = _instantiate_cli()
    snapshot = cli._health_probe()
    # Required keys: version, mode, default_port, modes_available
    for key in ("version", "default_port", "modes_available"):
        assert key in snapshot, f"snapshot missing required key {key!r}"
    assert isinstance(snapshot["modes_available"], list)
    # The mode registry is a real surface — lite + standard should both be present.
    assert "lite" in snapshot["modes_available"]
    assert "standard" in snapshot["modes_available"]


def test_health_probe_default_port_matches_akosha_default() -> None:
    """default_port mirrors akosha.config.DEFAULT_MCP_PORT."""
    from akosha.config import DEFAULT_MCP_PORT

    cli = _instantiate_cli()
    snapshot = cli._health_probe()
    assert snapshot["default_port"] == DEFAULT_MCP_PORT


# ----------------------------------------------------------------------
# BodaiCLIBase.run() — typer wiring
# ----------------------------------------------------------------------
def test_app_run_exposes_version_command() -> None:
    """``version`` is registered by BodaiCLIBase and must work end-to-end."""
    runner = CliRunner()
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    # BodaiCLIBase prints "{component_name}: {version}" — must include the
    # component name and resolved version, but tolerate either capitalization.
    stdout = result.stdout.lower()
    assert "akosha" in stdout


def test_app_run_exposes_doctor_command() -> None:
    """``doctor`` runs the subclass's real _doctor_checks."""
    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    # The doctor output prints "check_name: status - detail" lines.
    assert "package_version:" in result.stdout


def test_app_run_doctor_json_flag_emits_machine_readable() -> None:
    """The unified callback's ``--json`` flag must produce valid JSON.

    Typer global options are parsed before the subcommand, so ``--json``
    must come BEFORE ``doctor`` (not after).
    """
    runner = CliRunner()
    result = runner.invoke(app, ["--json", "doctor"])
    assert result.exit_code == 0, f"doctor --json failed: {result.stdout!r}"
    # Strip any leading log lines from the runner capture so we parse the
    # JSON payload only.
    payload = _extract_json(result.stdout)
    assert "checks" in payload
    assert isinstance(payload["checks"], dict)


def test_app_run_exposes_health_command() -> None:
    """``health`` runs the subclass's real _health_probe."""
    runner = CliRunner()
    result = runner.invoke(app, ["health"])
    # Exit code 0 (ok) or 1 (degraded); both are real-probe shapes — never 3 (UNAVAILABLE).
    assert result.exit_code in (0, 1)
    assert "akosha" in result.stdout or "version" in result.stdout


def test_app_run_health_json_flag_emits_machine_readable() -> None:
    """Health subcommand must accept ``--json`` (global flag) and emit JSON."""
    runner = CliRunner()
    result = runner.invoke(app, ["--json", "health"])
    assert result.exit_code in (0, 1), f"health --json failed: {result.stdout!r}"
    payload = _extract_json(result.stdout)
    assert "status" in payload
    assert "version" in payload


def test_app_run_no_args_shows_help() -> None:
    """``no_args_is_help=True`` (inherited) must surface the unified help."""
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    # Typer emits a Usage line plus the registered subcommand names.
    assert "version" in result.stdout
    assert "doctor" in result.stdout
    assert "health" in result.stdout


# ----------------------------------------------------------------------
# Typer command registration
# ----------------------------------------------------------------------
def test_app_existing_akosha_commands_still_registered() -> None:
    """The migration must NOT drop the existing Akosha commands."""
    runner = CliRunner()
    result = runner.invoke(app, ["info", "--help"])
    assert result.exit_code == 0
    assert "info" in result.stdout.lower()


@pytest.mark.parametrize(
    "cmd_name",
    ["info", "modes", "version", "doctor", "health"],
)
def test_app_subcommands_resolvable(cmd_name: str) -> None:
    """Every Bodai + Akosha subcommand should be discoverable in the Typer app."""
    runner = CliRunner()
    # ``--help`` succeeds for a registered command; fails for an unknown one.
    result = runner.invoke(app, [cmd_name, "--help"])
    assert result.exit_code == 0, f"{cmd_name} not registered: {result.stdout}"


def test_doctor_checks_handles_storage_failure_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``get_config`` raises, status='fail' with the real error message."""
    import importlib

    cli = _instantiate_cli()

    def _bad_get_config() -> Any:
        raise RuntimeError("simulated config failure")

    # The ``akosha.config`` module exposes a module-level ``config`` instance
    # that shadows the module name; use ``importlib`` to grab the actual
    # module object and patch its ``get_config`` attribute directly.
    cfg_module = importlib.import_module("akosha.config")
    monkeypatch.setattr(cfg_module, "get_config", _bad_get_config)
    checks = cli._doctor_checks()
    assert checks["storage_paths"]["status"] == "fail"
    assert "simulated config failure" in checks["storage_paths"]["detail"]
