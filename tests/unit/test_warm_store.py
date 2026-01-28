"""Tests for warm store (DuckDB on-disk)."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from akasha.models import WarmRecord
from akasha.storage.warm_store import WarmStore


class TestWarmStore:
    """Test suite for WarmStore."""

    @pytest.fixture
    async def warm_store(self) -> WarmStore:
        """Create fresh warm store for each test."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "warm.db"
            store = WarmStore(database_path=db_path)
            await store.initialize()
            yield store
            await store.close()

    @pytest.mark.asyncio
    async def test_initialization(self, warm_store: WarmStore) -> None:
        """Test warm store initialization."""
        assert warm_store.conn is not None
        assert warm_store.db_path.exists()

        # Check table exists
        result = warm_store.conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name = 'conversations'"
        ).fetchone()
        assert result is not None

    @pytest.mark.asyncio
    async def test_initialization_creates_directory(self) -> None:
        """Test that initialization creates parent directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create path in non-existent subdirectory
            db_path = Path(tmpdir) / "subdir" / "warm.db"
            assert not db_path.parent.exists()

            store = WarmStore(database_path=db_path)
            await store.initialize()

            # Directory should be created
            assert db_path.parent.exists()
            assert db_path.exists()

            await store.close()

    @pytest.mark.asyncio
    async def test_insert_conversation(self, warm_store: WarmStore) -> None:
        """Test inserting a conversation."""
        record = WarmRecord(
            system_id="system-1",
            conversation_id="conv-1",
            embedding=[1] * 384,  # INT8 quantized
            summary="FastAPI conversation summary",
            timestamp=datetime.now(UTC),
            metadata={"topic": "FastAPI"},
        )

        await warm_store.insert(record)

        # Verify insertion
        result = warm_store.conn.execute(
            "SELECT COUNT(*) FROM conversations"
        ).fetchone()
        assert result[0] == 1

    @pytest.mark.asyncio
    async def test_insert_duplicate_conversation(self, warm_store: WarmStore) -> None:
        """Test inserting duplicate conversation (should fail)."""
        record = WarmRecord(
            system_id="system-1",
            conversation_id="conv-1",
            embedding=[1] * 384,
            summary="Test summary",
            timestamp=datetime.now(UTC),
            metadata={},
        )

        await warm_store.insert(record)

        # Try inserting duplicate
        with pytest.raises(Exception):  # DuckDB constraint violation
            await warm_store.insert(record)

    @pytest.mark.asyncio
    async def test_insert_multiple_conversations(self, warm_store: WarmStore) -> None:
        """Test inserting multiple conversations."""
        now = datetime.now(UTC)

        for i in range(10):
            record = WarmRecord(
                system_id=f"system-{i % 3}",
                conversation_id=f"conv-{i}",
                embedding=[i] * 384,
                summary=f"Summary {i}",
                timestamp=now,
                metadata={"index": i},
            )
            await warm_store.insert(record)

        # Verify all inserted
        result = warm_store.conn.execute(
            "SELECT COUNT(*) FROM conversations"
        ).fetchone()
        assert result[0] == 10

    @pytest.mark.asyncio
    async def test_date_partition_index(self, warm_store: WarmStore) -> None:
        """Test that date partition index is created and functional."""
        # Verify index works by querying with date range
        now = datetime.now(UTC)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=999999)

        await warm_store.insert(WarmRecord(
            system_id="system-1",
            conversation_id="conv-1",
            embedding=[1] * 384,
            summary="Test",
            timestamp=now,
            metadata={},
        ))

        # Query using date range (index should optimize this)
        result = warm_store.conn.execute("""
            SELECT COUNT(*)
            FROM conversations
            WHERE timestamp >= ? AND timestamp <= ?
        """, [start_of_day, end_of_day]).fetchone()

        assert result[0] == 1

    @pytest.mark.asyncio
    async def test_query_by_date_range(self, warm_store: WarmStore) -> None:
        """Test querying conversations by date range."""
        now = datetime.now(UTC)

        # Insert conversations with different timestamps
        await warm_store.insert(WarmRecord(
            system_id="system-1",
            conversation_id="conv-1",
            embedding=[1] * 384,
            summary="Old conversation",
            timestamp=now.replace(hour=0),  # Earlier today
            metadata={},
        ))

        await warm_store.insert(WarmRecord(
            system_id="system-1",
            conversation_id="conv-2",
            embedding=[2] * 384,
            summary="Recent conversation",
            timestamp=now,  # Now
            metadata={},
        ))

        # Query today's conversations
        result = warm_store.conn.execute("""
            SELECT COUNT(*)
            FROM conversations
            WHERE timestamp >= ? AND timestamp <= ?
        """, [now.replace(hour=0, minute=0, second=0), now]).fetchone()

        assert result[0] == 2

    @pytest.mark.asyncio
    async def test_query_by_system(self, warm_store: WarmStore) -> None:
        """Test querying conversations by system."""
        now = datetime.now(UTC)

        # Insert conversations from different systems
        await warm_store.insert(WarmRecord(
            system_id="system-1",
            conversation_id="conv-1",
            embedding=[1] * 384,
            summary="System 1",
            timestamp=now,
            metadata={},
        ))

        await warm_store.insert(WarmRecord(
            system_id="system-2",
            conversation_id="conv-2",
            embedding=[2] * 384,
            summary="System 2",
            timestamp=now,
            metadata={},
        ))

        # Query system-1 conversations
        result = warm_store.conn.execute("""
            SELECT COUNT(*)
            FROM conversations
            WHERE system_id = ?
        """, ["system-1"]).fetchone()

        assert result[0] == 1

    @pytest.mark.asyncio
    async def test_metadata_storage(self, warm_store: WarmStore) -> None:
        """Test that metadata is stored correctly as JSON."""
        now = datetime.now(UTC)

        metadata = {
            "topic": "FastAPI",
            "tags": ["python", "web"],
            "count": 42,
        }

        await warm_store.insert(WarmRecord(
            system_id="system-1",
            conversation_id="conv-1",
            embedding=[1] * 384,
            summary="Test",
            timestamp=now,
            metadata=metadata,
        ))

        # Retrieve and verify metadata
        result = warm_store.conn.execute("""
            SELECT metadata
            FROM conversations
            WHERE conversation_id = ?
        """, ["conv-1"]).fetchone()

        assert result is not None
        # DuckDB stores JSON as strings
        import json
        retrieved_metadata = json.loads(result[0])
        assert retrieved_metadata == metadata

    @pytest.mark.asyncio
    async def test_close_warm_store(self, warm_store: WarmStore) -> None:
        """Test closing warm store."""
        assert warm_store.conn is not None

        await warm_store.close()

        # Double close should not raise
        await warm_store.close()

    @pytest.mark.asyncio
    async def test_insert_without_initialization(self) -> None:
        """Test insertion without initialization raises error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WarmStore(database_path=Path(tmpdir) / "warm.db")
            # Don't initialize

            record = WarmRecord(
                system_id="system-1",
                conversation_id="conv-1",
                embedding=[1] * 384,
                summary="Test",
                timestamp=datetime.now(UTC),
                metadata={},
            )

            with pytest.raises(RuntimeError, match="not initialized"):
                await store.insert(record)

    @pytest.mark.asyncio
    async def test_concurrent_operations(self, warm_store: WarmStore) -> None:
        """Test concurrent insert operations."""
        import asyncio

        now = datetime.now(UTC)

        # Insert multiple conversations concurrently
        tasks = []
        for i in range(10):
            record = WarmRecord(
                system_id="system-1",
                conversation_id=f"conv-{i}",
                embedding=[i] * 384,
                summary=f"Summary {i}",
                timestamp=now,
                metadata={"index": i},
            )
            tasks.append(warm_store.insert(record))

        # Should not raise errors
        await asyncio.gather(*tasks)

        # Verify all inserted
        result = warm_store.conn.execute(
            "SELECT COUNT(*) FROM conversations"
        ).fetchone()
        assert result[0] == 10

    @pytest.mark.asyncio
    async def test_embedding_quantization(self, warm_store: WarmStore) -> None:
        """Test that embeddings are stored as INT8."""
        now = datetime.now(UTC)

        embedding = [100, -50, 0, 127, -128] + [0] * 379  # INT8 range

        await warm_store.insert(WarmRecord(
            system_id="system-1",
            conversation_id="conv-1",
            embedding=embedding,
            summary="Test",
            timestamp=now,
            metadata={},
        ))

        # Retrieve embedding
        result = warm_store.conn.execute("""
            SELECT embedding
            FROM conversations
            WHERE conversation_id = ?
        """, ["conv-1"]).fetchone()

        assert result is not None
        # Verify it's stored as array
        retrieved_embedding = result[0]
        assert len(retrieved_embedding) == 384
        assert retrieved_embedding[0] == 100
        assert retrieved_embedding[1] == -50
