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
    # Phase 4: body ``status`` mirrors the aggregator's verdict enum.
    # The custom probe has no data feeds, so the aggregator's default
    # worst_status is "healthy" → body["status"] == "healthy". The
    # HTTP code stays 200 (healthy < warming_up threshold).
    assert http_response.json()["status"] == "healthy"


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


# ---------------------------------------------------------------------------
# Third surface: MCP ``get_health()`` tool.
# ---------------------------------------------------------------------------
# Akosha ships three health surfaces: CLI snapshot, HTTP ``/health``
# route, and (as of the mcp-backend-wiring-discipline fix) the MCP
# ``get_health()`` tool. The MCP body MUST match the HTTP body so that
# radar / orchestrator callers polling via MCP get the same verdict
# the load balancer sees on its HTTP probe. Divergence is the bug
# the wiring discipline was written to surface.


@pytest.fixture
def mcp_app() -> Any:
    """Build a real FastMCP app via ``create_app()`` for tool-surface tests.

    Real app (not a mock) is required so ``list_tools()`` and
    ``call_tool()`` exercise FastMCP's actual tool-resolution path —
    mocks would let us fake ``get_health`` into existence on a
    different module without proving the registration landed.
    """
    return create_app()


@pytest.mark.asyncio
async def test_mcp_get_health_is_registered(mcp_app: Any) -> None:
    """The ``get_health`` MCP tool must appear in the live tool surface.

    Without it, bodai-radar's parallel ``mcp__akosha__get_health``
    fan-out returns "tool not found" and the Akosha health signal
    drops to ``:grey_circle:``. This test pins the registration so a
    future refactor (e.g. moving the tool into a profile-gated group
    that ``AKOSHA_TOOL_PROFILE=minimal`` would disable) cannot silently
    strip it.
    """
    tools = await mcp_app.list_tools()
    tool_names = sorted(t.name for t in tools)
    assert "get_health" in tool_names, (
        f"get_health missing from akosha MCP tool surface. Available: "
        f"{tool_names}. Re-register the tool inside create_app() or "
        f"document why it is profile-gated (and update this test)."
    )


@pytest.mark.asyncio
async def test_mcp_get_health_body_matches_http_route_when_all_ok(
    mcp_app: Any,
) -> None:
    """Healthy probe → MCP body == HTTP body (drift is the discipline bug).

    Both surfaces route through ``_compute_health_body_and_status()``
    in ``akosha.mcp.server`` so they MUST agree. We compare every
    field except ``uptime_seconds`` (clock moves; duration drifts by
    a few microseconds between the two calls).
    """

    async def all_ok_probe() -> dict[str, dict[str, Any]]:
        return {
            "hot_store": {"ok": True},
            "embeddings": {"ok": True, "mode": "real"},
            "cold_storage": {"ok": True},
        }

    set_health_probe(all_ok_probe)
    client = TestClient(mcp_app.http_app())

    http_response = client.get("/health")
    tool_record = await mcp_app.get_tool("get_health")
    mcp_body = await tool_record.fn()

    assert http_response.status_code == 200
    assert http_response.json()["status"] == "healthy"
    assert mcp_body["status"] == "healthy"

    # Field-by-field parity; tolerate the uptime clock advancing.
    http_body = http_response.json()
    for field in ("status", "service", "version", "launcher", "checks"):
        assert mcp_body[field] == http_body[field], (
            f"MCP/HTTP drift on {field!r}: MCP={mcp_body[field]!r} "
            f"HTTP={http_body[field]!r}"
        )
    assert "http_status" in mcp_body  # canonical wire snapshot
    assert mcp_body["http_status"] == http_response.status_code


@pytest.mark.asyncio
async def test_mcp_get_health_reports_degraded_with_failing_probe(
    mcp_app: Any,
) -> None:
    """Degraded probe → MCP body ``status=degraded`` + failing-feed detail.

    Symmetric with ``test_http_health_returns_503_when_any_feed_unhealthy``
    — the MCP caller sees the same verdict as the load balancer.
    """

    async def degraded_probe() -> dict[str, dict[str, Any]]:
        return {
            "hot_store": {"ok": False, "error": "connection refused"},
            "embeddings": {"ok": True},
        }

    set_health_probe(degraded_probe)
    tool_record = await mcp_app.get_tool("get_health")
    body = await tool_record.fn()

    assert body["status"] == "degraded"
    assert body["http_status"] == 503
    assert body["checks"]["hot_store"]["ok"] is False
    assert "connection refused" in body["checks"]["hot_store"]["error"]
    assert body["checks"]["embeddings"]["ok"] is True


@pytest.mark.asyncio
async def test_mcp_get_health_reports_degraded_when_no_probe_registered(
    mcp_app: Any,
) -> None:
    """Fail-loud default: no probe → MCP body ``status=degraded``.

    Mirrors ``test_health_returns_503_when_no_probe_registered`` for
    the HTTP route. A misconfigured server MUST NOT silently report
    healthy.
    """
    set_health_probe(None)
    tool_record = await mcp_app.get_tool("get_health")
    body = await tool_record.fn()

    assert body["status"] == "degraded"
    assert body["http_status"] == 503
    assert body["checks"]["probe"]["ok"] is False
    assert "no health probe registered" in body["checks"]["probe"]["error"]


@pytest.mark.asyncio
async def test_mcp_get_health_includes_uptime_seconds(mcp_app: Any) -> None:
    """``uptime_seconds`` is non-negative and bounded by wall-clock sanity.

    The helper captures ``_HEALTH_START_TIME`` at module-import time
    and subtracts from ``time.time()`` — uptime should always be ≥ 0
    and never absurdly large (cap at 1 hour for a unit test process).
    """
    set_health_probe(None)
    tool_record = await mcp_app.get_tool("get_health")
    body = await tool_record.fn()

    assert "uptime_seconds" in body
    assert isinstance(body["uptime_seconds"], (int, float))
    assert body["uptime_seconds"] >= 0
    assert body["uptime_seconds"] < 3600, (
        f"uptime_seconds={body['uptime_seconds']!r} implausibly large "
        f"for a process spawned during a unit test"
    )
