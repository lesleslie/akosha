"""Tests for cold store (Parquet export to S3/R2)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path

import pytest

from akosha.models import ColdRecord
from akosha.storage.cold_store import ColdStore


class TestColdStore:
    """Test suite for ColdStore."""

    @pytest.fixture
    def mock_storage(self) -> AsyncMock:
        """Create mock storage adapter."""
        storage = AsyncMock()
        storage.upload = AsyncMock(return_value="s3://key/path")
        return storage

    @pytest.fixture
    def cold_store(self, mock_storage: AsyncMock) -> ColdStore:
        """Create cold store with mocked storage."""
        return ColdStore(
            bucket="test-bucket",
            prefix="conversations/",
            storage=mock_storage,  # type: ignore
        )

    @pytest.fixture
    def sample_records(self) -> list[ColdRecord]:
        """Create sample cold records."""
        return [
            ColdRecord(
                system_id=f"system-{i}",
                conversation_id=f"conv-{i}",
                fingerprint=f"fp-{i}",
                ultra_summary=f"Summary {i}",
                timestamp=datetime.now(UTC),
                daily_metrics={"count": i},
            )
            for i in range(10)
        ]

    def test_initialization(self, cold_store: ColdStore) -> None:
        """Test cold store initialization."""
        assert cold_store.bucket == "test-bucket"
        assert cold_store.prefix == "conversations/"

    @pytest.mark.asyncio
    async def test_export_batch_success(
        self, cold_store: ColdStore, sample_records: list[ColdRecord]
    ) -> None:
        """Test successful Parquet export."""
        object_key = await cold_store.export_batch(
            records=sample_records,
            partition_path="system-001/2025/01/31",
        )

        # Should return S3 object key
        assert object_key is not None
        assert "conversations/" in object_key
        assert "system-001/2025/01/31" in object_key

    @pytest.mark.asyncio
    async def test_export_batch_creates_parquet_file(
        self, cold_store: ColdStore, sample_records: list[ColdRecord]
    ) -> None:
        """Test that Parquet file is created."""
        await cold_store.export_batch(
            records=sample_records,
            partition_path="test/partition",
        )

        # Verify temporary Parquet file was created and cleaned up
        # (The implementation should create, upload, then delete temp file)

    @pytest.mark.asyncio
    async def test_export_batch_uploads_to_storage(
        self, cold_store: ColdStore, sample_records: list[ColdRecord]
    ) -> None:
        """Test that data is uploaded to storage."""
        partition_path = "system-001/2025/01/31"
        await cold_store.export_batch(
            records=sample_records,
            partition_path=partition_path,
        )

        # Verify storage.upload was called
        cold_store.storage.upload.assert_called_once()
        call_args = cold_store.storage.upload.call_args
        assert call_args is not None

    @pytest.mark.asyncio
    async def test_export_batch_handles_empty_records(
        self, cold_store: ColdStore
    ) -> None:
        """Test exporting empty record list."""
        object_key = await cold_store.export_batch(
            records=[],
            partition_path="test/path",
        )

        # Should still succeed
        assert object_key is not None

    @pytest.mark.asyncio
    async def test_export_batch_preserves_data(
        self, cold_store: ColdStore, sample_records: list[ColdRecord]
    ) -> None:
        """Test that data is preserved during export."""
        partition_path = "system-001/2025/01/31"
        await cold_store.export_batch(
            records=sample_records,
            partition_path=partition_path,
        )

        # The Parquet file should contain all records
        # (This would require reading back the Parquet file to verify)

    @pytest.mark.asyncio
    async def test_export_batch_partition_path(
        self, cold_store: ColdStore, sample_records: list[ColdRecord]
    ) -> None:
        """Test that partition path is used correctly."""
        partition_path = "system-alpha/2025/02/01"
        object_key = await cold_store.export_batch(
            records=sample_records,
            partition_path=partition_path,
        )

        assert partition_path in object_key

    @pytest.mark.asyncio
    async def test_export_batch_handles_special_characters(
        self, cold_store: ColdStore
    ) -> None:
        """Test handling of special characters in data."""
        records_with_special_chars = [
            ColdRecord(
                system_id="system-with-dashes",
                conversation_id="conv/with/slashes",
                fingerprint="fp:with:colons",
                ultra_summary="Summary with 'quotes' and \"double quotes\"",
                timestamp=datetime.now(UTC),
                daily_metrics={"key": "value with spaces"},
            )
        ]

        # Should not crash
        object_key = await cold_store.export_batch(
            records=records_with_special_chars,
            partition_path="test/path",
        )
        assert object_key is not None

    @pytest.mark.asyncio
    async def test_export_batch_timestamp_format(
        self, cold_store: ColdStore, sample_records: list[ColdRecord]
    ) -> None:
        """Test that timestamps are preserved correctly."""
        await cold_store.export_batch(
            records=sample_records,
            partition_path="test/path",
        )

        # Timestamps should be serialized to Parquet correctly
        # (Would require reading Parquet file to verify)

    @pytest.mark.asyncio
    async def test_export_batch_metadata_format(
        self, cold_store: ColdStore, sample_records: list[ColdRecord]
    ) -> None:
        """Test that metadata is serialized correctly."""
        await cold_store.export_batch(
            records=sample_records,
            partition_path="test/path",
        )

        # daily_metrics (dict) should be serialized to JSON string
        # (Would require reading Parquet file to verify)

    @pytest.mark.asyncio
    async def test_export_large_batch(
        self, cold_store: ColdStore
    ) -> None:
        """Test exporting a large batch of records."""
        # Create 1000 records
        large_batch = [
            ColdRecord(
                system_id=f"system-{i//100}",
                conversation_id=f"conv-{i}",
                fingerprint=f"fp-{i}",
                ultra_summary=f"Summary {i}",
                timestamp=datetime.now(UTC),
                daily_metrics={"index": i},
            )
            for i in range(1000)
        ]

        object_key = await cold_store.export_batch(
            records=large_batch,
            partition_path="large/batch",
        )

        assert object_key is not None

    @pytest.mark.asyncio
    async def test_export_batch_fingerprint_bytes(
        self, cold_store: ColdStore
    ) -> None:
        """Test that fingerprints (binary) are handled correctly."""
        records_with_binary_fp = [
            ColdRecord(
                system_id="system-1",
                conversation_id="conv-1",
                fingerprint=b"\x00\x01\x02\x03",  # Binary fingerprint
                ultra_summary="Summary",
                timestamp=datetime.now(UTC),
                daily_metrics={},
            )
        ]

        object_key = await cold_store.export_batch(
            records=records_with_binary_fp,
            partition_path="test/path",
        )

        assert object_key is not None
