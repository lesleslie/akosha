"""Tests for ``BootstrapOrchestrator.report_health`` active probe (audit M3).

Audit M3: the previous ``report_health`` returned ``last_heartbeat``
without verifying reachability — the orchestrator was reporting
"healthy" even when the upstream mahavishnu was unreachable. The
fix actively pings the injected ``mahavishnu_client`` and surfaces
the failure as ``status="degraded"`` + ``ping_error``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from akosha.ingestion.orchestrator import BootstrapOrchestrator


def _make_client(**attrs: object) -> MagicMock:
    """Build a mock mahavishnu client with the given async methods."""
    client = MagicMock()
    for name, value in attrs.items():
        setattr(client, name, value)
    return client


# ---------------------------------------------------------------------------
# Active ping — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_health_pings_mahavishnu_client_when_present() -> None:
    """When a client is injected, ``report_health`` must invoke its ping."""
    client = _make_client(ping=AsyncMock(return_value={"status": "ok"}))
    orch = BootstrapOrchestrator(mahavishnu_client=client)
    health = await orch.report_health()
    client.ping.assert_awaited_once()
    assert health["last_actual_ping"] is not None


@pytest.mark.asyncio
async def test_report_health_marks_degraded_on_ping_failure() -> None:
    """A ping exception surfaces as ``status="degraded"`` + ``ping_error``."""
    client = _make_client(ping=AsyncMock(side_effect=ConnectionError("unreachable")))
    orch = BootstrapOrchestrator(mahavishnu_client=client)
    health = await orch.report_health()
    assert health["status"] == "degraded"
    assert "unreachable" in health["ping_error"]
    # The ping was attempted — we don't hide it.
    client.ping.assert_awaited_once()


@pytest.mark.asyncio
async def test_report_health_propagates_unexpected_ping_exception() -> None:
    """A non-Exception subclass (e.g. BaseException) must NOT be swallowed."""
    client = _make_client(ping=AsyncMock(side_effect=KeyboardInterrupt))
    orch = BootstrapOrchestrator(mahavishnu_client=client)
    with pytest.raises(KeyboardInterrupt):
        await orch.report_health()


# ---------------------------------------------------------------------------
# No client injected — fall back gracefully
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_health_works_without_mahavishnu_client() -> None:
    """No client → skip the ping, still return a valid health dict."""
    orch = BootstrapOrchestrator()  # no client injected
    health = await orch.report_health()
    assert health["status"] == "normal"
    assert health["last_actual_ping"] is None


@pytest.mark.asyncio
async def test_report_health_skips_ping_when_client_lacks_ping_method() -> None:
    """A client without a ``ping`` method is treated as unreachable-skipped."""
    client = MagicMock(spec=[])  # no ping attribute
    orch = BootstrapOrchestrator(mahavishnu_client=client)
    health = await orch.report_health()
    assert health["last_actual_ping"] is None
    assert health["status"] == "normal"


# ---------------------------------------------------------------------------
# Fallback mode + timestamp invariants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_health_status_reflects_fallback_mode() -> None:
    orch = BootstrapOrchestrator()
    orch.fallback_mode = True
    health = await orch.report_health()
    assert health["status"] == "fallback"
    assert health["fallback_mode"] is True


@pytest.mark.asyncio
async def test_report_health_includes_timestamp() -> None:
    orch = BootstrapOrchestrator()
    health = await orch.report_health()
    assert "timestamp" in health
    assert health["timestamp"]  # non-empty ISO 8601 string


@pytest.mark.asyncio
async def test_report_health_keeps_last_mahavishnu_contact_field() -> None:
    """Backwards compat: ``last_mahavishnu_contact`` (self-attested) stays."""
    orch = BootstrapOrchestrator()
    health = await orch.report_health()
    assert "last_mahavishnu_contact" in health
    assert "fallback_mode" in health
