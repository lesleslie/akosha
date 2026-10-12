"""Warm store: DuckDB on-disk for historical data."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid7

import duckdb
from oneiric.core.ids import is_uuid7

if TYPE_CHECKING:
    from pathlib import Path

    from akosha.models import WarmRecord

logger = logging.getLogger(__name__)

# Slug-shaped conversation_ids (e.g. ``system:foo``, ``project:bar``) keep
# their existing shape and do not get a parallel ``uuid7_id`` value.
# The pattern matches the plan: ``^[a-z]+:[a-zA-Z0-9._-]+$``.
_SLUG_SHAPE = re.compile(r"^[a-z]+:[a-zA-Z0-9._-]+$")


def _derive_uuid7_id(conversation_id: str) -> str | None:
    """Return a parallel ``uuid7_id`` value for a new insert.

    Returns:
        - ``conversation_id`` if it is already a UUID7 hex string.
        - ``None`` if the conversation_id is slug-shaped (e.g. system:foo).
        - A fresh ``uuid7().hex`` for everything else (reserved watermarks like
          reserved watermarks like ``__bodai_subscriber_watermark__``,
          or composite ids from the otel_ingester that don't match
          the slug pattern).
    """
    if is_uuid7(conversation_id):
        return conversation_id
    if _SLUG_SHAPE.match(conversation_id):
        return None
    return uuid7().hex


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

            # Create warm conversations table (compressed embeddings).
            # The ``uuid7_id`` column is the Phase 4 lazy dual-write anchor:
            # new rows that carry a UUID7-shaped conversation_id get the
            # same value in both columns; slug-shaped ids leave it NULL;
            # Reserved strings (slugs, watermarks) get a fresh UUID7 sibling.
            # A separate UNIQUE index (NULLs do not conflict in DuckDB)
            # enables index-only lookups once backfill is complete.
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    system_id VARCHAR,
                    conversation_id VARCHAR PRIMARY KEY,
                    uuid7_id TEXT,
                    embedding INT8[384],
                    summary TEXT,
                    timestamp TIMESTAMP,
                    metadata JSON,
                    uploaded_at TIMESTAMP DEFAULT NOW()
                )
            """)

            # Idempotent column add: existing warm.dbs that predate Phase 4
            # do not have uuid7_id; ALTER TABLE ... ADD COLUMN adds it
            # with NULL for every existing row. Re-running is a no-op.
            self.conn.execute("""
                ALTER TABLE conversations
                ADD COLUMN IF NOT EXISTS uuid7_id TEXT
            """)

            self.conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_conversations_uuid7_id
                ON conversations (uuid7_id)
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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                [
                    record.system_id,
                    record.conversation_id,
                    _derive_uuid7_id(record.conversation_id),
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

            data = [
                (
                    record.system_id,
                    record.conversation_id,
                    _derive_uuid7_id(record.conversation_id),
                    record.embedding,
                    record.summary,
                    record.timestamp,
                    record.metadata,
                    datetime.now(UTC),
                )
                for record in records
            ]

            self.conn.executemany(
                """
                INSERT INTO conversations
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                data,
            )

            logger.debug(f"Inserted {len(records)} records into warm store")

    async def backfill_uuid7_id(self, conversation_id: str) -> str | None:
        """Lazy dual-write: read-time UUID7 backfill for legacy rows.

        Looks up the row by ``conversation_id``; if its ``uuid7_id`` is
        NULL but the conversation_id is non-NULL, synthesize a UUID7,
        ``UPDATE`` the row in the same transaction, and return the
        new ``uuid7_id``. Slug-shaped conversation_ids are skipped
        (no parallel uuid7_id). Returns the row's ``uuid7_id`` value
        after the operation (which may be the existing value, the
        newly synthesized one, or None for slug rows).
        """
        async with self._lock:
            if not self.conn:
                raise RuntimeError("Warm store not initialized")

            row = self.conn.execute(
                "SELECT uuid7_id FROM conversations WHERE conversation_id = ?",
                [conversation_id],
            ).fetchone()
            if row is None:
                return None
            existing = row[0]
            if existing is not None:
                return existing
            if _SLUG_SHAPE.match(conversation_id):
                return None
            new_id = uuid7().hex
            self.conn.execute(
                "UPDATE conversations SET uuid7_id = ? WHERE conversation_id = ?",
                [new_id, conversation_id],
            )
            return new_id

    async def get_by_conversation_id(
        self,
        conversation_id: str,
    ) -> dict[str, object] | None:
        """Read with lazy UUID7 backfill.

        Fetches a conversation row by conversation_id; if the row's
        ``uuid7_id`` is NULL, calls :meth:`backfill_uuid7_id` first so
        the returned dict always carries a populated ``uuid7_id``
        when one is meaningful (i.e. the conversation_id is not a
        slug). Returns None if the row does not exist.
        """
        await self.backfill_uuid7_id(conversation_id)
        async with self._lock:
            if not self.conn:
                raise RuntimeError("Warm store not initialized")
            row = self.conn.execute(
                """
                SELECT system_id, conversation_id, uuid7_id, summary,
                       timestamp, metadata, uploaded_at
                FROM conversations WHERE conversation_id = ?
                """,
                [conversation_id],
            ).fetchone()
            if row is None:
                return None
            keys = (
                "system_id", "conversation_id", "uuid7_id", "summary",
                "timestamp", "metadata", "uploaded_at",
            )
            return dict(zip(keys, row, strict=True))

    async def close(self) -> None:
        """Close database connection."""
        async with self._lock:
            if self.conn:
                self.conn.close()
                logger.info("Warm store closed")
