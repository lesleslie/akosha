"""Tests for distributed query engine (fan-out across shards)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from akosha.query.distributed import DistributedQueryEngine
from akosha.storage.sharding import ShardRouter


class TestDistributedQueryEngine:
    """Test suite for DistributedQueryEngine."""

    @pytest.fixture
    def shard_router(self) -> ShardRouter:
        """Create shard router."""
        return ShardRouter(num_shards=16)

    @pytest.fixture
    def mock_get_shard_store(self) -> AsyncMock:
        """Create mock shard store factory."""
        store = AsyncMock()
        # Mock search_similar to return results
        store.search_similar = AsyncMock(
            return_value=[
                {
                    "conversation_id": "conv-1",
                    "similarity": 0.9,
                    "content": "Result 1",
                },
                {
                    "conversation_id": "conv-2",
                    "similarity": 0.8,
                    "content": "Result 2",
                },
            ]
        )
        return store

    @pytest.fixture
    def distributed_engine(
        self, shard_router: ShardRouter, mock_get_shard_store: AsyncMock
    ) -> DistributedQueryEngine:
        """Create distributed query engine."""
        return DistributedQueryEngine(
            shard_router=shard_router,
            get_shard_store=lambda shard_id: mock_get_shard_store,
        )

    def test_initialization(
        self, distributed_engine: DistributedQueryEngine
    ) -> None:
        """Test distributed query engine initialization."""
        assert distributed_engine.shard_router is not None
        assert distributed_engine.get_shard_store is not None

    @pytest.mark.asyncio
    async def test_search_all_shards_system_filter(
        self, distributed_engine: DistributedQueryEngine
    ) -> None:
        """Test searching with system ID filter (single shard)."""
        results = await distributed_engine.search_all_shards(
            query_embedding=[0.1] * 384,
            system_id="system-1",
            limit=10,
            timeout=5.0,
        )

        # Should return results
        assert len(results) > 0
        # Results should be sorted by similarity
        assert results[0]["similarity"] >= results[1]["similarity"]

    @pytest.mark.asyncio
    async def test_search_all_shards_global_query(
        self, distributed_engine: DistributedQueryEngine
    ) -> None:
        """Test global query across all shards."""
        results = await distributed_engine.search_all_shards(
            query_embedding=[0.1] * 384,
            system_id=None,  # No filter = all shards
            limit=10,
            timeout=5.0,
        )

        # Should return results from multiple shards
        assert len(results) > 0

    @pytest.mark.asyncio
    async def test_search_respects_limit(
        self, distributed_engine: DistributedQueryEngine
    ) -> None:
        """Test that limit parameter is respected."""
        results = await distributed_engine.search_all_shards(
            query_embedding=[0.1] * 384,
            system_id="system-1",
            limit=1,  # Only want 1 result
            timeout=5.0,
        )

        # Should return at most 1 result
        assert len(results) <= 1

    @pytest.mark.asyncio
    async def test_search_timeout_protection(
        self, distributed_engine: DistributedQueryEngine
    ) -> None:
        """Test that shard queries timeout correctly."""
        # Mock slow shard
        async def slow_search(*args, **kwargs):
            import asyncio
            await asyncio.sleep(10)  # Sleep longer than timeout
            return []

        distributed_engine.get_shard_store = lambda shard_id: AsyncMock(
            search_similar=slow_search
        )

        # Should complete within timeout
        results = await distributed_engine.search_all_shards(
            query_embedding=[0.1] * 384,
            system_id=None,
            limit=10,
            timeout=0.1,  # Very short timeout
        )

        # Should handle timeout gracefully
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_search_handles_shard_errors(
        self, distributed_engine: DistributedQueryEngine
    ) -> None:
        """Test that shard query errors are handled gracefully."""
        # Mock failing shard
        async def failing_search(*args, **kwargs):
            raise RuntimeError("Shard query failed")

        # First shard fails, others succeed
        call_count = [0]
        async def conditional_search(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("First shard fails")
            return [
                {
                    "conversation_id": f"conv-{call_count[0]}",
                    "similarity": 0.9,
                    "content": "Result",
                }
            ]

        distributed_engine.get_shard_store = lambda shard_id: AsyncMock(
            search_similar=conditional_search
        )

        results = await distributed_engine.search_all_shards(
            query_embedding=[0.1] * 384,
            system_id=None,
            limit=10,
            timeout=5.0,
        )

        # Should return results from successful shards
        assert len(results) > 0

    @pytest.mark.asyncio
    async def test_search_merges_results(
        self, distributed_engine: DistributedQueryEngine
    ) -> None:
        """Test that results from multiple shards are merged."""
        # Mock different results per shard
        call_count = [0]
        async def per_shard_results(*args, **kwargs):
            call_count[0] += 1
            return [
                {
                    "conversation_id": f"conv-{call_count[0]}-{i}",
                    "similarity": 0.9 - (i * 0.1),
                    "content": f"Result {i}",
                }
                for i in range(3)
            ]

        distributed_engine.get_shard_store = lambda shard_id: AsyncMock(
            search_similar=per_shard_results
        )

        results = await distributed_engine.search_all_shards(
            query_embedding=[0.1] * 384,
            system_id=None,
            limit=20,
            timeout=5.0,
        )

        # Should have results from multiple shards
        assert len(results) > 0
        # Should be sorted by similarity
        for i in range(len(results) - 1):
            assert results[i]["similarity"] >= results[i + 1]["similarity"]

    @pytest.mark.asyncio
    async def test_search_concurrent_execution(
        self, distributed_engine: DistributedQueryEngine
    ) -> None:
        """Test that shard queries execute concurrently."""
        import asyncio
        import time

        query_times = []

        async def timed_search(*args, **kwargs):
            start = time.time()
            await asyncio.sleep(0.1)
            query_times.append(time.time() - start)
            return [{"conversation_id": "conv-1", "similarity": 0.9}]

        distributed_engine.get_shard_store = lambda shard_id: AsyncMock(
            search_similar=timed_search
        )

        start = time.time()
        await distributed_engine.search_all_shards(
            query_embedding=[0.1] * 384,
            system_id=None,
            limit=10,
            timeout=5.0,
        )
        total_time = time.time() - start

        # If queries ran concurrently, total time should be much less
        # than sum of individual query times
        assert total_time < sum(query_times)

    @pytest.mark.asyncio
    async def test_search_empty_results(
        self, distributed_engine: DistributedQueryEngine
    ) -> None:
        """Test search when no results found."""
        # Mock empty results
        distributed_engine.get_shard_store = lambda shard_id: AsyncMock(
            search_similar=AsyncMock(return_value=[])
        )

        results = await distributed_engine.search_all_shards(
            query_embedding=[0.1] * 384,
            system_id="system-1",
            limit=10,
            timeout=5.0,
        )

        assert results == []
