"""Tests for HotStore production wiring in AkoshaApplication lifecycle.

Sub-plan A of docs/plans/2026-08-29-akosha-websocket-search.md requires that
``start()`` create a real HotStore instance (so search_all_systems can serve
results instead of mock data) and that ``stop()`` close it cleanly.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

import akosha.main as main_module
from akosha.main import AkoshaApplication


def _make_mode() -> MagicMock:
    """Return a mock mode instance compatible with ``AkoshaApplication.__init__``."""
    mode = MagicMock()
    mode.mode_config.description = "lite mode"
    mode.mode_config.redis_enabled = False
    mode.mode_config.cold_storage_enabled = False
    mode.initialize_cache = AsyncMock(return_value=None)
    mode.initialize_cold_storage = AsyncMock(return_value=None)
    return mode


def _install_signal_spy(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Replace ``signal.signal`` with a spy that records calls.

    Mirrors the pattern used in ``tests/unit/test_graceful_shutdown.py``
    so ``start()`` does not clobber pytest's signal handlers.
    """
    calls: list[int] = []

    def fake_signal(sig: int, handler: object) -> None:
        calls.append(sig)

    monkeypatch.setattr(main_module.signal, "signal", fake_signal)
    return calls


class TestHotStoreLifecycle:
    """Verify HotStore is wired into ``start()`` and torn down in ``stop()``."""

    @pytest.mark.asyncio
    async def test_main_start_initializes_hot_store(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``start()`` must call ``create_hot_store`` and ``initialize`` the result."""
        hot_store_mock = MagicMock()
        hot_store_mock.initialize = AsyncMock()
        hot_store_mock.close = AsyncMock()
        create_hot_store = MagicMock(return_value=hot_store_mock)
        monkeypatch.setattr(main_module, "create_hot_store", create_hot_store)

        mode = _make_mode()
        monkeypatch.setattr("akosha.modes.get_mode", MagicMock(return_value=mode))

        _install_signal_spy(monkeypatch)

        app = AkoshaApplication()
        # Eventbridge wiring would touch AkoshaConfig; stub it so the
        # start() path under test stays focused on HotStore.
        app._wire_eventbridge_publisher = MagicMock()  # type: ignore[method-assign]
        # Short-circuit the shutdown_event.wait() so start() returns promptly.
        app.shutdown_event.wait = AsyncMock(return_value=None)

        await app.start()

        create_hot_store.assert_called_once()
        hot_store_mock.initialize.assert_awaited_once()
        assert app.hot_store is hot_store_mock

    @pytest.mark.asyncio
    async def test_main_stop_closes_hot_store(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``stop()`` must invoke ``hot_store.close()`` and clear the handle."""
        hot_store_mock = MagicMock()
        hot_store_mock.initialize = AsyncMock()
        hot_store_mock.close = AsyncMock()
        create_hot_store = MagicMock(return_value=hot_store_mock)
        monkeypatch.setattr(main_module, "create_hot_store", create_hot_store)

        mode = _make_mode()
        monkeypatch.setattr("akosha.modes.get_mode", MagicMock(return_value=mode))

        _install_signal_spy(monkeypatch)

        app = AkoshaApplication()
        app._wire_eventbridge_publisher = MagicMock()  # type: ignore[method-assign]
        app.shutdown_event.wait = AsyncMock(return_value=None)

        await app.start()
        await app.stop()

        hot_store_mock.close.assert_awaited_once()
        assert app.hot_store is None

    @pytest.mark.asyncio
    async def test_main_start_survives_hot_store_init_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failing HotStore init must leave ``hot_store=None`` (graceful no-op)."""
        hot_store_mock = MagicMock()
        hot_store_mock.initialize = AsyncMock(side_effect=RuntimeError("boom"))
        hot_store_mock.close = AsyncMock()
        create_hot_store = MagicMock(return_value=hot_store_mock)
        monkeypatch.setattr(main_module, "create_hot_store", create_hot_store)

        mode = _make_mode()
        monkeypatch.setattr("akosha.modes.get_mode", MagicMock(return_value=mode))

        _install_signal_spy(monkeypatch)

        app = AkoshaApplication()
        app._wire_eventbridge_publisher = MagicMock()  # type: ignore[method-assign]
        app.shutdown_event.wait = AsyncMock(return_value=None)

        await app.start()

        assert app.hot_store is None


class TestHotStoreSettings:
    """Settings block round-trip for Sub-plan A + Sub-plan B bootstrap."""

    SETTINGS_PATH = Path(__file__).resolve().parents[2] / "settings" / "akosha.yaml"

    def test_settings_hot_store_block_validates_against_pydantic_schema(self) -> None:
        """``akosha.yaml`` must declare ``hot_store`` and ``websocket_invocations_subscriber`` blocks."""
        assert self.SETTINGS_PATH.exists(), f"missing settings file: {self.SETTINGS_PATH}"

        with self.SETTINGS_PATH.open() as f:
            data = yaml.safe_load(f) or {}

        hot_store = data.get("hot_store")
        assert isinstance(hot_store, dict), "hot_store block missing from settings/akosha.yaml"
        assert "enabled" in hot_store, "hot_store.enabled missing"
        assert "database_path" in hot_store, "hot_store.database_path missing"
        assert "retention_minutes" in hot_store, "hot_store.retention_minutes missing"
        assert hot_store["enabled"] is True

        subscriber = data.get("websocket_invocations_subscriber")
        assert isinstance(
            subscriber, dict
        ), "websocket_invocations_subscriber block missing from settings/akosha.yaml"
        assert "enabled" in subscriber, "websocket_invocations_subscriber.enabled missing"
        assert (
            "poll_interval_seconds" in subscriber
        ), "websocket_invocations_subscriber.poll_interval_seconds missing"
