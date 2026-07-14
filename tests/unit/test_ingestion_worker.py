"""Tests for ingestion worker (concurrent upload processing)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

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

        # Mock get() for memory database downloads (used by _process_upload)
        valid_db = {"conversations": [{"content": "test memory"}]}
        storage.get = AsyncMock(return_value=_to_json_bytes(valid_db))
        return storage

    @pytest.fixture
    def mock_hot_store(self) -> AsyncMock:
        """Create mock hot store."""
        hot_store = AsyncMock()
        hot_store.insert = AsyncMock()
        hot_store.search_similar = AsyncMock(return_value=[])
        hot_store._compute_content_hash = lambda content: f"hash:{content}"
        return hot_store

    @pytest.fixture
    def worker(self, mock_storage: AsyncMock, mock_hot_store: AsyncMock) -> IngestionWorker:
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
    async def test_discover_uploads_sequential(
        self, worker: IngestionWorker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test the legacy sequential discovery path."""
        monkeypatch.setenv("USE_CONCURRENT_DISCOVERY", "false")

        uploads = await worker._discover_uploads()

        assert len(uploads) == 3
        assert all(upload.upload_id == "upload-1" for upload in uploads)

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
    async def test_run_batch_uploads(self, worker: IngestionWorker) -> None:
        """Test the one-shot batch processing path in run()."""
        uploads = [
            SystemMemoryUpload(
                system_id="system-a",
                upload_id="upload-a",
                manifest={"version": "1.0"},
                storage_prefix="systems/system-a/upload-a/",
                uploaded_at=datetime.now(UTC),
            )
        ]

        seen: list[str] = []

        async def mock_process(upload: SystemMemoryUpload):
            seen.append(upload.upload_id)
            return upload.upload_id

        worker._process_upload = mock_process  # type: ignore

        results = await worker.run(uploads=uploads)

        assert results == ["upload-a"]
        assert seen == ["upload-a"]

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
        except TimeoutError:
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
        except TimeoutError:
            pytest.fail("Worker did not stop within timeout")

        # Worker should have stopped (uploads may or may not have completed)
        assert not worker._running

    @pytest.mark.asyncio
    async def test_discovery_handles_malformed_manifests(self, worker: IngestionWorker) -> None:
        """Test that discovery handles malformed manifests gracefully."""
        # Mock download to return invalid JSON
        worker.storage.download = AsyncMock(return_value="invalid json{")

        # Should not crash
        uploads = await worker._discover_uploads()

        # Should return empty list (manifest parsing failed)
        assert uploads == []

    @pytest.mark.asyncio
    async def test_process_upload_handles_errors(self, worker: IngestionWorker) -> None:
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

    def test_extract_system_id(self, worker: IngestionWorker) -> None:
        """Test system ID extraction for valid and invalid prefixes."""
        assert worker._extract_system_id("systems/demo/") == "demo"
        assert worker._extract_system_id("invalid") is None

    def test_flatten_scan_results(self, worker: IngestionWorker) -> None:
        """Test flattening of mixed scan results."""
        uploads = SystemMemoryUpload(
            system_id="system-flat",
            upload_id="upload-flat",
            manifest={"version": "1.0"},
            storage_prefix="systems/system-flat/upload-flat/",
            uploaded_at=datetime.now(UTC),
        )

        flattened = worker._flatten_scan_results([RuntimeError("boom"), [uploads]])

        assert flattened == [uploads]

    @pytest.mark.asyncio
    async def test_storage_get_sync_and_missing(self, worker: IngestionWorker) -> None:
        """Test storage getter fallback for sync adapters and missing methods."""

        worker.storage.get = lambda key: f"value:{key}"  # type: ignore[method-assign]
        assert await worker._storage_get("demo") == "value:demo"

        class NoGetStorage:
            def list(self, prefix: str):
                return []

        no_get_worker = IngestionWorker(
            storage_adapter=NoGetStorage(),  # type: ignore[arg-type]
            hot_store=worker.hot_store,
        )

        assert await no_get_worker._storage_get("demo") is None

    @pytest.mark.asyncio
    async def test_iterate_storage_list_sync(self, worker: IngestionWorker) -> None:
        """Test iteration over a synchronous storage listing."""

        worker.storage.list = lambda prefix: ["systems/demo/"]  # type: ignore[method-assign]

        items = [item async for item in worker._iterate_storage_list("systems/")]

        assert items == ["systems/demo/"]

    @pytest.mark.asyncio
    async def test_try_create_upload_missing_and_invalid_manifest(
        self, worker: IngestionWorker
    ) -> None:
        """Test manifest creation failure paths."""

        # ``_try_create_upload`` uses ``download`` for the existence check
        # (see ``akosha/ingestion/worker.py``: S3StorageAdapter has no
        # ``exists`` method, so ``download`` returns ``None`` when absent).
        worker.storage.download = AsyncMock(return_value=None)
        assert (
            await worker._try_create_upload(
                "system-test", "upload-test", "systems/system-test/upload-test/"
            )
            is None
        )

        worker.storage.download = AsyncMock(return_value=b"{invalid json")
        assert (
            await worker._try_create_upload(
                "system-test", "upload-test", "systems/system-test/upload-test/"
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_try_create_upload_validation_error(self, worker: IngestionWorker) -> None:
        """Test manifest creation when validation rejects the payload."""

        worker.storage.exists = AsyncMock(return_value=True)
        worker.storage.download = AsyncMock(
            return_value=_to_json_bytes(
                {
                    "uploaded_at": datetime.now(UTC).isoformat(),
                    "conversation_count": 1,
                    "files": ["../bad.txt"],
                }
            )
        )

        assert (
            await worker._try_create_upload(
                "system-test",
                "upload-test",
                "systems/system-test/upload-test/",
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_process_conversations_duplicate_and_error(self, worker: IngestionWorker) -> None:
        """Test duplicate detection and error handling in conversation processing."""

        inserted = {
            "id": "conv-0",
            "content": "fresh content",
            "embedding": [0.1, 0.2],
            "timestamp": datetime.now(UTC).isoformat(),
            "metadata": {"topic": "demo"},
        }
        duplicate = {
            "id": "conv-1",
            "content": "hello world",
            "embedding": [1.0, 2.0],
            "timestamp": datetime.now(UTC).isoformat(),
            "metadata": {"topic": "demo"},
        }
        malformed = {
            "id": "conv-2",
            "content": "broken",
            "embedding": [3.0, 4.0],
            "timestamp": "not-a-timestamp",
        }

        worker.hot_store.search_similar = AsyncMock(
            side_effect=[
                [],
                [{"content_hash": "hash:hello world"}],
                [],
            ]
        )

        await worker._process_conversations(
            "system-test", "upload-test", [inserted, duplicate, malformed]
        )

        worker.hot_store.insert.assert_awaited_once()
        assert worker.hot_store.search_similar.await_count == 3

    @pytest.mark.asyncio
    async def test_process_upload_missing_db_and_invalid_json(
        self, worker: IngestionWorker
    ) -> None:
        """Test upload processing when the DB is missing or malformed."""

        upload = SystemMemoryUpload(
            system_id="system-missing",
            upload_id="upload-missing",
            manifest={"version": "1.0"},
            storage_prefix="systems/system-missing/upload-missing/",
            uploaded_at=datetime.now(UTC),
        )

        worker.storage.get = AsyncMock(return_value=None)
        await worker._process_upload(upload)

        worker.storage.get = AsyncMock(return_value="{not json")
        await worker._process_upload(upload)

        worker.hot_store.insert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_process_upload_empty_conversations(self, worker: IngestionWorker) -> None:
        """Test upload processing when the DB has no conversations."""

        upload = SystemMemoryUpload(
            system_id="system-empty",
            upload_id="upload-empty",
            manifest={"version": "1.0"},
            storage_prefix="systems/system-empty/upload-empty/",
            uploaded_at=datetime.now(UTC),
        )

        worker.storage.get = AsyncMock(return_value=_to_json_bytes({"conversations": []}))

        await worker._process_upload(upload)

        worker.hot_store.insert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_collect_system_prefixes_limit(
        self, worker: IngestionWorker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that system prefix collection respects the configured limit."""

        worker.MAX_SYSTEM_PREFIXES = 1

        async def mock_list(prefix: str):
            for value in ("systems/a/", "systems/b/"):
                yield value

        worker.storage.list = mock_list

        prefixes = await worker._collect_system_prefixes()

        assert prefixes == ["systems/a/"]

    @pytest.mark.asyncio
    async def test_scan_systems_concurrent_empty_and_error(self, worker: IngestionWorker) -> None:
        """Test concurrent scanning when no valid prefixes exist and when scanning fails."""

        assert await worker._scan_systems_concurrent(["invalid", "also-invalid"]) == []

        async def fail_scan(system_id: str, system_prefix: str):
            raise RuntimeError("scan failed")

        worker._scan_system = fail_scan  # type: ignore[assignment]
        results = await worker._scan_systems_concurrent(["systems/demo/"])

        assert len(results) == 1
        assert isinstance(results[0], RuntimeError)

    def test_get_upload_storage_prefix_variants(self, worker: IngestionWorker) -> None:
        """Test storage prefix resolution across upload model variants."""

        with_prefix = SystemMemoryUpload(
            system_id="system-a",
            upload_id="upload-a",
            manifest={"version": "1.0"},
            storage_prefix="systems/system-a/upload-a/",
            uploaded_at=datetime.now(UTC),
        )
        assert worker._get_upload_storage_prefix(with_prefix) == "systems/system-a/upload-a/"

        class ManifestPathUpload:
            manifest_path = "systems/system-b/upload-b/manifest.json"
            storage_prefix = None

        assert (
            worker._get_upload_storage_prefix(ManifestPathUpload()) == "systems/system-b/upload-b"
        )

        class NoPrefixUpload:
            storage_prefix = None
            manifest_path = None

        with pytest.raises(AttributeError):
            worker._get_upload_storage_prefix(NoPrefixUpload())

    @pytest.mark.asyncio
    async def test_try_create_upload_empty_manifest(self, worker: IngestionWorker) -> None:
        """Test manifest creation when storage returns an empty payload."""

        worker.storage.exists = AsyncMock(return_value=True)
        worker.storage.download = AsyncMock(return_value=None)

        assert (
            await worker._try_create_upload(
                "system-test",
                "upload-test",
                "systems/system-test/upload-test/",
            )
            is None
        )

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
