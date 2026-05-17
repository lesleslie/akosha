"""Tests for graceful shutdown lifecycle management."""

from __future__ import annotations

import asyncio
from pathlib import Path
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import akosha.main as main_module
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

    def test_shutdown_handler_sets_event(self, application: AkoshaApplication) -> None:
        """Test that shutdown handler sets the shutdown event."""
        # Initially not set
        assert not application.shutdown_event.is_set()

        # Simulate signal handler
        application._handle_shutdown(signal.SIGTERM, None)

        # Event should be set
        assert application.shutdown_event.is_set()

    def test_shutdown_handler_handles_sigint(self, application: AkoshaApplication) -> None:
        """Test that SIGINT is handled correctly."""
        application._handle_shutdown(signal.SIGINT, None)

        assert application.shutdown_event.is_set()

    def test_shutdown_handler_handles_sigterm(self, application: AkoshaApplication) -> None:
        """Test that SIGTERM is handled correctly."""
        application._handle_shutdown(signal.SIGTERM, None)

        assert application.shutdown_event.is_set()

    @pytest.mark.asyncio
    async def test_stop_calls_worker_stop(self, application: AkoshaApplication) -> None:
        """Test that stop() calls stop() on all workers."""
        await application.stop()

        # All workers should have stop() called
        for worker in application.ingestion_workers:
            worker.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_waits_for_shutdown_event(self, application: AkoshaApplication) -> None:
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
        except TimeoutError:
            pytest.fail("stop() did not complete when event was set")

    @pytest.mark.asyncio
    async def test_stop_timeout_after_30_seconds(self, application: AkoshaApplication) -> None:
        """Test that stop() times out after 30 seconds."""
        # Don't set shutdown event
        stop_task = asyncio.create_task(application.stop())

        # Should timeout after 30 seconds (we'll interrupt after 1 second for testing)
        try:
            await asyncio.wait_for(stop_task, timeout=1.0)
            pytest.fail("Expected timeout")
        except TimeoutError:
            # Expected - stop() is waiting for event
            stop_task.cancel()

    @pytest.mark.asyncio
    async def test_multiple_shutdown_signals(self, application: AkoshaApplication) -> None:
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
    async def test_shutdown_event_coordinate_workers(self, application: AkoshaApplication) -> None:
        """Test that shutdown event coordinates worker shutdown."""
        # Set shutdown event before calling stop
        application.shutdown_event.set()

        # stop() should complete immediately
        await application.stop()

        # Workers should still be stopped
        for worker in application.ingestion_workers:
            worker.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_concurrent_shutdown_calls(self, application: AkoshaApplication) -> None:
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


class TestAkoshaApplicationLifecycle:
    """Test broader application lifecycle behaviors."""

    def _make_mode(self, cache_result: object | None, cold_result: object | None) -> MagicMock:
        mode = MagicMock()
        mode.mode_config.description = "lite mode"
        mode.mode_config.redis_enabled = False
        mode.mode_config.cold_storage_enabled = False
        mode.initialize_cache = AsyncMock(return_value=cache_result)
        mode.initialize_cold_storage = AsyncMock(return_value=cold_result)
        return mode

    @pytest.fixture
    def application(self, monkeypatch: pytest.MonkeyPatch) -> AkoshaApplication:
        """Create an application instance with mocked workers."""
        mode_instance = self._make_mode(cache_result=None, cold_result=None)
        monkeypatch.setattr("akosha.modes.get_mode", MagicMock(return_value=mode_instance))

        app = AkoshaApplication()
        workers = [AsyncMock() for _ in range(3)]
        for worker in workers:
            worker.stop = AsyncMock()
            worker.run = AsyncMock()
        app.ingestion_workers = workers
        return app

    def test_initialization_uses_mode_factory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Application initialization should delegate mode creation to the factory."""
        mode_instance = self._make_mode(cache_result=None, cold_result=None)
        get_mode = MagicMock(return_value=mode_instance)
        monkeypatch.setattr("akosha.modes.get_mode", get_mode)

        app = AkoshaApplication(mode="standard")

        assert app.mode == "standard"
        assert app.mode_instance is mode_instance
        get_mode.assert_called_once_with("standard", config={})

    @pytest.mark.asyncio
    async def test_initialize_mode_components_without_optional_storage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Initialization should handle missing cache and cold storage."""
        mode_instance = self._make_mode(cache_result=None, cold_result=None)
        monkeypatch.setattr("akosha.modes.get_mode", MagicMock(return_value=mode_instance))

        app = AkoshaApplication()
        await app._initialize_mode_components()

        mode_instance.initialize_cache.assert_awaited_once()
        mode_instance.initialize_cold_storage.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_initialize_mode_components_with_optional_storage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Initialization should also cover the branch where optional storage is present."""
        mode_instance = self._make_mode(cache_result=object(), cold_result=object())
        monkeypatch.setattr("akosha.modes.get_mode", MagicMock(return_value=mode_instance))

        app = AkoshaApplication()
        await app._initialize_mode_components()

        mode_instance.initialize_cache.assert_awaited_once()
        mode_instance.initialize_cold_storage.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_registers_signal_handlers_and_waits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Start should install signal handlers and wait for shutdown."""
        mode_instance = self._make_mode(cache_result=None, cold_result=None)
        monkeypatch.setattr("akosha.modes.get_mode", MagicMock(return_value=mode_instance))

        app = AkoshaApplication()
        app._initialize_mode_components = AsyncMock()
        app.shutdown_event.wait = AsyncMock(return_value=None)
        signal_calls: list[signal.Signals] = []

        def fake_signal(sig: signal.Signals, handler: object) -> None:
            signal_calls.append(sig)

        monkeypatch.setattr(main_module.signal, "signal", fake_signal)

        await app.start()

        assert signal_calls == [signal.SIGTERM, signal.SIGINT]
        app._initialize_mode_components.assert_awaited_once()
        app.shutdown_event.wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_handles_cancelled_wait(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Start should swallow a cancelled shutdown wait and return cleanly."""
        mode_instance = self._make_mode(cache_result=None, cold_result=None)
        monkeypatch.setattr("akosha.modes.get_mode", MagicMock(return_value=mode_instance))

        app = AkoshaApplication()
        app._initialize_mode_components = AsyncMock()
        app.shutdown_event.wait = AsyncMock(side_effect=asyncio.CancelledError())
        monkeypatch.setattr(main_module.signal, "signal", MagicMock())

        await app.start()

    @pytest.mark.asyncio
    async def test_stop_handles_workers_without_stop_method(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Stop should skip workers that do not implement stop()."""
        mode_instance = self._make_mode(cache_result=None, cold_result=None)
        monkeypatch.setattr("akosha.modes.get_mode", MagicMock(return_value=mode_instance))

        app = AkoshaApplication()
        app.shutdown_event.set()
        app.ingestion_workers = [object()]

        await app.stop()

    @pytest.mark.asyncio
    async def test_test_storage_uses_mock_stores(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The storage smoke test should work against mocked storage classes."""
        hot_instance = MagicMock()
        hot_instance.initialize = AsyncMock()
        hot_instance.close = AsyncMock()
        warm_instance = MagicMock()
        warm_instance.initialize = AsyncMock()
        warm_instance.close = AsyncMock()

        monkeypatch.setattr(main_module, "HotStore", MagicMock(return_value=hot_instance))
        monkeypatch.setattr(main_module, "WarmStore", MagicMock(return_value=warm_instance))

        await main_module.test_storage()

        hot_instance.initialize.assert_awaited_once()
        hot_instance.close.assert_awaited_once()
        warm_instance.initialize.assert_awaited_once()
        warm_instance.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_test_storage_removes_existing_temp_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The storage smoke test should remove an existing temp artifact."""
        hot_instance = MagicMock()
        hot_instance.initialize = AsyncMock()
        hot_instance.close = AsyncMock()
        warm_instance = MagicMock()
        warm_instance.initialize = AsyncMock()
        warm_instance.close = AsyncMock()

        unlink = MagicMock()
        monkeypatch.setattr(main_module, "HotStore", MagicMock(return_value=hot_instance))
        monkeypatch.setattr(main_module, "WarmStore", MagicMock(return_value=warm_instance))
        monkeypatch.setattr("pathlib.Path.exists", MagicMock(return_value=True))
        monkeypatch.setattr("pathlib.Path.unlink", unlink)

        await main_module.test_storage()

        unlink.assert_called_once()

    def test_module_main_block_calls_asyncio_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The __main__ guard should call asyncio.run with the storage smoke test."""
        called: dict[str, object] = {}

        def fake_run(coro: object) -> None:
            called["coro"] = coro
            close = getattr(coro, "close", None)
            if callable(close):
                close()

        monkeypatch.setattr(asyncio, "run", fake_run)

        source = Path(main_module.__file__).read_text()
        exec(
            compile(source, main_module.__file__ or "<main>", "exec"),
            {"__name__": "__main__", "__file__": main_module.__file__},
        )

        assert "coro" in called

    @pytest.mark.asyncio
    async def test_worker_cleanup_on_shutdown(self, application: AkoshaApplication) -> None:
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
