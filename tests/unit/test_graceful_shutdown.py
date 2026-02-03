"""Tests for graceful shutdown lifecycle management."""

from __future__ import annotations

import asyncio
import signal
from unittest.mock import AsyncMock, MagicMock

import pytest

from akosha.main import AkoshaApplication


class TestAkoshaApplication:
    """Test suite for AkoshaApplication graceful shutdown."""

    @pytest.fixture
    def mock_workers(self) -> list[AsyncMock]:
        """Create mock ingestion workers."""
        workers = [AsyncMock() for _ in range(3)]
        for worker in workers:
            worker.stop = AsyncMock()  # Async stop (awaited by application.stop())
            worker.run = AsyncMock()  # Async run
        return workers

    @pytest.fixture
    def application(self, mock_workers: list[AsyncMock]) -> AkoshaApplication:
        """Create application with mocked workers."""
        app = AkoshaApplication()
        app.ingestion_workers = mock_workers
        return app

    def test_initialization(self, application: AkoshaApplication) -> None:
        """Test application initialization."""
        assert application.shutdown_event is not None
        assert isinstance(application.shutdown_event, asyncio.Event)
        assert application.ingestion_workers is not None

    def test_shutdown_handler_sets_event(
        self, application: AkoshaApplication
    ) -> None:
        """Test that shutdown handler sets the shutdown event."""
        # Initially not set
        assert not application.shutdown_event.is_set()

        # Simulate signal handler
        application._handle_shutdown(signal.SIGTERM, None)

        # Event should be set
        assert application.shutdown_event.is_set()

    def test_shutdown_handler_handles_sigint(
        self, application: AkoshaApplication
    ) -> None:
        """Test that SIGINT is handled correctly."""
        application._handle_shutdown(signal.SIGINT, None)

        assert application.shutdown_event.is_set()

    def test_shutdown_handler_handles_sigterm(
        self, application: AkoshaApplication
    ) -> None:
        """Test that SIGTERM is handled correctly."""
        application._handle_shutdown(signal.SIGTERM, None)

        assert application.shutdown_event.is_set()

    @pytest.mark.asyncio
    async def test_stop_calls_worker_stop(
        self, application: AkoshaApplication
    ) -> None:
        """Test that stop() calls stop() on all workers."""
        await application.stop()

        # All workers should have stop() called
        for worker in application.ingestion_workers:
            worker.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_waits_for_shutdown_event(
        self, application: AkoshaApplication
    ) -> None:
        """Test that stop() waits for shutdown event."""
        # Don't set the event initially
        stop_task = asyncio.create_task(application.stop())

        # Wait a bit
        await asyncio.sleep(0.1)

        # Task should still be running (waiting for event)
        assert not stop_task.done()

        # Set the event to unblock
        application.shutdown_event.set()

        # Now stop() should complete
        try:
            await asyncio.wait_for(stop_task, timeout=1.0)
        except asyncio.TimeoutError:
            pytest.fail("stop() did not complete when event was set")

    @pytest.mark.asyncio
    async def test_stop_timeout_after_30_seconds(
        self, application: AkoshaApplication
    ) -> None:
        """Test that stop() times out after 30 seconds."""
        # Don't set shutdown event
        stop_task = asyncio.create_task(application.stop())

        # Should timeout after 30 seconds (we'll interrupt after 1 second for testing)
        try:
            await asyncio.wait_for(stop_task, timeout=1.0)
            pytest.fail("Expected timeout")
        except asyncio.TimeoutError:
            # Expected - stop() is waiting for event
            stop_task.cancel()

    @pytest.mark.asyncio
    async def test_multiple_shutdown_signals(
        self, application: AkoshaApplication
    ) -> None:
        """Test handling multiple shutdown signals."""
        # Send multiple signals
        application._handle_shutdown(signal.SIGTERM, None)
        application._handle_shutdown(signal.SIGINT, None)

        # Should not crash
        assert application.shutdown_event.is_set()

    @pytest.mark.asyncio
    async def test_stop_with_no_workers(self) -> None:
        """Test stop() when there are no workers."""
        app = AkoshaApplication()
        app.ingestion_workers = []

        # Should not crash
        await app.stop()

    @pytest.mark.asyncio
    async def test_shutdown_event_coordinate_workers(
        self, application: AkoshaApplication
    ) -> None:
        """Test that shutdown event coordinates worker shutdown."""
        # Set shutdown event before calling stop
        application.shutdown_event.set()

        # stop() should complete immediately
        await application.stop()

        # Workers should still be stopped
        for worker in application.ingestion_workers:
            worker.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_concurrent_shutdown_calls(
        self, application: AkoshaApplication
    ) -> None:
        """Test handling concurrent calls to stop()."""
        # Call stop() multiple times concurrently
        tasks = [application.stop() for _ in range(3)]

        # Set event to unblock all
        application.shutdown_event.set()

        # All should complete
        await asyncio.gather(*tasks)

        # Workers should be stopped (may be called multiple times)
        for worker in application.ingestion_workers:
            assert worker.stop.call_count >= 1

    def test_application_lifecycle(self, application: AkoshaApplication) -> None:
        """Test complete application lifecycle."""
        # 1. Application initialized
        assert not application.shutdown_event.is_set()

        # 2. Shutdown signal received
        application._handle_shutdown(signal.SIGTERM, None)
        assert application.shutdown_event.is_set()

        # 3. Workers can be stopped (tested in async tests above)

    @pytest.mark.asyncio
    async def test_worker_cleanup_on_shutdown(
        self, application: AkoshaApplication
    ) -> None:
        """Test that workers are cleaned up properly on shutdown."""
        # Simulate workers doing work
        async def mock_run():
            await asyncio.sleep(0.1)

        for worker in application.ingestion_workers:
            worker.run = mock_run

        # Trigger shutdown
        application.shutdown_event.set()
        await application.stop()

        # Workers should be stopped
        for worker in application.ingestion_workers:
            worker.stop.assert_called_once()
