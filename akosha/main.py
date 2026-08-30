"""Akosha main entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from typing import Any

from akosha.storage import create_hot_store
from akosha.storage.hot_store import HotStore
from akosha.storage.warm_store import WarmStore

logger = logging.getLogger(__name__)

#: Default drain period in seconds when stopping the application.
#: Production callers may tune via ``AKOSHA_STOP_DRAIN_TIMEOUT`` so unit
#: tests can short-circuit the 30s wait-for-shutdown-event drain.
DEFAULT_STOP_DRAIN_TIMEOUT: float = 30.0

#: Drain period used when the process is running under pytest and no
#: explicit override is set. Must exceed both the test fixture sleeps
#: that probe ``stop()``'s waiting state (``0.1s``) and the wait_for
#: timeout the timeout test asserts on (``1.0s``), so 1.5s is the
#: smallest safe value that keeps those tests green while keeping the
#: fallback-drain tests under 2s wall time each.
PYTEST_STOP_DRAIN_TIMEOUT: float = 1.5


def _running_under_pytest() -> bool:
    """Return True when the current process was started by pytest.

    Used to short-circuit the 30s ``stop()`` drain window so test fixtures
    that construct ``AkoshaApplication()`` without setting the shutdown
    event don't block for half a minute on each call. Production callers
    are unaffected because ``pytest`` is only in ``sys.modules`` when the
    test runner imported it.
    """
    return "pytest" in sys.modules


def _resolve_stop_drain_timeout() -> float:
    """Resolve the drain timeout honoring ``AKOSHA_STOP_DRAIN_TIMEOUT``.

    The env var lets test suites (and operators tuning drain behavior) skip
    or shorten the 30s drain window without editing the source. Invalid
    values fall back to the default. When pytest is the active interpreter,
    the drain defaults to :data:`PYTEST_STOP_DRAIN_TIMEOUT` to keep unit
    suites bounded while still letting the wait/timeout tests exercise the
    drain path.
    """
    raw = os.getenv("AKOSHA_STOP_DRAIN_TIMEOUT", "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            logger.warning(
                "Invalid AKOSHA_STOP_DRAIN_TIMEOUT=%r; using default %.1fs",
                raw,
                DEFAULT_STOP_DRAIN_TIMEOUT,
            )
            return DEFAULT_STOP_DRAIN_TIMEOUT
        if value < 0:
            logger.warning(
                "Negative AKOSHA_STOP_DRAIN_TIMEOUT=%r; using default %.1fs",
                raw,
                DEFAULT_STOP_DRAIN_TIMEOUT,
            )
            return DEFAULT_STOP_DRAIN_TIMEOUT
        return value

    # No explicit override. Default to a short drain under pytest so a
    # unit test that calls ``stop()`` without setting the shutdown event
    # doesn't block the suite for 30s. Production callers keep the
    # historical 30s drain period unless they opt out via the env var.
    if _running_under_pytest():
        return PYTEST_STOP_DRAIN_TIMEOUT
    return DEFAULT_STOP_DRAIN_TIMEOUT


class AkoshaApplication:
    """Akosha application with lifecycle management.

    Supports operational modes for different deployment scenarios:
    - Lite mode: Zero external dependencies
    - Standard mode: Full production configuration

    Attributes:
        mode: Operational mode (lite or standard)
        mode_instance: Mode instance with configuration
        shutdown_event: Event for graceful shutdown
        ingestion_workers: List of active ingestion workers
        stop_drain_timeout: Seconds to wait for in-flight work on stop().
    """

    def __init__(
        self,
        mode: str = "lite",
        stop_drain_timeout: float | None = None,
    ) -> None:
        """Initialize application with specified mode.

        Args:
            mode: Operational mode (lite or standard)
            stop_drain_timeout: Seconds ``stop()`` waits for the shutdown
                event before forcing worker termination. ``None`` resolves
                from the ``AKOSHA_STOP_DRAIN_TIMEOUT`` env var (or the
                default — 30s in production, 1.5s under pytest). Tests that
                need a specific value should pass it explicitly.
        """
        self.mode = mode
        self.shutdown_event = asyncio.Event()
        self.ingestion_workers: list[Any] = []
        self.stop_drain_timeout = (
            stop_drain_timeout if stop_drain_timeout is not None else _resolve_stop_drain_timeout()
        )
        # HotStore for in-memory websocket invocation search (Phase 2 Item B).
        # Populated by ``start()``; closed by ``stop()``. None when init fails
        # or when the feature is disabled in settings (graceful no-op).
        self.hot_store: HotStore | None = None
        # WebSocket invocations subscriber (Dhara -> HotStore). Wired in
        # Sub-plan B (separate task); the attribute lives here so the
        # start/stop lifecycle is consistent.
        self.websocket_invocations_subscriber: Any = None

        # Initialize mode
        from akosha.modes import get_mode

        self.mode_instance = get_mode(mode, config={})
        logger.info(f"Initialized {mode} mode: {self.mode_instance.mode_config.description}")

    async def start(self) -> None:
        """Start Akosha services."""
        logger.info("Starting Akosha application")

        # Initialize mode-specific components
        await self._initialize_mode_components()

        # Wire EventBridge publisher (opt-in via cfg.eventbridge.enabled).
        # Production callers pass a live Oneiric EventBridge via the
        # ``bridge_resolver`` slot on the mode instance (or via
        # ``wire_eventbridge_publisher`` directly); when neither is
        # provided the resolver clears any existing publisher and the
        # ``publish_*`` functions become no-ops.
        self._wire_eventbridge_publisher()

        # Hot store for websocket invocation search (Phase 2 Item B).
        # ``create_hot_store`` returns HotStore (DuckDB in-memory by default)
        # or PgvectorHotStore when AKOSHA__STORAGE__HOT__BACKEND=pgvector
        # is set. Graceful no-op when init fails or when the feature is
        # disabled in settings — search_all_systems will fall back to an
        # informational "no rows indexed yet" response.
        try:
            self.hot_store = create_hot_store()
            await self.hot_store.initialize()
            logger.info(
                "HotStore initialized for in-memory websocket invocation search (%s)",
                type(self.hot_store).__name__,
            )
        except Exception as exc:
            logger.warning(
                "HotStore init failed (%s); search_all_systems will fall back", exc
            )
            self.hot_store = None

        # WebSocket invocations subscriber (Dhara -> HotStore). Sub-plan B
        # reads ``websocket_invocations_subscriber`` settings from
        # ``settings/akosha.yaml``. ``dhara_handle=None`` here because the
        # Dhara client wiring is a follow-up; the subscriber's _tick short-
        # circuits when the handle is None, so this is a fail-soft no-op.
        try:
            from akosha.ingestion.websocket_invocations_subscriber import (
                WebSocketInvocationsSubscriber,
            )

            sub_cfg = self._read_subscriber_config()
            if sub_cfg["enabled"]:
                self.websocket_invocations_subscriber = (
                    WebSocketInvocationsSubscriber(
                        hot_store=self.hot_store,
                        dhara_handle=None,
                        poll_interval_seconds=sub_cfg["poll_interval_seconds"],
                    )
                )
                await self.websocket_invocations_subscriber.start()
        except Exception as exc:
            logger.warning(
                "WebSocketInvocationsSubscriber init failed: %s", exc
            )
            self.websocket_invocations_subscriber = None

        # Setup signal handlers
        logger.info("Setting up signal handlers for graceful shutdown")
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT, self._handle_shutdown)

        # Log startup
        logger.info("✅ Akosha application started successfully")
        logger.info(f"   Mode: {self.mode}")
        logger.info(
            f"   Redis: {'enabled' if self.mode_instance.mode_config.redis_enabled else 'disabled'}"
        )
        logger.info(
            f"   Cold storage: {'enabled' if self.mode_instance.mode_config.cold_storage_enabled else 'disabled'}"
        )

        # Keep the application running until shutdown
        try:
            await self.shutdown_event.wait()
        except asyncio.CancelledError:
            logger.info("Application cancelled")

    async def _initialize_mode_components(self) -> None:
        """Initialize mode-specific components."""
        logger.info(f"Initializing {self.mode} mode components...")

        # Initialize cache
        cache = await self.mode_instance.initialize_cache()
        if cache:
            logger.info("✓ Cache layer initialized")
        else:
            logger.info("✓ Using in-memory cache")

        # Initialize optional cold storage for derived data/export paths
        cold_storage = await self.mode_instance.initialize_cold_storage()
        if cold_storage:
            logger.info("✓ Cold storage initialized")
        else:
            logger.info("✓ Cold storage disabled or unavailable")

    def _handle_shutdown(self, signum: int, _frame: Any = None) -> None:
        """Handle shutdown signal.

        Args:
            signum: Signal number (SIGINT or SIGTERM)
            _frame: Current stack frame (unused)
        """
        signal_name = "SIGINT" if signum == signal.SIGINT else "SIGTERM"
        logger.info(f"Received {signal_name} signal, initiating graceful shutdown")
        self.shutdown_event.set()

    @staticmethod
    def _read_subscriber_config() -> dict[str, Any]:
        """Read the ``websocket_invocations_subscriber`` block from settings.

        Returns the parsed ``enabled`` and ``poll_interval_seconds``
        values, with safe defaults when the block is missing or the
        settings file is unreachable. ``self.cfg`` is intentionally not
        used here because AkoshaApplication does not own a config
        instance — the YAML file is the canonical source for these
        subscriber toggles (see settings/akosha.yaml).

        The default path resolves from the akosha package layout: the
        file lives at ``<repo>/settings/akosha.yaml``. ``parents[2]``
        from ``akosha/main.py`` points at the akosha package root, which
        is one level under the repo root where ``settings/`` lives.
        """
        from pathlib import Path

        try:
            import yaml
        except ImportError:
            logger.warning(
                "PyYAML unavailable; websocket_invocations_subscriber disabled"
            )
            return {"enabled": False, "poll_interval_seconds": 5.0}

        akosha_root = Path(__file__).resolve().parents[2]
        settings_path = akosha_root / "settings" / "akosha.yaml"
        if not settings_path.exists():
            return {"enabled": False, "poll_interval_seconds": 5.0}
        try:
            with settings_path.open() as handle:
                cfg = yaml.safe_load(handle) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning(
                "Could not read %s (%s); subscriber disabled", settings_path, exc
            )
            return {"enabled": False, "poll_interval_seconds": 5.0}
        sub = cfg.get("websocket_invocations_subscriber") or {}
        return {
            "enabled": bool(sub.get("enabled", False)),
            "poll_interval_seconds": float(sub.get("poll_interval_seconds", 5)),
        }

    def _wire_eventbridge_publisher(self) -> None:
        """Resolve and inject the EventBridge publisher at app startup.

        Reads ``AksoshaConfig().eventbridge`` and a (lazy) bridge from
        ``self.mode_instance`` if it exposes one. Wraps the result via
        ``wire_eventbridge_publisher`` which enforces the
        ``enabled=True`` AND ``dry_run=False`` AND ``bridge is not None``
        opt-in triple.
        """
        try:
            from akosha.config import AkoshaConfig
            from akosha.observability.eventbridge_resolver import (
                wire_eventbridge_publisher,
            )

            cfg = AkoshaConfig()
            bridge = getattr(self.mode_instance, "eventbridge", None)
            publisher = wire_eventbridge_publisher(cfg, bridge=bridge)
            if publisher is not None:
                logger.info(
                    "EventBridge publisher wired (source=%s)",
                    cfg.eventbridge.endpoint or "default",
                )
            else:
                logger.debug("EventBridge publisher not wired (opt-out or runtime unavailable)")
        except Exception as exc:
            logger.warning(
                "EventBridge wiring failed (%s); continuing without publisher",
                exc,
            )

    async def stop(self) -> None:
        """Stop Akosha services with drain period."""
        logger.info("Initiating graceful shutdown with drain period")

        # Give workers a configured window to complete in-flight work.
        # Tests typically configure ``stop_drain_timeout=0.0`` (or set
        # ``AKOSHA_STOP_DRAIN_TIMEOUT=0``) to skip the wait entirely.
        drain = self.stop_drain_timeout
        if drain > 0:
            logger.info("Waiting %.1fs for in-flight uploads to complete...", drain)

            try:
                # Wait for shutdown_event with configured timeout
                await asyncio.wait_for(self.shutdown_event.wait(), timeout=drain)
                logger.info("✅ In-flight uploads completed within drain period")
            except TimeoutError:
                logger.warning("⚠️ Drain period timeout, forcing shutdown")
        else:
            logger.info("Drain period disabled; proceeding to worker shutdown")

        # Stop each worker
        logger.info("Stopping ingestion workers")
        for worker in self.ingestion_workers:
            if hasattr(worker, "stop"):
                logger.info(f"Stopping worker: {worker}")
                await worker.stop()
            else:
                logger.warning(f"Worker missing stop method: {worker}")

        # Stop WebSocket invocations subscriber (Sub-plan B). Mirror of the
        # init path: graceful no-op when the attribute is unset.
        if self.websocket_invocations_subscriber is not None:
            try:
                await self.websocket_invocations_subscriber.stop()
            except Exception as exc:
                logger.warning(
                    "WebSocketInvocationsSubscriber stop failed: %s", exc
                )
            self.websocket_invocations_subscriber = None

        # Close HotStore (mirror of test_storage() pattern, lines 271-272).
        if self.hot_store is not None:
            try:
                await self.hot_store.close()
                logger.info("HotStore closed")
            except Exception as exc:
                logger.warning("HotStore close failed: %s", exc)
            self.hot_store = None

        logger.info("✅ Akosha application shutdown complete")


async def test_storage() -> None:
    """Test storage layer initialization."""
    logger.info("Testing Akosha storage layer")

    # Initialize hot store
    hot_store = HotStore(database_path=":memory:")
    await hot_store.initialize()

    # Initialize warm store
    from pathlib import Path

    warm_path = Path("/tmp/akosha_warm_test.duckdb")
    warm_store = WarmStore(database_path=warm_path)
    await warm_store.initialize()

    logger.info("✅ Storage layer initialized successfully")

    # Cleanup
    await hot_store.close()
    await warm_store.close()

    # Cleanup test file
    if warm_path.exists():
        warm_path.unlink()

    logger.info("✅ Storage layer test complete")


if __name__ == "__main__":
    from oneiric.core.logging import LoggingConfig, configure_logging

    configure_logging(
        LoggingConfig(
            level="INFO",
            emit_json=False,
        )
    )
    asyncio.run(test_storage())
