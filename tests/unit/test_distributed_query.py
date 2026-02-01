"""Unit tests for distributed query engine."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from akosha.query.distributed import DistributedQueryEngine
from akosha.storage.sharding import ShardRouter


@pytest.fixture
def shard_router():
    """Create shard router for testing."""
    return ShardRouter(num_shards=4)


@pytest.fixture
def mock_hot_store():
    """Create mock hot store."""
    store = MagicMock()
    store.search_similar = AsyncMock(return_value=[
        {
            "conversation_id": "conv-1",
            "content": "Test content",
            "similarity": 0.9,
        }
    ])
    return store


@pytest.fixture
def distributed_engine(shard_router, mock_hot_store):
    """Create distributed query engine for testing."""
    get_shard_store = lambda shard_id: mock_hot_store
    return DistributedQueryEngine(
        shard_router=shard_router,
        get_shard_store=get_shard_store,
    )


@pytest.mark.asyncio
async def test_search_all_shards_single_system(distributed_engine):
    """Test searching a single system (single shard)."""
    query_embedding = [0.1] * 384
    system_id = "test-system-123"

    results = await distributed_engine.search_all_shards(
        query_embedding=query_embedding,
        system_id=system_id,
        limit=10,
    )

    assert len(results) == 1
    assert results[0]["conversation_id"] == "conv-1"
    assert results[0]["similarity"] == 0.9


@pytest.mark.asyncio
async def test_search_all_shards_all_shards(distributed_engine):
    """Test searching across all shards."""
    query_embedding = [0.1] * 384

    results = await distributed_engine.search_all_shards(
        query_embedding=query_embedding,
        limit=10,
    )

    # Should query all 4 shards and return deduplicated results
    assert len(results) == 1
    assert results[0]["conversation_id"] == "conv-1"


@pytest.mark.asyncio
async def test_search_shard_timeout(shard_router, mock_hot_store):
    """Test timeout handling for shard queries."""
    # Make search_similar timeout
    async def slow_search(*args, **kwargs):
        await asyncio.sleep(10)
        return []

    mock_hot_store.search_similar = slow_search

    get_shard_store = lambda shard_id: mock_hot_store
    engine = DistributedQueryEngine(
        shard_router=shard_router,
        get_shard_store=get_shard_store,
    )

    query_embedding = [0.1] * 384
    system_id = "test-system-123"

    # Should timeout but handle gracefully
    results = await engine.search_all_shards(
        query_embedding=query_embedding,
        system_id=system_id,
        limit=10,
        timeout=0.1,  # Very short timeout
    )

    # Should return empty results instead of crashing
    assert len(results) == 0


@pytest.mark.asyncio
async def test_search_shard_failure(shard_router):
    """Test graceful handling of shard failures."""

    def failing_get_shard_store(shard_id):
        """Return a store that raises an exception."""
        store = MagicMock()
        store.search_similar = AsyncMock(side_effect=RuntimeError("Shard failure"))
        return store

    engine = DistributedQueryEngine(
        shard_router=shard_router,
        get_shard_store=failing_get_shard_store,
    )

    query_embedding = [0.1] * 384
    system_id = "test-system-123"

    # Should handle failure gracefully
    results = await engine.search_all_shards(
        query_embedding=query_embedding,
        system_id=system_id,
        limit=10,
    )

    # Should return empty results instead of crashing
    assert len(results) == 0


@pytest.mark.asyncio
async def test_search_shard_with_timeout(distributed_engine):
    """Test _search_shard_with_timeout method."""
    query_embedding = [0.1] * 384

    result = await distributed_engine._search_shard_with_timeout(
        shard_id=0,
        query_embedding=query_embedding,
        system_id=None,
        limit=10,
        timeout=5.0,
    )

    assert len(result) == 1
    assert result[0]["conversation_id"] == "conv-1"
