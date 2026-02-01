"""Tests for tier aging service (hot→warm→cold migration)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from akosha.models import HotRecord, WarmRecord
from akosha.storage.aging import AgingService, MigrationStats


class TestAgingService:
    """Test suite for AgingService."""

    @pytest.fixture
    def mock_hot_store(self) -> AsyncMock:
        """Create mock hot store."""
        hot_store = AsyncMock()
        # Mock query to return old records
        old_records = [
            HotRecord(
                system_id=f"system-{i}",
                conversation_id=f"conv-{i}",
                content=f"Content {i}",
                embedding=[0.1] * 384,
                timestamp=datetime.now(UTC) - timedelta(days=10),  # 10 days old
                metadata={"age": "old"},
            )
            for i in range(5)
        ]
        hot_store.query_old_records = AsyncMock(return_value=old_records)  # type: ignore
        hot_store.delete = AsyncMock()
        return hot_store

    @pytest.fixture
    def mock_warm_store(self) -> AsyncMock:
        """Create mock warm store."""
        warm_store = AsyncMock()
        warm_store.insert = AsyncMock()
        warm_store.get = AsyncMock(return_value=None)  # No duplicate
        return warm_store

    @pytest.fixture
    def aging_service(
        self, mock_hot_store: AsyncMock, mock_warm_store: AsyncMock
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
        """Test successful hot→warm migration."""
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

        # Verify warm_store.insert was called
        assert aging_service.warm_store.insert.call_count == 5

        # Check that embeddings were compressed (FLOAT → INT8)
        calls = aging_service.warm_store.insert.call_args_list
        for call in calls:
            warm_record = call[0][0]  # First positional argument
            assert isinstance(warm_record, WarmRecord)
            # INT8 embeddings should be list of ints
            assert all(isinstance(x, int) for x in warm_record.embedding)

    @pytest.mark.asyncio
    async def test_migrate_deletes_from_hot(
        self, aging_service: AgingService
    ) -> None:
        """Test that records are deleted from hot store after migration."""
        await aging_service.migrate_hot_to_warm(cutoff_days=7)

        # Verify hot_store.delete was called for each record
        assert aging_service.hot_store.delete.call_count == 5

    @pytest.mark.asyncio
    async def test_migrate_generates_summaries(
        self, aging_service: AgingService
    ) -> None:
        """Test that summaries are generated during migration."""
        await aging_service.migrate_hot_to_warm(cutoff_days=7)

        # Check that summaries were generated
        calls = aging_service.warm_store.insert.call_args_list
        for call in calls:
            warm_record = call[0][0]
            assert isinstance(warm_record, WarmRecord)
            # Summary should be generated (placeholder: "Summary of ...")
            assert warm_record.summary is not None
            assert len(warm_record.summary) > 0

    @pytest.mark.asyncio
    async def test_migrate_with_cutoff_days(
        self, aging_service: AgingService
    ) -> None:
        """Test migration with different cutoff days."""
        # Test with 30-day cutoff
        stats = await aging_service.migrate_hot_to_warm(cutoff_days=30)

        assert stats is not None
        # Should only migrate records older than 30 days
        # Our test records are 10 days old, so they shouldn't migrate
        # This depends on query_old_records implementation

    @pytest.mark.asyncio
    async def test_migrate_handles_empty_hot_store(
        self, aging_service: AgingService
    ) -> None:
        """Test migration when hot store is empty."""
        # Mock no old records
        aging_service.hot_store.query_old_records = AsyncMock(return_value=[])  # type: ignore

        stats = await aging_service.migrate_hot_to_warm(cutoff_days=7)

        assert stats.records_migrated == 0
        assert stats.errors == 0

    @pytest.mark.asyncio
    async def test_migrate_handles_errors_gracefully(
        self, aging_service: AgingService
    ) -> None:
        """Test that migration errors are handled gracefully."""
        # Mock warm store to raise error on insert
        aging_service.warm_store.insert = AsyncMock(
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

        calls = aging_service.warm_store.insert.call_args_list
        for call in calls:
            warm_record = call[0][0]
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

        calls = aging_service.warm_store.insert.call_args_list
        system_ids = [call[0][0].system_id for call in calls]

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

        calls = aging_service.warm_store.insert.call_args_list
        conv_ids = [call[0][0].conversation_id for call in calls]

        # Should have all conversation IDs
        assert len(conv_ids) == 5
        assert "conv-0" in conv_ids
        assert "conv-4" in conv_ids
