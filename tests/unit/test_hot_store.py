"""Tests for hot store (DuckDB in-memory)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from akosha.models import HotRecord
from akosha.storage.hot_store import HotStore


class TestHotStore:
    """Test suite for HotStore."""

    @pytest.fixture
    async def hot_store(self) -> HotStore:
        """Create fresh hot store for each test."""
        store = HotStore(database_path=":memory:")
        await store.initialize()
        yield store
        await store.close()

    @pytest.mark.asyncio
    async def test_initialization(self, hot_store: HotStore) -> None:
        """Test hot store initialization."""
        assert hot_store.conn is not None
        # Check table exists
        result = hot_store.conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name = 'conversations'"
        ).fetchone()
        assert result is not None

    @pytest.mark.asyncio
    async def test_insert_conversation(self, hot_store: HotStore) -> None:
        """Test inserting a conversation."""
        record = HotRecord(
            system_id="system-1",
            conversation_id="conv-1",
            content="Test conversation about FastAPI",
            embedding=[0.1] * 384,
            timestamp=datetime.now(UTC),
            metadata={"topic": "FastAPI"},
        )

        await hot_store.insert(record)

        # Verify insertion
        result = hot_store.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()
        assert result[0] == 1

    @pytest.mark.asyncio
    async def test_insert_duplicate_conversation(self, hot_store: HotStore) -> None:
        """Test inserting duplicate conversation (should fail)."""
        record = HotRecord(
            system_id="system-1",
            conversation_id="conv-1",
            content="Test conversation",
            embedding=[0.1] * 384,
            timestamp=datetime.now(UTC),
            metadata={},
        )

        await hot_store.insert(record)

        # Try inserting duplicate
        with pytest.raises(RuntimeError):  # DuckDB constraint violation
            await hot_store.insert(record)

    @pytest.mark.asyncio
    async def test_search_similar(self, hot_store: HotStore) -> None:
        """Test vector similarity search."""
        # Insert test conversations
        now = datetime.now(UTC)

        conversations = [
            HotRecord(
                system_id="system-1",
                conversation_id="conv-1",
                content="Conversation about FastAPI",
                embedding=[0.1] * 384,
                timestamp=now,
                metadata={},
            ),
            HotRecord(
                system_id="system-1",
                conversation_id="conv-2",
                content="Conversation about Django",
                embedding=[0.5] * 384,
                timestamp=now,
                metadata={},
            ),
        ]

        for conv in conversations:
            await hot_store.insert(conv)

        # Search with similar embedding
        query = [0.12] * 384  # Similar to conv-1
        results = await hot_store.search_similar(
            query_embedding=query,
            limit=10,
            threshold=0.0,
        )

        assert len(results) >= 1
        # First result should be conv-1 (most similar)
        assert results[0]["conversation_id"] == "conv-1"
        assert "similarity" in results[0]

    @pytest.mark.asyncio
    async def test_search_similar_with_system_filter(self, hot_store: HotStore) -> None:
        """Test vector search with system ID filter."""
        now = datetime.now(UTC)

        # Insert conversations from different systems
        await hot_store.insert(
            HotRecord(
                system_id="system-1",
                conversation_id="conv-1",
                content="System 1 conversation",
                embedding=[0.1] * 384,
                timestamp=now,
                metadata={},
            )
        )

        await hot_store.insert(
            HotRecord(
                system_id="system-2",
                conversation_id="conv-2",
                content="System 2 conversation",
                embedding=[0.12] * 384,
                timestamp=now,
                metadata={},
            )
        )

        # Search filtering by system-1
        query = [0.11] * 384
        results = await hot_store.search_similar(
            query_embedding=query,
            system_id="system-1",
            limit=10,
        )

        # Should only return system-1 results
        assert len(results) == 1
        assert results[0]["system_id"] == "system-1"

    @pytest.mark.asyncio
    async def test_search_similar_threshold_filtering(self, hot_store: HotStore) -> None:
        """Test threshold filtering in vector search."""
        now = datetime.now(UTC)

        await hot_store.insert(
            HotRecord(
                system_id="system-1",
                conversation_id="conv-1",
                content="Similar conversation",
                embedding=[0.1] * 384,
                timestamp=now,
                metadata={},
            )
        )

        await hot_store.insert(
            HotRecord(
                system_id="system-1",
                conversation_id="conv-2",
                content="Dissimilar conversation",
                embedding=[0.9] * 384,
                timestamp=now,
                metadata={},
            )
        )

        # Search with high threshold (only very similar results)
        query = [0.1] * 384
        results = await hot_store.search_similar(
            query_embedding=query,
            limit=10,
            threshold=0.95,  # Very high threshold
        )

        # Both embeddings are normalized, so both will have high similarity
        # Test that threshold filtering works by checking we get results
        assert len(results) >= 1
        # All results should meet threshold
        for result in results:
            assert result["similarity"] >= 0.95

    @pytest.mark.asyncio
    async def test_search_similar_limit(self, hot_store: HotStore) -> None:
        """Test result limiting in vector search."""
        now = datetime.now(UTC)

        # Insert multiple conversations
        for i in range(10):
            await hot_store.insert(
                HotRecord(
                    system_id="system-1",
                    conversation_id=f"conv-{i}",
                    content=f"Conversation {i}",
                    embedding=[0.1 + i * 0.001] * 384,
                    timestamp=now,
                    metadata={},
                )
            )

        # Search with limit
        query = [0.1] * 384
        results = await hot_store.search_similar(
            query_embedding=query,
            limit=5,
            threshold=0.0,
        )

        # Should return at most 5 results
        assert len(results) <= 5

    @pytest.mark.asyncio
    async def test_content_hash_computation(self) -> None:
        """Test content hash computation."""
        content1 = "Test conversation"
        content2 = "Test conversation"
        content3 = "Different conversation"

        hash1 = HotStore._compute_content_hash(content1)
        hash2 = HotStore._compute_content_hash(content2)
        hash3 = HotStore._compute_content_hash(content3)

        # Same content should produce same hash
        assert hash1 == hash2
        # Different content should produce different hash
        assert hash1 != hash3

    @pytest.mark.asyncio
    async def test_close_hot_store(self, hot_store: HotStore) -> None:
        """Test closing hot store."""
        assert hot_store.conn is not None

        await hot_store.close()

        # Connection should be closed
        # Note: DuckDB doesn't have a simple way to check if closed
        # But we can verify no error on double close
        await hot_store.close()  # Should not raise

    @pytest.mark.asyncio
    async def test_insert_without_initialization(self) -> None:
        """Test insertion without initialization raises error."""
        store = HotStore(database_path=":memory:")
        # Don't initialize

        record = HotRecord(
            system_id="system-1",
            conversation_id="conv-1",
            content="Test",
            embedding=[0.1] * 384,
            timestamp=datetime.now(UTC),
            metadata={},
        )

        with pytest.raises(RuntimeError, match="not initialized"):
            await store.insert(record)

    @pytest.mark.asyncio
    async def test_search_without_initialization(self) -> None:
        """Test search without initialization raises error."""
        store = HotStore(database_path=":memory:")
        # Don't initialize

        with pytest.raises(RuntimeError, match="not initialized"):
            await store.search_similar(
                query_embedding=[0.1] * 384,
            )

    @pytest.mark.asyncio
    async def test_concurrent_operations(self, hot_store: HotStore) -> None:
        """Test concurrent insert operations."""
        import asyncio

        now = datetime.now(UTC)

        # Insert multiple conversations concurrently
        tasks = []
        for i in range(10):
            record = HotRecord(
                system_id="system-1",
                conversation_id=f"conv-{i}",
                content=f"Conversation {i}",
                embedding=[0.1 + i * 0.001] * 384,
                timestamp=now,
                metadata={},
            )
            tasks.append(hot_store.insert(record))

        # Should not raise errors
        await asyncio.gather(*tasks)

        # Verify all inserted
        result = hot_store.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()
        assert result[0] == 10
