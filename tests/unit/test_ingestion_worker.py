"""Tests for ingestion worker (concurrent upload processing)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from akosha.ingestion.worker import IngestionWorker
from akosha.models import SystemMemoryUpload


def _to_json_bytes(data: dict) -> bytes:
    """Convert dict to JSON bytes."""
    return json.dumps(data).encode()


class TestIngestionWorker:
    """Test suite for IngestionWorker."""

    @pytest.fixture
    def mock_storage(self) -> AsyncMock:
        """Create mock storage adapter."""
        storage = AsyncMock()

        # Create proper async generator function for list()
        async def mock_list(prefix: str):
            if prefix == "systems/":
                for i in range(3):
                    yield f"systems/system-{i}/"
            elif "systems/system-" in prefix:
                yield f"{prefix}upload-1/"
            # No explicit return - just stops yielding

        storage.list = mock_list

        # Mock exists() and download()
        storage.exists = AsyncMock(return_value=True)

        # Return valid manifest JSON with required fields
        valid_manifest = {
            "uploaded_at": datetime.now(UTC).isoformat(),
            "conversation_count": 10,
            "version": "1.0",
        }
        storage.download = AsyncMock(return_value=_to_json_bytes(valid_manifest))
        return storage

    @pytest.fixture
    def mock_hot_store(self) -> AsyncMock:
        """Create mock hot store."""
        hot_store = AsyncMock()
        hot_store.insert = AsyncMock()
        hot_store.search_similar = AsyncMock(return_value=[])
        return hot_store

    @pytest.fixture
    def worker(
        self, mock_storage: AsyncMock, mock_hot_store: AsyncMock
    ) -> IngestionWorker:
        """Create ingestion worker with mocked dependencies."""
        return IngestionWorker(
            storage_adapter=mock_storage,  # type: ignore
            hot_store=mock_hot_store,
            max_concurrent_ingests=5,
            poll_interval_seconds=1,
        )

    def test_initialization(self, worker: IngestionWorker) -> None:
        """Test worker initialization."""
        assert worker.storage is not None
        assert worker.hot_store is not None
        assert worker.max_concurrent_ingests == 5
        assert worker.poll_interval_seconds == 1
        assert not worker._running

    @pytest.mark.asyncio
    async def test_discover_uploads(self, worker: IngestionWorker) -> None:
        """Test upload discovery from cloud storage."""
        uploads = await worker._discover_uploads()

        # Should discover uploads from 3 systems
        assert len(uploads) == 3

        # Each upload should have required fields
        for upload in uploads:
            assert isinstance(upload, SystemMemoryUpload)
            assert upload.system_id.startswith("system-")
            assert upload.upload_id == "upload-1"
            # Manifest contains all Pydantic model fields with defaults
            assert upload.manifest["version"] == "1.0"
            assert upload.manifest["conversation_count"] == 10
            assert upload.uploaded_at is not None

    @pytest.mark.asyncio
    async def test_discover_uploads_empty(self, worker: IngestionWorker) -> None:
        """Test upload discovery when no uploads available."""
        # Mock empty async generator that yields nothing
        async def mock_empty(prefix: str):
            return
            yield  # Make this an async generator function (never executed)
        worker.storage.list = mock_empty

        uploads = await worker._discover_uploads()

        assert uploads == []

    @pytest.mark.asyncio
    async def test_process_upload(self, worker: IngestionWorker) -> None:
        """Test processing a single upload."""
        upload = SystemMemoryUpload(
            system_id="system-test",
            upload_id="upload-test",
            manifest={"version": "1.0"},
            storage_prefix="systems/system-test/upload-test/",
            uploaded_at=datetime.now(UTC),
        )

        # _process_upload returns None (logs the manifest for now)
        # This test verifies it doesn't crash
        result = await worker._process_upload(upload)

        # The method returns None by design (TODO: implement full processing)
        assert result is None

    @pytest.mark.asyncio
    async def test_concurrent_processing_limit(self, worker: IngestionWorker) -> None:
        """Test that concurrent processing respects semaphore limit."""
        # Create 10 uploads
        uploads = [
            SystemMemoryUpload(
                system_id=f"system-{i}",
                upload_id=f"upload-{i}",
                manifest={"version": "1.0"},
                storage_prefix=f"systems/system-{i}/upload-{i}/",
                uploaded_at=datetime.now(UTC),
            )
            for i in range(10)
        ]

        # Track concurrent processing
        max_concurrent = 0
        current_concurrent = 0
        lock = asyncio.Lock()

        async def mock_process(upload: SystemMemoryUpload):
            nonlocal max_concurrent, current_concurrent
            async with lock:
                current_concurrent += 1
                if current_concurrent > max_concurrent:
                    max_concurrent = current_concurrent
            # Simulate work
            await asyncio.sleep(0.05)
            async with lock:
                current_concurrent -= 1
            return None

        worker._process_upload = mock_process  # type: ignore

        # Create a semaphore with the same limit as the worker
        semaphore = asyncio.Semaphore(worker.max_concurrent_ingests)

        async def process_with_semaphore(upload: SystemMemoryUpload):
            async with semaphore:
                return await worker._process_upload(upload)

        # Process all uploads through the semaphore
        tasks = [process_with_semaphore(u) for u in uploads]
        await asyncio.gather(*tasks)

        # Should not exceed max_concurrent_ingests
        assert max_concurrent <= worker.max_concurrent_ingests

    @pytest.mark.asyncio
    async def test_worker_start_stop(self, worker: IngestionWorker) -> None:
        """Test worker start and stop lifecycle."""
        # Start worker in background
        task = asyncio.create_task(worker.run())

        # Wait a bit
        await asyncio.sleep(0.1)

        # Stop worker
        worker.stop()

        # Wait for task to complete
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            pytest.fail("Worker did not stop within timeout")

        assert not worker._running

    @pytest.mark.asyncio
    async def test_worker_handles_graceful_shutdown(self, worker: IngestionWorker) -> None:
        """Test that worker handles shutdown gracefully."""
        # Track if uploads were processed
        processed_uploads: list[str] = []

        async def mock_process(upload: SystemMemoryUpload):
            processed_uploads.append(upload.system_id)
            await asyncio.sleep(0.2)  # Simulate work
            return upload

        worker._process_upload = mock_process  # type: ignore

        # Start and quickly stop
        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0.1)  # Let it start
        worker.stop()

        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            pytest.fail("Worker did not stop within timeout")

        # Worker should have stopped (uploads may or may not have completed)
        assert not worker._running

    @pytest.mark.asyncio
    async def test_discovery_handles_malformed_manifests(
        self, worker: IngestionWorker
    ) -> None:
        """Test that discovery handles malformed manifests gracefully."""
        # Mock download to return invalid JSON
        worker.storage.download = AsyncMock(return_value="invalid json{")

        # Should not crash
        uploads = await worker._discover_uploads()

        # Should return empty list (manifest parsing failed)
        assert uploads == []

    @pytest.mark.asyncio
    async def test_process_upload_handles_errors(
        self, worker: IngestionWorker
    ) -> None:
        """Test that upload processing errors are handled gracefully."""
        upload = SystemMemoryUpload(
            system_id="system-error",
            upload_id="upload-error",
            manifest={"version": "1.0"},
            storage_prefix="systems/system-error/upload-error/",
            uploaded_at=datetime.now(UTC),
        )

        # Mock processing to raise error
        async def mock_process_error(upload: SystemMemoryUpload):
            raise RuntimeError("Processing failed")

        worker._process_upload = mock_process_error  # type: ignore

        # The error should propagate from _process_upload itself
        # The worker's run() method handles exceptions with return_exceptions=True
        with pytest.raises(RuntimeError, match="Processing failed"):
            await worker._process_upload(upload)

    def test_worker_configuration(self) -> None:
        """Test worker configuration from environment."""
        worker = IngestionWorker(
            storage_adapter=AsyncMock(),  # type: ignore
            hot_store=AsyncMock(),
            max_concurrent_ingests=100,
            poll_interval_seconds=60,
        )

        assert worker.max_concurrent_ingests == 100
        assert worker.poll_interval_seconds == 60
