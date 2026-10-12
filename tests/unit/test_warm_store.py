"""Tests for warm store (DuckDB on-disk)."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid7

import pytest
from oneiric.core.ids import is_uuid7

from akosha.models import WarmRecord
from akosha.storage.warm_store import WarmStore


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
            "SELECT table_name FROM information_schema.tables WHERE table_name = 'conversations'"
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
        result = warm_store.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()
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
        with pytest.raises(Exception):  # DuckDB constraint violation (ConstraintException)
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
        result = warm_store.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()
        assert result[0] == 10

    @pytest.mark.asyncio
    async def test_date_partition_index(self, warm_store: WarmStore) -> None:
        """Test that date partition index is created and functional."""
        # Verify index works by querying with date range
        now = datetime.now(UTC)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=999999)

        await warm_store.insert(
            WarmRecord(
                system_id="system-1",
                conversation_id="conv-1",
                embedding=[1] * 384,
                summary="Test",
                timestamp=now,
                metadata={},
            )
        )

        # Query using date range (index should optimize this)
        result = warm_store.conn.execute(
            """
            SELECT COUNT(*)
            FROM conversations
            WHERE timestamp >= ? AND timestamp <= ?
        """,
            [start_of_day, end_of_day],
        ).fetchone()

        assert result[0] == 1

    @pytest.mark.asyncio
    async def test_query_by_date_range(self, warm_store: WarmStore) -> None:
        """Test querying conversations by date range."""
        now = datetime.now(UTC)

        # Insert conversations with different timestamps
        await warm_store.insert(
            WarmRecord(
                system_id="system-1",
                conversation_id="conv-1",
                embedding=[1] * 384,
                summary="Old conversation",
                timestamp=now.replace(hour=0),  # Earlier today
                metadata={},
            )
        )

        await warm_store.insert(
            WarmRecord(
                system_id="system-1",
                conversation_id="conv-2",
                embedding=[2] * 384,
                summary="Recent conversation",
                timestamp=now,  # Now
                metadata={},
            )
        )

        # Query today's conversations
        result = warm_store.conn.execute(
            """
            SELECT COUNT(*)
            FROM conversations
            WHERE timestamp >= ? AND timestamp <= ?
        """,
            [now.replace(hour=0, minute=0, second=0), now],
        ).fetchone()

        assert result[0] == 2

    @pytest.mark.asyncio
    async def test_query_by_system(self, warm_store: WarmStore) -> None:
        """Test querying conversations by system."""
        now = datetime.now(UTC)

        # Insert conversations from different systems
        await warm_store.insert(
            WarmRecord(
                system_id="system-1",
                conversation_id="conv-1",
                embedding=[1] * 384,
                summary="System 1",
                timestamp=now,
                metadata={},
            )
        )

        await warm_store.insert(
            WarmRecord(
                system_id="system-2",
                conversation_id="conv-2",
                embedding=[2] * 384,
                summary="System 2",
                timestamp=now,
                metadata={},
            )
        )

        # Query system-1 conversations
        result = warm_store.conn.execute(
            """
            SELECT COUNT(*)
            FROM conversations
            WHERE system_id = ?
        """,
            ["system-1"],
        ).fetchone()

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

        await warm_store.insert(
            WarmRecord(
                system_id="system-1",
                conversation_id="conv-1",
                embedding=[1] * 384,
                summary="Test",
                timestamp=now,
                metadata=metadata,
            )
        )

        # Retrieve and verify metadata
        result = warm_store.conn.execute(
            """
            SELECT metadata
            FROM conversations
            WHERE conversation_id = ?
        """,
            ["conv-1"],
        ).fetchone()

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
        result = warm_store.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()
        assert result[0] == 10

    @pytest.mark.asyncio
    async def test_embedding_quantization(self, warm_store: WarmStore) -> None:
        """Test that embeddings are stored as INT8."""
        now = datetime.now(UTC)

        embedding = [100, -50, 0, 127, -128] + [0] * 379  # INT8 range

        await warm_store.insert(
            WarmRecord(
                system_id="system-1",
                conversation_id="conv-1",
                embedding=embedding,
                summary="Test",
                timestamp=now,
                metadata={},
            )
        )

        # Retrieve embedding
        result = warm_store.conn.execute(
            """
            SELECT embedding
            FROM conversations
            WHERE conversation_id = ?
        """,
            ["conv-1"],
        ).fetchone()

        assert result is not None
        # Verify it's stored as array
        retrieved_embedding = result[0]
        assert len(retrieved_embedding) == 384
        assert retrieved_embedding[0] == 100
        assert retrieved_embedding[1] == -50

    @pytest.mark.asyncio
    async def test_insert_batch_inserts_multiple_records(self, warm_store: WarmStore) -> None:
        """Test batch insertion writes all records in one call."""
        now = datetime.now(UTC)
        records = [
            WarmRecord(
                system_id="system-1",
                conversation_id="conv-batch-1",
                embedding=[1] * 384,
                summary="Batch 1",
                timestamp=now,
                metadata={"batch": 1},
            ),
            WarmRecord(
                system_id="system-2",
                conversation_id="conv-batch-2",
                embedding=[2] * 384,
                summary="Batch 2",
                timestamp=now,
                metadata={"batch": 2},
            ),
        ]

        await warm_store.insert_batch(records)

        result = warm_store.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()
        assert result[0] == 2

    @pytest.mark.asyncio
    async def test_insert_batch_empty_is_noop(self, warm_store: WarmStore) -> None:
        """Empty batch inserts should return early without touching the table."""
        await warm_store.insert_batch([])

        result = warm_store.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()
        assert result[0] == 0

    @pytest.mark.asyncio
    async def test_insert_batch_without_initialization_raises(self) -> None:
        """Batch insertion should fail fast when the store was never initialized."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WarmStore(database_path=Path(tmpdir) / "warm.db")

            with pytest.raises(RuntimeError, match="not initialized"):
                await store.insert_batch([])

    @pytest.mark.asyncio
    async def test_close_without_connection_is_noop(self) -> None:
        """Closing an uninitialized store should be a no-op."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WarmStore(database_path=Path(tmpdir) / "warm.db")

            await store.close()


class TestWarmStoreUuid7DualWrite:
    """Lazy dual-write pattern for the Phase 4 UUID7 migration.

    New rows: ``uuid7_id`` is populated from the conversation_id
    shape (UUID7 -> same value, slug -> NULL, anything else ->
    fresh UUID7). Legacy rows: a pre-migration row with NULL
    ``uuid7_id`` is backfilled on first read.
    """

    @pytest.fixture
    async def warm_store(self) -> WarmStore:
        """Create fresh warm store for each test."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "warm.db"
            store = WarmStore(database_path=db_path)
            await store.initialize()
            yield store
            await store.close()

    @staticmethod
    def _record(conv_id: str) -> WarmRecord:
        return WarmRecord(
            system_id="system-1",
            conversation_id=conv_id,
            embedding=[1] * 384,
            summary="uuid7 dual-write test",
            timestamp=datetime.now(UTC),
            metadata={},
        )

    @pytest.mark.asyncio
    async def test_insert_uuid7_conversation_id_dual_populates(
        self,
        warm_store: WarmStore,
    ) -> None:
        """A new row whose conversation_id is a UUID7 hex has the same
        value in both columns (no fresh UUID7 generated, no backfill)."""
        conv_id = uuid7().hex
        await warm_store.insert(self._record(conv_id))

        row = warm_store.conn.execute(
            "SELECT conversation_id, uuid7_id FROM conversations WHERE conversation_id = ?",
            [conv_id],
        ).fetchone()
        assert row is not None
        assert row[0] == conv_id
        assert row[1] == conv_id
        assert is_uuid7(row[1])
        assert UUID(row[1]).version == 7

    @pytest.mark.asyncio
    async def test_insert_slug_conversation_id_leaves_uuid7_id_null(
        self,
        warm_store: WarmStore,
    ) -> None:
        """Slug-shaped ids (system:foo, project:bar) keep their shape
        and do not get a parallel uuid7_id value."""
        await warm_store.insert(self._record("system:foo"))
        await warm_store.insert(self._record("project:bar"))

        rows = warm_store.conn.execute(
            "SELECT conversation_id, uuid7_id FROM conversations ORDER BY conversation_id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0][0] == "project:bar"
        assert rows[0][1] is None
        assert rows[1][0] == "system:foo"
        assert rows[1][1] is None

    @pytest.mark.asyncio
    async def test_insert_legacy_ulid_synthesizes_fresh_uuid7(
        self,
        warm_store: WarmStore,
    ) -> None:
        """A non-slug, non-UUID7 conversation_id (e.g. a legacy ULID)
        triggers generation of a fresh uuid7_id sibling column."""
        ulid = "01ARZ3NDEKTS6PQRYF0AB1C2D3E"  # 26-char Crockford ULID
        await warm_store.insert(self._record(ulid))

        row = warm_store.conn.execute(
            "SELECT conversation_id, uuid7_id FROM conversations WHERE conversation_id = ?",
            [ulid],
        ).fetchone()
        assert row is not None
        assert row[0] == ulid
        assert row[1] is not None
        assert is_uuid7(row[1])
        assert row[1] != ulid  # different value, fresh synthesis

    @pytest.mark.asyncio
    async def test_insert_batch_populates_uuid7_id_for_each_row(
        self,
        warm_store: WarmStore,
    ) -> None:
        """Batch insert also derives uuid7_id for every row."""
        records = [
            self._record(uuid7().hex),
            self._record("system:batch-test"),
            self._record("01HZAB3NDEKTS6PQRYF0AB1C2D"),
        ]
        await warm_store.insert_batch(records)

        rows = warm_store.conn.execute(
            "SELECT conversation_id, uuid7_id FROM conversations ORDER BY conversation_id"
        ).fetchall()
        by_id = {row[0]: row[1] for row in rows}
        assert len(by_id) == 3
        # UUID7 conversation_id -> same uuid7_id
        assert by_id[records[0].conversation_id] == records[0].conversation_id
        # Slug -> NULL
        assert by_id["system:batch-test"] is None
        # Legacy ULID -> fresh UUID7
        ulid = "01HZAB3NDEKTS6PQRYF0AB1C2D"
        assert is_uuid7(by_id[ulid])
        assert by_id[ulid] != ulid

    @pytest.mark.asyncio
    async def test_get_by_conversation_id_backfills_legacy_row(
        self,
        warm_store: WarmStore,
    ) -> None:
        """Reading a legacy row whose uuid7_id is NULL synthesizes one
        and persists it; a second read returns the cached value without
        rewriting.

        Simulates a pre-migration warm.db: insert via direct SQL with
        a ULID conversation_id and NULL uuid7_id, then exercise
        :meth:`get_by_conversation_id` and verify the lazy backfill
        plus the second-read no-op."""
        legacy_ulid = "01HZA2NDEKTS6PQRYF0AB1C2D3E"
        warm_store.conn.execute(
            """
            INSERT INTO conversations
                (system_id, conversation_id, uuid7_id, embedding,
                 summary, timestamp, metadata, uploaded_at)
            VALUES (?, ?, NULL, ?, ?, ?, ?, ?)
            """,
            [
                "system-1",
                legacy_ulid,
                [1] * 384,
                "legacy row pre-migration",
                datetime.now(UTC),
                {},
                datetime.now(UTC),
            ],
        )

        # First read: lazy backfill should populate uuid7_id.
        first = await warm_store.get_by_conversation_id(legacy_ulid)
        assert first is not None
        assert first["conversation_id"] == legacy_ulid
        assert first["uuid7_id"] is not None
        assert is_uuid7(first["uuid7_id"])
        assert first["uuid7_id"] != legacy_ulid

        # Second read: same uuid7_id, no re-write.
        second = await warm_store.get_by_conversation_id(legacy_ulid)
        assert second is not None
        assert second["uuid7_id"] == first["uuid7_id"]

    @pytest.mark.asyncio
    async def test_get_by_conversation_id_skips_backfill_for_slug(
        self,
        warm_store: WarmStore,
    ) -> None:
        """A slug-shaped conversation_id never gets a uuid7_id, even
        after a read."""
        await warm_store.insert(self._record("system:slag-test"))
        result = await warm_store.get_by_conversation_id("system:slag-test")
        assert result is not None
        assert result["uuid7_id"] is None

    @pytest.mark.asyncio
    async def test_get_by_conversation_id_returns_none_for_missing(
        self,
        warm_store: WarmStore,
    ) -> None:
        """A read for an unknown conversation_id returns None and does
        not write a row."""
        result = await warm_store.get_by_conversation_id("does-not-exist")
        assert result is None
        count = warm_store.conn.execute(
            "SELECT COUNT(*) FROM conversations"
        ).fetchone()
        assert count[0] == 0
