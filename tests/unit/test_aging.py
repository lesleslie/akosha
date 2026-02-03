"""Tests for tier aging service (hot->warm->cold migration)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from akosha.models import WarmRecord
from akosha.storage.aging import AgingService


class TestAgingService:
    """Test suite for AgingService."""

    @pytest.fixture
    def mock_hot_store(self) -> MagicMock:
        """Create mock hot store with proper DuckDB connection mocking.

        The aging service uses conn.execute() directly for queries,
        so we need to mock the connection properly.
        """
        # Create mock connection
        mock_conn = MagicMock()

        # Create test data in dict format (as returned by SQL queries)
        old_records_rows = [
            (
                f"system-{i}",  # system_id
                f"conv-{i}",  # conversation_id
                f"Content {i}",  # content
                [0.1] * 384,  # embedding
                datetime.now(UTC) - timedelta(days=10),  # timestamp (10 days old)
                {"age": "old"},  # metadata
                f"hash-{i}",  # content_hash
            )
            for i in range(5)
        ]

        # Mock execute to return a result with fetchall()
        mock_result = MagicMock()
        mock_result.fetchall.return_value = old_records_rows
        mock_conn.execute.return_value = mock_result

        # Create mock hot store with conn attribute
        hot_store = MagicMock()
        hot_store.conn = mock_conn
        hot_store.delete = AsyncMock()  # Used by _delete_from_hot_store
        hot_store.conn.executemany = MagicMock()  # Used by _delete_batch_from_hot_store

        return hot_store

    @pytest.fixture
    def mock_warm_store(self) -> AsyncMock:
        """Create mock warm store with batch insert support."""
        warm_store = AsyncMock()
        warm_store.insert = AsyncMock()
        warm_store.insert_batch = AsyncMock()  # Used by batch migration
        warm_store.get = AsyncMock(return_value=None)  # No duplicate
        warm_store.conn = None  # Warm store may not have conn initialized
        return warm_store

    @pytest.fixture
    def aging_service(
        self, mock_hot_store: MagicMock, mock_warm_store: AsyncMock
    ) -> AgingService:
        """Create aging service with mocked dependencies."""
        return AgingService(
            hot_store=mock_hot_store,
            warm_store=mock_warm_store,
        )

    def test_initialization(
        self, aging_service: AgingService
    ) -> None:
        """Test aging service initialization."""
        assert aging_service.hot_store is not None
        assert aging_service.warm_store is not None

    @pytest.mark.asyncio
    async def test_migrate_hot_to_warm_success(
        self, aging_service: AgingService
    ) -> None:
        """Test successful hot->warm migration."""
        stats = await aging_service.migrate_hot_to_warm(cutoff_days=7)

        # Should complete successfully
        assert stats is not None
        assert stats.records_migrated == 5
        assert stats.errors == 0
        assert stats.start_time is not None
        assert stats.end_time is not None

    @pytest.mark.asyncio
    async def test_migrate_compresses_embeddings(
        self, aging_service: AgingService
    ) -> None:
        """Test that embeddings are compressed during migration."""
        await aging_service.migrate_hot_to_warm(cutoff_days=7)

        # Verify warm_store.insert_batch was called (batch migration)
        assert aging_service.warm_store.insert_batch.call_count == 1

        # Check that embeddings were compressed (FLOAT -> INT8)
        call = aging_service.warm_store.insert_batch.call_args_list[0]
        warm_records = call[0][0]  # First positional argument (list of WarmRecord)
        assert len(warm_records) == 5

        for warm_record in warm_records:
            assert isinstance(warm_record, WarmRecord)
            # INT8 embeddings should be list of ints
            assert all(isinstance(x, int) for x in warm_record.embedding)

    @pytest.mark.asyncio
    async def test_migrate_deletes_from_hot(
        self, aging_service: AgingService
    ) -> None:
        """Test that records are deleted from hot store after migration."""
        await aging_service.migrate_hot_to_warm(cutoff_days=7)

        # Verify hot_store.conn.executemany was called for batch delete
        assert aging_service.hot_store.conn.executemany.call_count == 1

    @pytest.mark.asyncio
    async def test_migrate_generates_summaries(
        self, aging_service: AgingService
    ) -> None:
        """Test that summaries are generated during migration."""
        await aging_service.migrate_hot_to_warm(cutoff_days=7)

        # Check that summaries were generated
        call = aging_service.warm_store.insert_batch.call_args_list[0]
        warm_records = call[0][0]

        for warm_record in warm_records:
            assert isinstance(warm_record, WarmRecord)
            # Summary should be generated (placeholder: "Content N")
            assert warm_record.summary is not None
            assert len(warm_record.summary) > 0

    @pytest.mark.asyncio
    async def test_migrate_with_cutoff_days(
        self, aging_service: AgingService
    ) -> None:
        """Test migration with different cutoff days."""
        # Test with 30-day cutoff (our test records are 10 days old)
        stats = await aging_service.migrate_hot_to_warm(cutoff_days=30)

        assert stats is not None
        # Records are 10 days old and cutoff is 30 days, so they migrate
        # (SQL query: timestamp < cutoff_date, where cutoff_date = now - 30 days)
        # 10 days ago < 30 days ago is True, so records should migrate
        assert stats.records_migrated == 5

    @pytest.mark.asyncio
    async def test_migrate_handles_empty_hot_store(
        self, aging_service: AgingService
    ) -> None:
        """Test migration when hot store is empty."""
        # Mock no old records by returning empty result
        mock_result = MagicMock()
        mock_result.fetchall.return_value = []
        aging_service.hot_store.conn.execute.return_value = mock_result

        stats = await aging_service.migrate_hot_to_warm(cutoff_days=7)

        assert stats.records_migrated == 0
        assert stats.errors == 0

    @pytest.mark.asyncio
    async def test_migrate_handles_errors_gracefully(
        self, aging_service: AgingService
    ) -> None:
        """Test that migration errors are handled gracefully."""
        # Mock warm store to raise error on insert_batch
        aging_service.warm_store.insert_batch = AsyncMock(
            side_effect=RuntimeError("Insert failed")
        )

        stats = await aging_service.migrate_hot_to_warm(cutoff_days=7)

        # Should complete with errors
        assert stats.errors > 0

    @pytest.mark.asyncio
    async def test_migrate_stats_tracking(
        self, aging_service: AgingService
    ) -> None:
        """Test that migration stats are tracked correctly."""
        stats = await aging_service.migrate_hot_to_warm(cutoff_days=7)

        assert stats.records_migrated == 5
        assert stats.start_time is not None
        assert stats.end_time is not None
        assert stats.end_time > stats.start_time

    @pytest.mark.asyncio
    async def test_migrate_converts_timestamps(
        self, aging_service: AgingService
    ) -> None:
        """Test that timestamps are preserved during migration."""
        await aging_service.migrate_hot_to_warm(cutoff_days=7)

        call = aging_service.warm_store.insert_batch.call_args_list[0]
        warm_records = call[0][0]

        for warm_record in warm_records:
            assert isinstance(warm_record, WarmRecord)
            # Timestamp should be preserved
            assert warm_record.timestamp is not None
            assert isinstance(warm_record.timestamp, datetime)

    @pytest.mark.asyncio
    async def test_migrate_preserves_system_ids(
        self, aging_service: AgingService
    ) -> None:
        """Test that system IDs are preserved during migration."""
        await aging_service.migrate_hot_to_warm(cutoff_days=7)

        call = aging_service.warm_store.insert_batch.call_args_list[0]
        warm_records = call[0][0]
        system_ids = [r.system_id for r in warm_records]

        # Should have all system IDs
        assert len(system_ids) == 5
        assert "system-0" in system_ids
        assert "system-4" in system_ids

    @pytest.mark.asyncio
    async def test_migrate_preserves_conversation_ids(
        self, aging_service: AgingService
    ) -> None:
        """Test that conversation IDs are preserved during migration."""
        await aging_service.migrate_hot_to_warm(cutoff_days=7)

        call = aging_service.warm_store.insert_batch.call_args_list[0]
        warm_records = call[0][0]
        conv_ids = [r.conversation_id for r in warm_records]

        # Should have all conversation IDs
        assert len(conv_ids) == 5
        assert "conv-0" in conv_ids
        assert "conv-4" in conv_ids
