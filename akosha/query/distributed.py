"""Distributed query engine for fan-out across multiple shards."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from akosha.storage.hot_store import HotStore
    from akosha.storage.sharding import ShardRouter

logger = logging.getLogger(__name__)


class DistributedQueryEngine:
    """Query engine that fans out across multiple shards."""

    def __init__(
        self,
        shard_router: ShardRouter,
        get_shard_store: Callable[[int], HotStore],
    ) -> None:
        """Initialize distributed query engine.

        Args:
            shard_router: Shard router for determining target shards
            get_shard_store: Function to get HotStore for a shard ID
        """
        self.shard_router = shard_router
        self.get_shard_store = get_shard_store
        logger.info(f"DistributedQueryEngine initialized with {shard_router.num_shards} shards")

    async def search_all_shards(
        self,
        query_embedding: list[float],
        system_id: str | None = None,
        limit: int = 10,
        timeout: float = 5.0,
    ) -> list[dict[str, Any]]:
        """Search across all relevant shards.

        This method fans out queries concurrently to all target shards,
        handles partial failures gracefully, merges results, and re-ranks
        by similarity score.

        Steps:
            1. Determine target shards using shard_router
            2. Fan-out queries concurrently using asyncio.gather
            3. Handle partial shard failures gracefully
            4. Merge results using QueryAggregator
            5. Re-rank by similarity
            6. Return top N results

        Args:
            query_embedding: Query vector for similarity search
            system_id: Optional system filter (queries single shard if provided)
            limit: Maximum number of results to return
            timeout: Per-shard timeout in seconds

        Returns:
            List of search results ranked by similarity score
        """
        # Step 1: Determine target shards
        target_shards = self.shard_router.get_target_shards(system_id)
        logger.info(f"Querying {len(target_shards)} shards (system_id={system_id or 'all'})")

        # Step 2: Fan-out queries concurrently
        tasks = [
            self._search_shard_with_timeout(
                shard_id=shard_id,
                query_embedding=query_embedding,
                system_id=system_id,
                limit=limit,
                timeout=timeout,
            )
            for shard_id in target_shards
        ]

        # Execute all queries with graceful failure handling
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Step 3: Handle partial failures
        successful_results: list[list[dict[str, Any]]] = []
        failed_shards: list[int] = []

        for shard_id, result in zip(target_shards, results, strict=False):
            if isinstance(result, Exception):
                logger.warning(f"Shard {shard_id} failed: {type(result).__name__}: {result}")
                failed_shards.append(shard_id)
            elif isinstance(result, list):
                successful_results.append(result)
            else:
                logger.error(f"Shard {shard_id} returned unexpected type: {type(result)}")

        if failed_shards:
            logger.warning(
                f"{len(failed_shards)} shards failed: {failed_shards}. "
                f"Continuing with {len(successful_results)} successful shards."
            )

        # Step 4: Merge results using QueryAggregator
        from akosha.query.aggregator import QueryAggregator

        merged = QueryAggregator.merge_results(
            result_sets=successful_results,
            limit=limit,
        )

        logger.info(f"Returning {len(merged)} results from {len(successful_results)} shards")

        return merged

    async def _search_shard_with_timeout(
        self,
        shard_id: int,
        query_embedding: list[float],
        system_id: str | None,
        limit: int,
        timeout: float,
    ) -> list[dict[str, Any]]:
        """Search a single shard with timeout protection.

        Args:
            shard_id: Shard identifier
            query_embedding: Query vector for similarity search
            system_id: Optional system filter
            limit: Maximum results from this shard
            timeout: Timeout in seconds

        Returns:
            List of search results from this shard

        Raises:
            TimeoutError: If shard query exceeds timeout
            Exception: If shard query fails for other reasons
        """
        try:
            # Execute shard search with timeout protection
            result = await asyncio.wait_for(
                self._search_shard(
                    shard_id=shard_id,
                    query_embedding=query_embedding,
                    system_id=system_id,
                    limit=limit,
                ),
                timeout=timeout,
            )
            return result

        except TimeoutError:
            logger.warning(f"Shard {shard_id} timed out after {timeout}s")
            raise
        except Exception as e:
            logger.error(f"Shard {shard_id} query failed: {e}")
            raise

    async def _search_shard(
        self,
        shard_id: int,
        query_embedding: list[float],
        system_id: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Execute search on a single shard.

        Args:
            shard_id: Shard identifier
            query_embedding: Query vector for similarity search
            system_id: Optional system filter
            limit: Maximum results from this shard

        Returns:
            List of search results from this shard
        """
        # Get HotStore for this shard
        shard_store = self.get_shard_store(shard_id)

        # Execute search on the shard
        results = await shard_store.search_similar(
            query_embedding=query_embedding,
            system_id=system_id,
            limit=limit,
        )

        logger.debug(f"Shard {shard_id} returned {len(results)} results")

        return results
