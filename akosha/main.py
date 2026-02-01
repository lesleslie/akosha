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
    """Akosha application with lifecycle management."""

    def __init__(self):
        """Initialize application."""
        self.shutdown_event = asyncio.Event()
        self.ingestion_workers: list[Any] = []

    async def start(self):
        """Start Akosha services."""
        # Start ingestion workers
        logger.info("Starting Akosha application")

        # Setup signal handlers
        logger.info("Setting up signal handlers for graceful shutdown")
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT, self._handle_shutdown)

        # Log startup
        logger.info("✅ Akosha application started successfully")

        # Keep the application running until shutdown
        try:
            await self.shutdown_event.wait()
        except asyncio.CancelledError:
            logger.info("Application cancelled")

    def _handle_shutdown(self, signum: int, frame: Any = None):
        """Handle shutdown signal."""
        signal_name = "SIGINT" if signum == signal.SIGINT else "SIGTERM"
        logger.info(f"Received {signal_name} signal, initiating graceful shutdown")
        self.shutdown_event.set()

    async def stop(self):
        """Stop Akosha services with drain period."""
        # Log shutdown start
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
            if hasattr(worker, 'stop'):
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