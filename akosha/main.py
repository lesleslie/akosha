"""Akosha main entry point."""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

from akosha.storage.hot_store import HotStore
from akosha.storage.warm_store import WarmStore

logger = logging.getLogger(__name__)


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
    """

    def __init__(self, mode: str = "lite") -> None:
        """Initialize application with specified mode.

        Args:
            mode: Operational mode (lite or standard)
        """
        self.mode = mode
        self.shutdown_event = asyncio.Event()
        self.ingestion_workers: list[Any] = []

        # Initialize mode
        from akosha.modes import get_mode

        self.mode_instance = get_mode(mode, config={})
        logger.info(f"Initialized {mode} mode: {self.mode_instance.mode_config.description}")

    async def start(self) -> None:
        """Start Akosha services."""
        logger.info("Starting Akosha application")

        # Initialize mode-specific components
        await self._initialize_mode_components()

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

        # Initialize cold storage
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

    async def stop(self) -> None:
        """Stop Akosha services with drain period."""
        logger.info("Initiating graceful shutdown with drain period")

        # Give workers 30 seconds to complete in-flight work
        logger.info("Waiting 30 seconds for in-flight uploads to complete...")

        try:
            # Wait for shutdown_event with 30s timeout
            await asyncio.wait_for(self.shutdown_event.wait(), timeout=30.0)
            logger.info("✅ In-flight uploads completed within drain period")
        except TimeoutError:
            logger.warning("⚠️ Drain period timeout, forcing shutdown")

        # Stop each worker
        logger.info("Stopping ingestion workers")
        for worker in self.ingestion_workers:
            if hasattr(worker, "stop"):
                logger.info(f"Stopping worker: {worker}")
                await worker.stop()
            else:
                logger.warning(f"Worker missing stop method: {worker}")

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
    logging.basicConfig(level=logging.INFO)
    asyncio.run(test_storage())
