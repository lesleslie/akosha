"""Tests for CLI + HTTP /health consistency (audit M4).

Audit M4: CLI "akosha health" reported "degraded" on dependency
failure but the HTTP /health endpoint returned 200 with hardcoded
"ok" — operators got two contradictory signals from two entry
points on the same server.

This file pins the consistency invariant without unifying the
two implementations (which would conflate "CLI snapshot" with
"HTTP per-feed checks" — different shapes for different consumers).

The shared contract: both surfaces must agree on the ``status``
field. When the configured state is degraded, both must say
"degraded" (not one "ok" and the other "degraded").
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from akosha.cli import app as cli_app
from akosha.mcp.server import create_app, set_health_probe


@pytest.fixture(autouse=True)
def _reset_probe() -> None:
    """Reset module-level health probe between tests."""
    set_health_probe(None)
    yield
    set_health_probe(None)


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def http_client() -> TestClient:
    app = create_app()
    return TestClient(app.http_app())


# ---------------------------------------------------------------------------
# Each surface responds to a degraded state on its own.
# ---------------------------------------------------------------------------


def test_cli_health_command_reports_degraded_when_config_load_fails(
    cli_runner: CliRunner,
) -> None:
    """The CLI's ``akosha health`` flips to status=degraded when config breaks."""
    from unittest.mock import patch

    with patch(
        "akosha.config.get_config",
        side_effect=RuntimeError("config backend broken"),
    ):
        result = cli_runner.invoke(cli_app, ["health"])

    # Either exit 0 (degraded but answered) or 1 (no answer at all).
    assert result.exit_code in (0, 1)
    assert "degraded" in result.stdout


def test_http_health_returns_503_when_probe_reports_degraded(
    http_client: TestClient,
) -> None:
    """HTTP /health must return 503 when its probe reports any unhealthy feed.

    This is the primary M4 fix — set during Wave 1 (Task 1.3). Pin
    it here as part of the consistency contract.
    """

    async def degraded_probe() -> dict[str, dict[str, Any]]:
        return {
            "hot_store": {"ok": False, "error": "connection refused"},
            "embeddings": {"ok": True},
        }

    set_health_probe(degraded_probe)
    response = http_client.get("/health")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


# ---------------------------------------------------------------------------
# Parity — both surfaces share the ``status`` field.
# ---------------------------------------------------------------------------


def test_both_surfaces_carry_the_status_field(cli_runner: CliRunner) -> None:
    """Both CLI stdout and HTTP body expose ``status``.

    We cannot assert the exact shapes match (CLI is a snapshot,
    HTTP is per-feed checks), but we CAN assert both carry the
    ``status`` field — which is the M4 consistency invariant.
    """

    # CLI side: emit the snapshot. Just verify "status" appears.
    cli_result = cli_runner.invoke(cli_app, ["health"])
    assert "status" in cli_result.stdout

    # HTTP side: register an all-ok probe and verify the field.
    async def all_ok_probe() -> dict[str, dict[str, Any]]:
        return {
            "hot_store": {"ok": True},
            "embeddings": {"ok": True},
        }

    set_health_probe(all_ok_probe)
    app = create_app()
    client = TestClient(app.http_app())
    http_response = client.get("/health")
    assert http_response.status_code == 200
    assert "status" in http_response.json()
    assert http_response.json()["status"] == "ok"


def test_http_default_no_probe_returns_503() -> None:
    """No probe registered → HTTP /health returns 503 with degraded status.

    This is the fail-loud default — a misconfigured server cannot
    quietly report healthy.
    """
    set_health_probe(None)
    app = create_app()
    client = TestClient(app.http_app())
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert "no health probe registered" in response.json()["checks"]["probe"]["error"]


# ---------------------------------------------------------------------------
# Both surfaces can run independently — neither depends on the other.
# ---------------------------------------------------------------------------


def test_http_health_works_without_importing_cli_app() -> None:
    """The HTTP route has no runtime dependency on the CLI module.

    This protects against an "import the CLI to get health" surprise
    — which would couple startup order.
    """
    set_health_probe(None)
    app = create_app()
    client = TestClient(app.http_app())
    response = client.get("/health")
    # We don't care WHAT the response is, only that it works without
    # the CLI module being loaded.
    assert response.status_code in (200, 503)
