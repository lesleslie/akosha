"""Hot store: DuckDB in-memory for recent data."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import duckdb

if TYPE_CHECKING:
    from pathlib import Path

    from akosha.models import HotRecord

logger = logging.getLogger(__name__)


class HotStore:
    """Hot store with DuckDB in-memory storage."""

    def __init__(self, database_path: str | Path = ":memory:") -> None:
        """Initialize hot store.

        Args:
            database_path: DuckDB database path (":memory:" for in-memory)
        """
        self.db_path = database_path
        self.conn: duckdb.DuckDBPyConnection | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize database schema."""
        async with self._lock:
            self.conn = duckdb.connect(str(self.db_path))

            # Create conversations table with HNSW index support
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    system_id VARCHAR,
                    conversation_id VARCHAR PRIMARY KEY,
                    content TEXT,
                    embedding FLOAT[384],
                    timestamp TIMESTAMP,
                    metadata JSON,
                    content_hash VARCHAR,
                    uploaded_at TIMESTAMP DEFAULT NOW()
                )
            """)

            # Create HNSW index for vector search
            try:
                self.conn.execute("""
                    CREATE INDEX IF NOT EXISTS embedding_hnsw_index
                    ON conversations USING HNSW (embedding)
                    WITH (m = 16, ef_construction = 200)
                """)
            except Exception as e:
                logger.warning(f"HNSW index creation failed: {e}")

            logger.info("Hot store initialized")

    async def insert(self, record: HotRecord) -> None:
        """Insert conversation into hot store.

        Args:
            record: Hot record to insert
        """
        async with self._lock:
            if not self.conn:
                raise RuntimeError("Hot store not initialized")

            self.conn.execute(
                """
                INSERT INTO conversations
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                [
                    record.system_id,
                    record.conversation_id,
                    record.content,
                    record.embedding,
                    record.timestamp,
                    record.metadata,
                    self._compute_content_hash(record.content),
                    datetime.now(UTC),
                ],
            )

    async def search_similar(
        self,
        query_embedding: list[float],
        system_id: str | None = None,
        limit: int = 10,
        threshold: float = 0.7,
    ) -> list[dict[str, Any]]:
        """Search for similar conversations using vector similarity.

        Args:
            query_embedding: Query vector (FLOAT[384])
            system_id: Optional system filter
            limit: Maximum results to return
            threshold: Minimum similarity score (0-1)

        Returns:
            List of similar conversations with metadata
        """
        async with self._lock:
            if not self.conn:
                raise RuntimeError("Hot store not initialized")

            # Set HNSW search parameters
            import contextlib

            with contextlib.suppress(Exception):
                self.conn.execute("SET hnsw_ef_search = 100")

            # Build query
            where_clause = f"WHERE system_id = '{system_id}'" if system_id else ""
            query = f"""
                SELECT
                    system_id,
                    conversation_id,
                    content,
                    timestamp,
                    metadata,
                    array_cosine_similarity(embedding, ?::FLOAT[384]) as similarity
                FROM conversations
                {where_clause}
                ORDER BY similarity DESC
                LIMIT ?
            """

            results = self.conn.execute(query, [query_embedding, limit]).fetchall()

            # Filter by threshold
            return [
                {
                    "system_id": r[0],
                    "conversation_id": r[1],
                    "content": r[2],
                    "timestamp": r[3],
                    "metadata": r[4],
                    "similarity": r[5],
                }
                for r in results
                if r[5] >= threshold
            ]

    @staticmethod
    def _compute_content_hash(content: str) -> str:
        """Compute SHA-256 hash of content."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    async def close(self) -> None:
        """Close database connection."""
        async with self._lock:
            if self.conn:
                self.conn.close()
                logger.info("Hot store closed")
