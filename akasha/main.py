"""Akasha main entry point."""

from __future__ import annotations

import asyncio
import logging

from akasha.config import config
from akasha.storage.hot_store import HotStore
from akasha.storage.warm_store import WarmStore

logger = logging.getLogger(__name__)


async def test_storage() -> None:
    """Test storage layer initialization."""
    logger.info("Testing Akasha storage layer")

    # Initialize hot store
    hot_store = HotStore(database_path=":memory:")
    await hot_store.initialize()

    # Initialize warm store
    from pathlib import Path
    warm_path = Path("/tmp/akasha_warm_test.duckdb")
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
