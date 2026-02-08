"""Integration tests for distributed query engine.

Tests fan-out queries across shards with timeout protection.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from akosha.query.distributed import DistributedQueryEngine
from akosha.storage.sharding import ShardRouter
from akosha.query.aggregator import QueryAggregator


@pytest.fixture
def shard_router():
    """Create shard router with 256 shards."""
    return ShardRouter(num_shards=256)


@pytest.fixture
def mock_shard_query():
    """Mock single shard query execution."""
    async def mock_query(shard_id, query_embedding, limit):
        await asyncio.sleep(0.1)  # Simulate network latency
        return [
            {
                "conversation_id": f"conv-{shard_id}-{i}",
                "similarity": 0.9 - (i * 0.01),
                "metadata": {},
            }
            for i in range(limit)
        ]

    return mock_query


@pytest.mark.asyncio
async def test_distributed_query_fans_out_to_all_shards(shard_router, mock_shard_query):
    """Test that distributed query fans out to all shards concurrently."""
    engine = DistributedQueryEngine(
        shard_router=shard_router,
        shard_query=mock_shard_query,
        num_shards=256,
        timeout_per_shard=5.0,
    )

    query_embedding = [0.1] * 384
    system_id = "test-system"

    start = asyncio.get_event_loop().time()
    results = await engine.search_all_shards(
        query_embedding=query_embedding,
        system_id=system_id,
        limit=10,
    )
    duration = asyncio.get_event_loop().time() - start

    # Should be fast due to concurrent execution
    assert duration < 1.0  # <1 second for 256 shards
    assert len(results) > 0


@pytest.mark.asyncio
async def test_distributed_query_handles_partial_shard_failures(shard_router):
    """Test that query engine handles shard failures gracefully."""
    async def failing_query(shard_id, query_embedding, limit):
        # Every 3rd shard fails
        if shard_id % 3 == 0:
            raise Exception(f"Shard {shard_id} unavailable")
        await asyncio.sleep(0.05)
        return [{"conversation_id": f"conv-{shard_id}", "similarity": 0.8}]

    engine = DistributedQueryEngine(
        shard_router=shard_router,
        shard_query=failing_query,
        num_shards=12,  # Smaller for testing
        timeout_per_shard=5.0,
    )

    results = await engine.search_all_shards(
        query_embedding=[0.1] * 384,
        system_id="test-system",
        limit=10,
    )

    # Should return results from successful shards
    successful_shards = 12 - (12 // 3)  # 8 successful
    assert len(results) == successful_shards


@pytest.mark.asyncio
async def test_distributed_query_enforces_timeout(shard_router):
    """Test that per-shard timeout is enforced."""
    async def slow_query(shard_id, query_embedding, limit):
        await asyncio.sleep(10.0)  # Too slow, should timeout
        return []

    engine = DistributedQueryEngine(
        shard_router=shard_router,
        shard_query=slow_query,
        num_shards=4,
        timeout_per_shard=0.5,  # 500ms timeout
    )

    start = asyncio.get_event_loop().time()
    results = await engine.search_all_shards(
        query_embedding=[0.1] * 384,
        system_id="test-system",
        limit=10,
    )
    duration = asyncio.get_event_loop().time() - start

    # Should timeout quickly
    assert duration < 2.0  # Much less than 10s per shard
    # Results may be empty or partial depending on timeout
    assert isinstance(results, list)


@pytest.mark.asyncio
async def test_query_aggregator_merges_and_deduplicates():
    """Test that query aggregator merges and deduplicates results."""
    shard_1_results = [
        {"conversation_id": "conv-1", "similarity": 0.9},
        {"conversation_id": "conv-2", "similarity": 0.8},
        {"conversation_id": "conv-3", "similarity": 0.7},
    ]

    shard_2_results = [
        {"conversation_id": "conv-1", "similarity": 0.85},  # Duplicate
        {"conversation_id": "conv-4", "similarity": 0.75},
        {"conversation_id": "conv-5", "similarity": 0.65},
    ]

    merged = QueryAggregator.merge_results(
        results=[shard_1_results, shard_2_results]
    )

    # Should deduplicate by conversation_id
    conversation_ids = [r["conversation_id"] for r in merged]
    assert len(conversation_ids) == len(set(conversation_ids))  # No duplicates

    # Should keep highest similarity for duplicates
    conv_1 = next(r for r in merged if r["conversation_id"] == "conv-1")
    assert conv_1["similarity"] == 0.9  # Kept higher similarity


@pytest.mark.asyncio
async def test_query_aggregator_reranks_by_similarity():
    """Test that query aggregator re-ranks by similarity."""
    results = [
        {"conversation_id": "conv-1", "similarity": 0.7},
        {"conversation_id": "conv-2", "similarity": 0.9},
        {"conversation_id": "conv-3", "similarity": 0.8},
        {"conversation_id": "conv-4", "similarity": 0.6},
    ]

    merged = QueryAggregator.merge_results([results])

    # Should be sorted by similarity descending
    similarities = [r["similarity"] for r in merged]
    assert similarities == sorted(similarities, reverse=True)

    # First result should be highest similarity
    assert merged[0]["conversation_id"] == "conv-2"
    assert merged[0]["similarity"] == 0.9


@pytest.mark.asyncio
async def test_shard_router_consistent_hashing(shard_router):
    """Test that shard router provides consistent hashing."""
    # Same system ID should always map to same shard
    system_id = "test-system"

    shard_1 = shard_router.get_shard(system_id)
    shard_2 = shard_router.get_shard(system_id)

    assert shard_1 == shard_2

    # Different system IDs should distribute across shards
    system_ids = [f"system-{i}" for i in range(1000)]
    shards = [shard_router.get_shard(sid) for sid in system_ids]

    # Should distribute reasonably evenly
    shard_counts = {}
    for s in shards:
        shard_counts[s] = shard_counts.get(s, 0) + 1

    # Max shard should have <2× average
    avg_count = len(system_ids) / shard_router.num_shards
    max_count = max(shard_counts.values())
    assert max_count < avg_count * 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
