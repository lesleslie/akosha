"""Warm store: DuckDB on-disk for historical data."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import duckdb

if TYPE_CHECKING:
    from pathlib import Path

    from akosha.models import WarmRecord

logger = logging.getLogger(__name__)


class WarmStore:
    """Warm store with DuckDB on-disk storage."""

    def __init__(self, database_path: Path) -> None:
        """Initialize warm store.

        Args:
            database_path: Path to DuckDB database file
        """
        self.db_path = database_path
        self.conn: duckdb.DuckDBPyConnection | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize database schema."""
        async with self._lock:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = duckdb.connect(str(self.db_path))

            # Create warm conversations table (compressed embeddings)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    system_id VARCHAR,
                    conversation_id VARCHAR PRIMARY KEY,
                    embedding INT8[384],  -- Quantized to INT8 (75% size reduction)
                    summary TEXT,  -- Extractive summary (3 sentences)
                    timestamp TIMESTAMP,
                    metadata JSON,
                    uploaded_at TIMESTAMP DEFAULT NOW()
                )
            """)

            # Partition by date for efficient queries
            self.conn.execute("""
                CREATE INDEX IF NOT EXISTS date_partition_idx
                ON conversations (date_trunc('day', timestamp))
            """)

            logger.info(f"Warm store initialized at {self.db_path}")

    async def insert(self, record: WarmRecord) -> None:
        """Insert conversation into warm store.

        Args:
            record: Warm record to insert
        """
        async with self._lock:
            if not self.conn:
                raise RuntimeError("Warm store not initialized")

            self.conn.execute(
                """
                INSERT INTO conversations
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                [
                    record.system_id,
                    record.conversation_id,
                    record.embedding,
                    record.summary,
                    record.timestamp,
                    record.metadata,
                    datetime.now(UTC),
                ],
            )

    async def insert_batch(self, records: list[WarmRecord]) -> None:
        """Insert multiple conversations into warm store in a single batch.

        Args:
            records: List of warm records to insert
        """
        async with self._lock:
            if not self.conn:
                raise RuntimeError("Warm store not initialized")

            if not records:
                return

            # Prepare batch data
            data = [
                (
                    record.system_id,
                    record.conversation_id,
                    record.embedding,
                    record.summary,
                    record.timestamp,
                    record.metadata,
                    datetime.now(UTC),
                )
                for record in records
            ]

            # Batch insert using DuckDB's executemany
            self.conn.executemany(
                """
                INSERT INTO conversations
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                data,
            )

            logger.debug(f"Inserted {len(records)} records into warm store")

    async def close(self) -> None:
        """Close database connection."""
        async with self._lock:
            if self.conn:
                self.conn.close()
                logger.info("Warm store closed")
