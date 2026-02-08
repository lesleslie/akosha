"""Integration tests for Akosha ingestion pipeline.

Tests Session-Buddy upload flow through ingestion worker to hot tier storage.
"""

import asyncio
import pytest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from akosha.ingestion.worker import IngestionWorker
from akosha.storage.hot_store import HotStore
from akosha.storage.models import SystemMemoryUpload


@pytest.fixture
async def hot_store(tmp_path: Path):
    """Create a temporary hot store for testing."""
    store = HotStore(database_path=str(tmp_path / "test_hot.duckdb"))
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture
def mock_storage():
    """Mock Oneiric storage adapter."""
    storage = AsyncMock()

    # Mock list_prefixes to return systems
    storage.list_prefixes = AsyncMock(return_value=[
        "systems/system-1/",
        "systems/system-2/",
    ])

    # Mock list to return uploads
    async def mock_list(prefix: str):
        if "system-1" in prefix:
            return ["systems/system-1/upload-2025-01-31/", "systems/system-1/upload-2025-02-01/"]
        elif "system-2" in prefix:
            return ["systems/system-2/upload-2025-01-31/"]
        return []

    storage.list = mock_list

    # Mock download to return manifest
    async def mock_download(path: str):
        if "manifest.json" in path:
            return {
                "system_id": "system-1",
                "upload_id": "upload-001",
                "conversation_count": 10,
                "timestamp": datetime.now(UTC).isoformat(),
                "checksum": "abc123",
            }
        return None

    storage.download = mock_download

    return storage


@pytest.mark.asyncio
async def test_ingestion_worker_discovers_uploads(mock_storage):
    """Test that ingestion worker discovers uploads from cloud storage."""
    worker = IngestionWorker(
        storage=mock_storage,
        max_concurrent_ingests=10,
    )

    uploads = await worker._discover_uploads()

    assert len(uploads) == 3  # 2 from system-1, 1 from system-2
    assert all(isinstance(u, SystemMemoryUpload) for u in uploads)

    # Verify system-1 uploads
    system_1_uploads = [u for u in uploads if u.system_id == "system-1"]
    assert len(system_1_uploads) == 2


@pytest.mark.asyncio
async def test_ingestion_worker_processes_upload(mock_storage, hot_store):
    """Test that ingestion worker processes upload to hot tier."""
    worker = IngestionWorker(
        storage=mock_storage,
        hot_store=hot_store,
        max_concurrent_ingests=10,
    )

    upload = SystemMemoryUpload(
        system_id="system-1",
        upload_id="upload-001",
        conversation_count=10,
        timestamp=datetime.now(UTC),
        manifest_path="systems/system-1/upload-2025-01-31/manifest.json",
    )

    # Mock the actual processing
    with patch.object(worker, '_process_conversations', return_value=10):
        processed = await worker._process_upload(upload)

    assert processed == 10  # 10 conversations processed


@pytest.mark.asyncio
async def test_concurrent_upload_processing(mock_storage, hot_store):
    """Test that worker processes uploads concurrently with semaphore."""
    worker = IngestionWorker(
        storage=mock_storage,
        hot_store=hot_store,
        max_concurrent_ingests=2,  # Limit to 2 for testing
    )

    uploads = [
        SystemMemoryUpload(
            system_id=f"system-{i}",
            upload_id=f"upload-{i}",
            conversation_count=1,
            timestamp=datetime.now(UTC),
            manifest_path=f"systems/system-{i}/manifest.json",
        )
        for i in range(5)
    ]

    # Mock processing
    async def mock_process(upload):
        await asyncio.sleep(0.1)  # Simulate work
        return 1

    with patch.object(worker, '_process_upload', side_effect=mock_process):
        start = asyncio.get_event_loop().time()
        results = await worker.run(uploads)
        duration = asyncio.get_event_loop().time() - start

    # With max_concurrent_ingests=2, 5 uploads should take ~0.3s (2 batches)
    assert duration < 0.5  # Should be <0.5s with concurrency
    assert len(results) == 5


@pytest.mark.asyncio
async def test_hot_store_insert_and_query(hot_store):
    """Test that hot store can insert and query conversations."""
    conversation_id = "test-conv-001"
    system_id = "test-system"
    content = "Test conversation content"
    embedding = [0.1] * 384  # Dummy 384D embedding
    metadata = {"test": "data"}

    # Insert conversation
    await hot_store.insert_conversation(
        conversation_id=conversation_id,
        system_id=system_id,
        content=content,
        embedding=embedding,
        metadata=metadata,
    )

    # Query with similar embedding
    results = await hot_store.search_similar(
        embedding=embedding,
        system_id=system_id,
        limit=10,
    )

    assert len(results) == 1
    assert results[0]["conversation_id"] == conversation_id
    assert results[0]["similarity"] > 0.99  # Should be very similar


@pytest.mark.asyncio
async def test_hot_store_persistence(hot_store, tmp_path: Path):
    """Test that hot store persists data across restarts."""
    conversation_id = "test-conv-persist"

    # Insert into first instance
    await hot_store.insert_conversation(
        conversation_id=conversation_id,
        system_id="test-system",
        content="Persistent content",
        embedding=[0.1] * 384,
    )

    # Close and reopen
    await hot_store.close()

    # Reopen hot store
    hot_store2 = HotStore(database_path=str(tmp_path / "test_hot.duckdb"))
    await hot_store2.initialize()

    # Verify data persists
    results = await hot_store2.search_similar(
        embedding=[0.1] * 384,
        system_id="test-system",
        limit=10,
    )

    assert len(results) == 1
    assert results[0]["conversation_id"] == conversation_id

    await hot_store2.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
