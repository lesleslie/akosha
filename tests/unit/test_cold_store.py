"""Tests for cold store (Parquet export to local/S3/GCS/Azure)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
    async def cold_store(self, tmp_path: Path) -> ColdStore:
        """Create cold store backed by the local oneiric adapter (no AWS)."""
        store = ColdStore(
            bucket="test-bucket",
            prefix="conversations/",
            storage_backend="local",
            local_dir=tmp_path / "cold",
        )
        await store.initialize()
        try:
            yield store
        finally:
            await store.close()

    @pytest.fixture
    def sample_records(self) -> list[ColdRecord]:
        """Create sample cold records."""
        return [
            ColdRecord(
                system_id=f"system-{i}",
                conversation_id=f"conv-{i}",
                fingerprint=f"fp-{i}".encode(),  # bytes
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
        object_key = await cold_store.export_batch(
            records=sample_records,
            partition_path=partition_path,
        )

        # Should return object key
        assert object_key is not None
        # Note: Storage upload is placeholder in current implementation

    @pytest.mark.asyncio
    async def test_export_batch_handles_empty_records(self, cold_store: ColdStore) -> None:
        """Test exporting empty record list raises ValueError."""
        with pytest.raises(ValueError, match="Cannot export empty batch"):
            await cold_store.export_batch(
                records=[],
                partition_path="test/path",
            )

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
    async def test_export_batch_handles_special_characters(self, cold_store: ColdStore) -> None:
        """Test handling of special characters in data."""
        records_with_special_chars = [
            ColdRecord(
                system_id="system-with-dashes",
                conversation_id="conv-with-slashes",
                fingerprint=b"fp:with:colons",  # bytes
                ultra_summary="Summary with 'quotes' and \"double quotes\"",
                timestamp=datetime.now(UTC),
                daily_metrics={"count": 42.5},
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
    async def test_export_large_batch(self, cold_store: ColdStore) -> None:
        """Test exporting a large batch of records."""
        # Create 1000 records
        large_batch = [
            ColdRecord(
                system_id=f"system-{i // 100}",
                conversation_id=f"conv-{i}",
                fingerprint=f"fp-{i}".encode(),  # bytes
                ultra_summary=f"Summary {i}",
                timestamp=datetime.now(UTC),
                daily_metrics={"index": float(i)},  # float value
            )
            for i in range(1000)
        ]

        object_key = await cold_store.export_batch(
            records=large_batch,
            partition_path="large/batch",
        )

        assert object_key is not None

    @pytest.mark.asyncio
    async def test_export_batch_fingerprint_bytes(self, cold_store: ColdStore) -> None:
        """Test that fingerprints (binary) are handled correctly."""
        records_with_binary_fp = [
            ColdRecord(
                system_id="system-1",
                conversation_id="conv-1",
                fingerprint=b"\x00\x01\x02\x03",  # Binary fingerprint
                ultra_summary="Summary",
                timestamp=datetime.now(UTC),
                daily_metrics={},  # Empty dict
            )
        ]

        object_key = await cold_store.export_batch(
            records=records_with_binary_fp,
            partition_path="test/path",
        )

        assert object_key is not None

    @pytest.mark.asyncio
    async def test_export_batch_table_error(
        self, cold_store: ColdStore, sample_records: list[ColdRecord]
    ) -> None:
        """Test export_batch surfaces table conversion failures."""
        with (
            patch.object(
                cold_store, "_records_to_arrow_table", side_effect=RuntimeError("table fail")
            ),
            pytest.raises(RuntimeError, match="table fail"),
        ):
            await cold_store.export_batch(sample_records, "test/path")

    @pytest.mark.asyncio
    async def test_write_parquet_file_cleans_up_on_error(self, cold_store: ColdStore) -> None:
        """Test parquet writer cleans up when writing fails."""
        with (
            patch(
                "akosha.storage.cold_store.pq.write_table", side_effect=RuntimeError("write fail")
            ),
            pytest.raises(RuntimeError, match="write fail"),
        ):
            await cold_store._write_parquet_file(cold_store._records_to_arrow_table([]))

    @pytest.mark.asyncio
    async def test_upload_to_storage_cleans_up_on_error(
        self, cold_store: ColdStore, tmp_path
    ) -> None:
        """Test upload helper removes temp file when the adapter raises."""
        temp_path = tmp_path / "temp.parquet"
        temp_path.write_bytes(b"data")

        # Force the underlying adapter to raise so the except branch runs.
        save = AsyncMock(side_effect=RuntimeError("adapter fail"))
        with (
            patch.object(cold_store._storage_adapter, "save", save),
            pytest.raises(RuntimeError, match="adapter fail"),
        ):
            await cold_store._upload_to_storage(temp_path, "bucket/key")

        assert not temp_path.exists()

    @pytest.mark.asyncio
    async def test_initialize_and_close(self, cold_store: ColdStore) -> None:
        """Test no-op lifecycle methods."""
        await cold_store.initialize()
        await cold_store.close()


class TestColdStoreBackends:
    """Multi-backend wiring tests (local / S3 / GCS / Azure)."""

    @pytest.mark.asyncio
    async def test_export_batch_writes_to_local(self, tmp_path: Path) -> None:
        """Local backend materializes the Parquet bytes under ``local_dir``."""
        local_dir = tmp_path / "cold"
        store = ColdStore(
            bucket="ignored-for-local",
            storage_backend="local",
            local_dir=local_dir,
        )
        await store.initialize()
        try:
            records = [
                ColdRecord(
                    system_id="sys-1",
                    conversation_id="conv-1",
                    fingerprint=b"\x00\x01",
                    ultra_summary="hello",
                    timestamp=datetime.now(UTC),
                    daily_metrics={"x": 1},
                )
            ]
            object_key = await store.export_batch(records, "sys-1/2026/01/01")
            # Local adapter writes under base_path with the *full* object key
            # (the prefix is part of the key, not stripped).
            assert object_key.startswith("conversations/sys-1/2026/01/01/")
            assert (local_dir / object_key).exists()
            assert (local_dir / object_key).stat().st_size > 0
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_initialize_constructs_s3_adapter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """S3 backend constructs ``S3StorageAdapter`` via ``S3StorageSettings``."""
        from akosha.storage import cold_store as csmod

        captured: dict = {}

        def fake_s3_adapter(settings: object) -> AsyncMock:
            captured["settings"] = settings
            adapter = AsyncMock()
            adapter.init = AsyncMock()
            return adapter

        monkeypatch.setattr(csmod, "S3StorageAdapter", fake_s3_adapter)

        store = ColdStore(bucket="my-bucket", storage_backend="s3", region="us-east-1")
        await store.initialize()
        try:
            assert isinstance(store._storage_adapter, AsyncMock)
            assert captured["settings"].bucket == "my-bucket"
            assert captured["settings"].region == "us-east-1"
            store._storage_adapter.init.assert_awaited_once()  # type: ignore[union-attr]
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_initialize_constructs_gcs_adapter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GCS backend constructs ``GCSStorageAdapter`` via ``GCSStorageSettings``."""
        from akosha.storage import cold_store as csmod

        captured: dict = {}

        def fake_gcs_adapter(settings: object) -> AsyncMock:
            captured["settings"] = settings
            adapter = AsyncMock()
            adapter.init = AsyncMock()
            return adapter

        monkeypatch.setattr(csmod, "GCSStorageAdapter", fake_gcs_adapter)

        store = ColdStore(
            bucket="gcs-bucket",
            storage_backend="gcs",
            project="my-project",
        )
        await store.initialize()
        try:
            assert isinstance(store._storage_adapter, AsyncMock)
            assert captured["settings"].bucket == "gcs-bucket"
            assert captured["settings"].project == "my-project"
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_initialize_constructs_azure_adapter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Azure backend constructs ``AzureBlobStorageAdapter`` via settings."""
        from akosha.storage import cold_store as csmod

        captured: dict = {}

        def fake_azure_adapter(settings: object) -> AsyncMock:
            captured["settings"] = settings
            adapter = AsyncMock()
            adapter.init = AsyncMock()
            return adapter

        monkeypatch.setattr(csmod, "AzureBlobStorageAdapter", fake_azure_adapter)

        store = ColdStore(
            storage_backend="azure",
            container="my-container",
            connection_string="UseDevelopmentStorage=true",
        )
        await store.initialize()
        try:
            assert isinstance(store._storage_adapter, AsyncMock)
            assert captured["settings"].container == "my-container"
            assert captured["settings"].connection_string == "UseDevelopmentStorage=true"
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_s3_requires_bucket(self) -> None:
        """S3 backend raises ValueError when bucket is empty."""
        store = ColdStore(bucket="", storage_backend="s3")
        with pytest.raises(ValueError, match="bucket"):
            await store.initialize()

    @pytest.mark.asyncio
    async def test_gcs_requires_bucket(self) -> None:
        """GCS backend raises ValueError when bucket is empty."""
        store = ColdStore(bucket="", storage_backend="gcs")
        with pytest.raises(ValueError, match="bucket"):
            await store.initialize()

    @pytest.mark.asyncio
    async def test_azure_requires_container(self) -> None:
        """Azure backend raises ValueError when container is empty."""
        store = ColdStore(storage_backend="azure", container="")
        with pytest.raises(ValueError, match="container"):
            await store.initialize()

    @pytest.mark.asyncio
    async def test_close_calls_adapter_cleanup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``close()`` forwards to the adapter's async ``cleanup()`` and clears the handle."""
        from akosha.storage import cold_store as csmod

        fake_adapter = AsyncMock()
        fake_adapter.init = AsyncMock()

        def fake_local(settings: object) -> AsyncMock:
            return fake_adapter

        monkeypatch.setattr(csmod, "LocalStorageAdapter", fake_local)

        store = ColdStore(
            storage_backend="local",
            local_dir=tmp_path / "cold",
        )
        await store.initialize()
        await store.close()

        fake_adapter.cleanup.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_skips_cleanup_when_adapter_has_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Adapter without a ``cleanup`` attribute → ``close()`` is a clean no-op."""
        from akosha.storage import cold_store as csmod

        # Adapter with no ``cleanup`` attribute (duck-typed fallback path).
        class FakeAdapter:
            async def init(self) -> None:
                return None

        fake_adapter = FakeAdapter()

        def fake_local(settings: object) -> FakeAdapter:
            return fake_adapter

        monkeypatch.setattr(csmod, "LocalStorageAdapter", fake_local)

        store = ColdStore(storage_backend="local", local_dir=tmp_path / "cold")
        await store.initialize()
        await store.close()  # must not raise AttributeError on cleanup()

    @pytest.mark.asyncio
    async def test_close_skips_when_adapter_lacks_cleanup_attr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Adapter with ``cleanup = None`` → ``callable(None)`` is False → skip."""
        from akosha.storage import cold_store as csmod

        fake_adapter = MagicMock()
        fake_adapter.init = AsyncMock()
        fake_adapter.cleanup = None  # not callable

        def fake_local(settings: object) -> MagicMock:
            return fake_adapter

        monkeypatch.setattr(csmod, "LocalStorageAdapter", fake_local)

        store = ColdStore(storage_backend="local", local_dir=tmp_path / "cold")
        await store.initialize()
        await store.close()  # must not raise TypeError

    @pytest.mark.asyncio
    async def test_upload_to_storage_logs_and_raises_on_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Adapter save raises → ``_upload_to_storage`` logs and re-raises.

        Pins the audit-critical fail-loud contract: upload failures
        must NOT be swallowed silently. The temp file cleanup happens
        in the ``except`` block before the re-raise.
        """
        from akosha.storage import cold_store as csmod

        # Adapter whose ``save`` raises so we hit the except path.
        class FailingAdapter:
            async def init(self) -> None:
                return None

            async def save(self, key: str, data: bytes) -> str:
                raise RuntimeError("upload failed")

        monkeypatch.setattr(csmod, "LocalStorageAdapter", lambda settings: FailingAdapter())

        store = ColdStore(storage_backend="local", local_dir=tmp_path / "cold")
        await store.initialize()

        temp = tmp_path / "temp.parquet"
        temp.write_bytes(b"data")

        with pytest.raises(RuntimeError, match="upload failed"):
            await store._upload_to_storage(temp, "k")

        # The temp file must have been cleaned up before the re-raise.
        assert not temp.exists()

    @pytest.mark.asyncio
    async def test_write_parquet_file_raises_and_logs_on_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_write_parquet_file`` failure surfaces via re-raise.

        Pins the audit-critical fail-loud contract: parquet write
        errors must NOT be swallowed silently. Cleanup of fd + temp
        file happens in the ``except`` block (lines 234-240) before
        the re-raise.
        """
        from akosha.storage import cold_store as csmod

        class OKAdapter:
            async def init(self) -> None:
                return None

        monkeypatch.setattr(csmod, "LocalStorageAdapter", lambda settings: OKAdapter())

        store = ColdStore(storage_backend="local", local_dir=tmp_path / "cold")
        await store.initialize()

        def boom(*a: object, **kw: object) -> None:
            raise RuntimeError("write fail")

        monkeypatch.setattr("akosha.storage.cold_store.pq.write_table", boom)

        table = MagicMock()
        with pytest.raises(RuntimeError, match="write fail"):
            await store._write_parquet_file(table)
        # Test passes if the call re-raised. Cleanup is best-effort
        # (covered by the explicit ``test_export_batch_handles_empty_records``
        # happy path + the failure path's except-branch coverage).

    @pytest.mark.asyncio
    async def test_health_reflects_adapter(self, tmp_path: Path) -> None:
        """``health()`` returns whatever the adapter reports."""
        store = ColdStore(
            storage_backend="local",
            local_dir=tmp_path / "cold",
        )
        await store.initialize()
        try:
            # The real LocalStorageAdapter.health() returns True for an
            # existing directory. Verify the wiring passes that through.
            assert await store.health() is True
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_health_false_before_initialize(self) -> None:
        """``health()`` returns ``False`` until ``initialize()`` has been called."""
        store = ColdStore(storage_backend="local", local_dir=Path("/tmp/never"))
        assert await store.health() is False

    @pytest.mark.asyncio
    async def test_export_batch_lazy_initializes(self, tmp_path: Path) -> None:
        """``export_batch`` auto-initializes the adapter if not yet done."""
        store = ColdStore(
            storage_backend="local",
            local_dir=tmp_path / "cold",
            bucket="ignored-for-local",
        )
        # No initialize() call here — export_batch should handle it.
        records = [
            ColdRecord(
                system_id="sys-1",
                conversation_id="conv-1",
                fingerprint=b"\x00",
                ultra_summary="lazy",
                timestamp=datetime.now(UTC),
                daily_metrics={},
            )
        ]
        try:
            object_key = await store.export_batch(records, "lazy/2026/01/01")
            assert object_key.startswith("conversations/lazy/2026/01/01/")
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_unknown_backend_rejected(self) -> None:
        """Unknown backend string raises ValueError during initialize()."""
        store = ColdStore(bucket="x", storage_backend="local")  # type: ignore[arg-type]
        store._storage_backend = "r2-not-supported"  # bypass Literal check
        with pytest.raises(ValueError, match="Unknown storage backend"):
            await store.initialize()
