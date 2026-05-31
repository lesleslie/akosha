"""Pgvector-backed hot store for Akosha — implements HotStore interface via Oneiric PgvectorAdapter.

This is a drop-in replacement for HotStore (DuckDB) when AKOSHA__STORAGE__HOT__BACKEND=pgvector.

Key interface differences from HotStore (DuckDB):
- PgvectorAdapter is collection-based; all operations require a collection name
- Methods are async (PgvectorAdapter is async-native)
- `search_similar` threshold is applied post-query in Python (PgvectorAdapter doesn't have a threshold param)
- `get_by_id` takes a list of ids and returns list (PgvectorAdapter.get() is list-based)

Collection name: "conversations" (hardcoded, matches DuckDB HotStore table name).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from oneiric.adapters.vector.pgvector import PgvectorAdapter, PgvectorSettings
from oneiric.adapters.vector.vector_types import VectorDocument

if TYPE_CHECKING:
    from akosha.models import HotRecord

logger = logging.getLogger(__name__)

_COLLECTION_NAME = "conversations"
_EMBEDDING_DIMENSION = 384


class PgvectorHotStore:
    """Pgvector-backed hot store — mirrors HotStore interface using Oneiric's PgvectorAdapter.

    Initialize with pgvector settings from config:
        adapter = PgvectorHotStore(pg_url="postgresql://localhost:5432/akosha")
        await adapter.initialize()
        await adapter.insert(record)
    """

    def __init__(
        self,
        pg_url: str,
        *,
        embedding_dimension: int = _EMBEDDING_DIMENSION,
    ) -> None:
        """Initialize PgvectorHotStore.

        Args:
            pg_url: PostgreSQL connection string (DSN format).
            embedding_dimension: Embedding vector dimension (default 384 for all-MiniLM-L6-v2).
        """
        self._pg_url = pg_url
        self._embedding_dimension = embedding_dimension
        self._adapter: PgvectorAdapter | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize pgvector adapter and ensure collection exists."""
        async with self._lock:
            settings = PgvectorSettings(dsn=self._pg_url)
            self._adapter = PgvectorAdapter(settings)
            await self._adapter.init()

            # Ensure conversations collection exists with correct schema
            await self._adapter.create_collection(
                name=_COLLECTION_NAME,
                dimension=self._embedding_dimension,
                distance_metric="cosine",
            )
            logger.info("PgvectorHotStore initialized (collection=%s)", _COLLECTION_NAME)

    async def insert(self, record: HotRecord) -> None:
        """Insert a HotRecord into the conversations collection.

        Args:
            record: HotRecord with system_id, conversation_id, content, embedding, timestamp, metadata.
        """
        if self._adapter is None:
            raise RuntimeError("PgvectorHotStore not initialized. Call initialize() first.")

        # Map HotRecord → VectorDocument
        doc = VectorDocument(
            id=record.conversation_id,
            metadata={
                "system_id": record.system_id,
                "content": record.content,
                "timestamp": record.timestamp.isoformat()
                if hasattr(record.timestamp, "isoformat")
                else str(record.timestamp),
            }
            | dict(record.metadata.items()),
            vector=record.embedding,
        )

        await self._adapter.insert(_COLLECTION_NAME, [doc])

    async def search_similar(
        self,
        query_embedding: list[float],
        system_id: str | None = None,
        limit: int = 10,
        threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        """Search for similar records by embedding.

        Args:
            query_embedding: Query vector.
            system_id: Optional filter — if set, only return records where metadata.system_id matches.
            limit: Maximum results to return.
            threshold: Minimum similarity score (0-1). Results below threshold are dropped.
                       Implemented as post-query filter (PgvectorAdapter.search() has no threshold param).

        Returns:
            List of matching records as dicts with: conversation_id, score, system_id, content, timestamp, metadata.
        """
        if self._adapter is None:
            raise RuntimeError("PgvectorHotStore not initialized. Call initialize() first.")

        # Build filter expression for system_id if provided
        filter_expr: dict[str, Any] | None = None
        if system_id:
            filter_expr = {"system_id": system_id}

        results = await self._adapter.search(
            collection=_COLLECTION_NAME,
            query_vector=query_embedding,
            limit=limit,
            filter_expr=filter_expr,
            include_vectors=False,
        )

        # Apply threshold filter post-query
        if threshold is not None:
            # score is distance; lower distance = higher similarity
            # PgvectorAdapter returns distance (cosine distance), not similarity score
            # We filter where distance <= (1 - threshold) for cosine distance
            max_distance = 1.0 - threshold
            results = [r for r in results if r.score <= max_distance]

        return [
            {
                "conversation_id": r.id,
                "score": r.score,
                "system_id": r.metadata.get("system_id"),
                "content": r.metadata.get("content"),
                "timestamp": r.metadata.get("timestamp"),
                "metadata": {
                    k: v
                    for k, v in r.metadata.items()
                    if k not in ("system_id", "content", "timestamp")
                },
            }
            for r in results
        ]

    async def get_by_id(self, conversation_id: str) -> dict[str, Any] | None:
        """Retrieve a single conversation by ID.

        Args:
            conversation_id: The conversation ID to look up.

        Returns:
            Record dict or None if not found.
        """
        if self._adapter is None:
            raise RuntimeError("PgvectorHotStore not initialized. Call initialize() first.")

        docs = await self._adapter.get(_COLLECTION_NAME, [conversation_id], include_vectors=False)
        if not docs:
            return None

        doc = docs[0]
        return {
            "conversation_id": doc.id,
            "system_id": doc.metadata.get("system_id"),
            "content": doc.metadata.get("content"),
            "timestamp": doc.metadata.get("timestamp"),
            "metadata": {
                k: v
                for k, v in doc.metadata.items()
                if k not in ("system_id", "content", "timestamp")
            },
        }

    async def delete(self, conversation_id: str) -> None:
        """Delete a conversation by ID.

        Args:
            conversation_id: The conversation ID to delete.
        """
        if self._adapter is None:
            raise RuntimeError("PgvectorHotStore not initialized. Call initialize() first.")

        await self._adapter.delete(_COLLECTION_NAME, [conversation_id])

    async def close(self) -> None:
        """Close the pgvector adapter connection."""
        if self._adapter is not None:
            await self._adapter.cleanup()
            self._adapter = None
            logger.info("PgvectorHotStore closed")
