"""Tests for bootstrap orchestrator (Mahavishnu fallback)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from akosha.ingestion.orchestrator import BootstrapOrchestrator


class TestBootstrapOrchestrator:
    """Test suite for BootstrapOrchestrator."""

    @pytest.fixture
    def mock_mahavishnu_client(self) -> AsyncMock:
        """Create mock Mahavishnu client."""
        client = AsyncMock()
        client.trigger_workflow = AsyncMock()
        return client

    @pytest.fixture
    def orchestrator(self, mock_mahavishnu_client: AsyncMock) -> BootstrapOrchestrator:
        """Create orchestrator with mocked Mahavishnu client."""
        return BootstrapOrchestrator(mahavishnu_client=mock_mahavishnu_client)

    @pytest.fixture
    def orchestrator_no_client(self) -> BootstrapOrchestrator:
        """Create orchestrator without Mahavishnu client."""
        return BootstrapOrchestrator(mahavishnu_client=None)

    def test_initialization_with_client(self, orchestrator: BootstrapOrchestrator) -> None:
        """Test orchestrator initialization with Mahavishnu client."""
        assert orchestrator.mahavishnu_client is not None
        assert not orchestrator.fallback_mode
        assert orchestrator.last_heartbeat is not None

    def test_initialization_without_client(
        self, orchestrator_no_client: BootstrapOrchestrator
    ) -> None:
        """Test orchestrator initialization without Mahavishnu client."""
        assert orchestrator_no_client.mahavishnu_client is None
        # Should not be in fallback mode initially
        assert not orchestrator_no_client.fallback_mode
        assert orchestrator_no_client.last_heartbeat is not None

    @pytest.mark.asyncio
    async def test_trigger_ingestion_success(self, orchestrator: BootstrapOrchestrator) -> None:
        """Test successful ingestion trigger via Mahavishnu."""
        # Mock successful trigger
        orchestrator.mahavishnu_client.trigger_workflow = AsyncMock(
            return_value={"status": "triggered"}
        )

        result = await orchestrator.trigger_ingestion()

        assert result is True
        assert not orchestrator.fallback_mode
        orchestrator.mahavishnu_client.trigger_workflow.assert_called_once_with(
            workflow_name="akosha-daily-ingest"
        )

    @pytest.mark.asyncio
    async def test_trigger_ingestion_fallback_on_error(
        self, orchestrator: BootstrapOrchestrator
    ) -> None:
        """Test fallback activation when Mahavishnu fails."""
        # Mock Mahavishnu failure
        orchestrator.mahavishnu_client.trigger_workflow = AsyncMock(
            side_effect=ConnectionError("Mahavishnu unavailable")
        )

        result = await orchestrator.trigger_ingestion()

        # Should succeed via fallback mode
        assert result is True
        assert orchestrator.fallback_mode

    @pytest.mark.asyncio
    async def test_trigger_ingestion_fallback_persistent(
        self, orchestrator: BootstrapOrchestrator
    ) -> None:
        """Test that fallback mode persists across calls."""
        # First call fails
        orchestrator.mahavishnu_client.trigger_workflow = AsyncMock(
            side_effect=ConnectionError("Mahavishnu unavailable")
        )

        await orchestrator.trigger_ingestion()
        assert orchestrator.fallback_mode

        # Second call should use fallback without trying Mahavishnu
        await orchestrator.trigger_ingestion()
        # Should still be in fallback mode
        assert orchestrator.fallback_mode

    @pytest.mark.asyncio
    async def test_trigger_ingestion_without_client(
        self, orchestrator_no_client: BootstrapOrchestrator
    ) -> None:
        """Test ingestion trigger when no Mahavishnu client."""
        result = await orchestrator_no_client.trigger_ingestion()

        # Should succeed via fallback mode
        assert result is True
        # Fallback mode is activated when no Mahavishnu client is available
        assert orchestrator_no_client.fallback_mode

    @pytest.mark.asyncio
    async def test_report_health_normal_mode(self, orchestrator: BootstrapOrchestrator) -> None:
        """Test health reporting in normal mode."""
        health = await orchestrator.report_health()

        assert health["status"] in ["healthy", "normal"]
        assert "fallback_mode" in health
        assert isinstance(health["fallback_mode"], bool)
        assert "last_mahavishnu_contact" in health
        assert "timestamp" in health

    @pytest.mark.asyncio
    async def test_report_health_without_callable_ping(self) -> None:
        """``ping`` is missing or non-callable → no ping is attempted."""
        # Build a client with no ``ping`` attribute at all.
        client = AsyncMock(spec=["trigger_workflow"])
        orchestrator = BootstrapOrchestrator(mahavishnu_client=client)
        result = await orchestrator.report_health()
        # No ping happened — last_actual_ping is None.
        assert result["last_actual_ping"] is None
        assert "ping_result" not in result or result.get("ping_result") is None

    @pytest.mark.asyncio
    async def test_report_health_ping_failure_marks_degraded(self) -> None:
        """When ``ping()`` raises, ``status`` flips to ``degraded``."""
        client = AsyncMock()
        client.ping = AsyncMock(side_effect=ConnectionError("ping failed"))
        orchestrator = BootstrapOrchestrator(mahavishnu_client=client)
        result = await orchestrator.report_health()
        assert result["status"] == "degraded"
        assert "ping failed" in result["ping_error"]
        assert result["last_actual_ping"] is None

    @pytest.mark.asyncio
    async def test_report_health_ping_success_records_result(self) -> None:
        """A successful ping populates ``last_actual_ping`` and ``ping_result``."""
        client = AsyncMock()
        client.ping = AsyncMock(return_value={"ok": True})
        orchestrator = BootstrapOrchestrator(mahavishnu_client=client)
        result = await orchestrator.report_health()
        assert result["ping_result"] == {"ok": True}
        assert result["last_actual_ping"] is not None

    @pytest.mark.asyncio
    async def test_report_health_fallback_mode(self, orchestrator: BootstrapOrchestrator) -> None:
        """Test health reporting in fallback mode."""
        # Activate fallback mode
        orchestrator.mahavishnu_client.trigger_workflow = AsyncMock(
            side_effect=ConnectionError("Mahavishnu unavailable")
        )
        await orchestrator.trigger_ingestion()

        health = await orchestrator.report_health()

        assert health["fallback_mode"] is True

    @pytest.mark.asyncio
    async def test_heartbeat_updated_on_success(self, orchestrator: BootstrapOrchestrator) -> None:
        """Test that heartbeat is updated on successful trigger."""
        initial_heartbeat = orchestrator.last_heartbeat

        # Wait a bit to ensure timestamp difference
        await asyncio.sleep(0.01)

        # Trigger successful ingestion
        orchestrator.mahavishnu_client.trigger_workflow = AsyncMock(
            return_value={"status": "triggered"}
        )
        await orchestrator.trigger_ingestion()

        # Heartbeat should be updated
        assert orchestrator.last_heartbeat > initial_heartbeat

    @pytest.mark.asyncio
    async def test_multiple_trigger_calls(self, orchestrator: BootstrapOrchestrator) -> None:
        """Test multiple trigger calls."""
        orchestrator.mahavishnu_client.trigger_workflow = AsyncMock(
            return_value={"status": "triggered"}
        )

        # Trigger multiple times
        for _ in range(5):
            result = await orchestrator.trigger_ingestion()
            assert result is True

        # Should have called Mahavishnu 5 times
        assert orchestrator.mahavishnu_client.trigger_workflow.call_count == 5

    def test_orchestrator_autonomy(self) -> None:
        """Test that orchestrator operates autonomously."""
        orchestrator = BootstrapOrchestrator(mahavishnu_client=None)

        # Should not crash without Mahavishnu
        assert orchestrator is not None
        assert not orchestrator.fallback_mode

    @pytest.mark.asyncio
    async def test_trigger_uses_call_tool_when_trigger_workflow_absent(self) -> None:
        """When the client lacks ``trigger_workflow`` but exposes ``call_tool``, use that.

        Some Mahavishnu clients expose a generic ``call_tool`` RPC instead
        of the typed ``trigger_workflow`` shortcut. The orchestrator
        should fall back to ``call_tool(tool_name="workflow-trigger", ...)``.
        """
        client = AsyncMock(spec=["call_tool"])  # no trigger_workflow attribute
        client.call_tool = AsyncMock()
        orchestrator = BootstrapOrchestrator(mahavishnu_client=client)
        result = await orchestrator.trigger_ingestion()
        assert result is True
        client.call_tool.assert_awaited_once_with(
            tool_name="workflow-trigger",
            arguments={"workflow": "akosha-daily-ingest"},
        )

    @pytest.mark.asyncio
    async def test_trigger_logs_warning_when_client_has_neither_method(self) -> None:
        """A client without ``trigger_workflow`` or ``call_tool`` falls through silently."""
        # spec=[] makes the mock have no attributes — hasattr() returns False for both.
        client = AsyncMock(spec=[])
        orchestrator = BootstrapOrchestrator(mahavishnu_client=client)
        # Trigger still returns True (heartbeat is updated, "success" logged),
        # but neither method is invoked.
        result = await orchestrator.trigger_ingestion()
        assert result is True
        # No call was recorded.
        assert not client.method_calls
