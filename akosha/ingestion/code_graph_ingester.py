"""Code graph ingestion worker from Session-Buddy.

This module provides a worker that pulls indexed code graphs from
Session-Buddy via MCP and stores them in Akosha's storage for
pattern analysis and cross-repo similarity detection.

MCP transport: uses ``mcp_common.clients.CommonMCPClient.call_tool``
(streamable-HTTP) instead of legacy ``POST /tools/call`` POSTs.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from mcp_common.clients.common_mcp_client import CommonMCPClient

from akosha.mcp.client import extract_mcp_payload
from akosha.storage.hot_store import HotStore

if TYPE_CHECKING:
    from akosha.storage.pgvector_hot_store import PgvectorHotStore

logger = logging.getLogger(__name__)


class CodeGraphIngester:
    """Pull-based ingestion worker for code graphs from Session-Buddy.

    Polls Session-Buddy MCP endpoint for newly indexed code graphs
    and ingests them into Akosha's storage tiers for pattern analysis.
    """

    def __init__(
        self,
        hot_store: HotStore | PgvectorHotStore,
        session_buddy_endpoint: str = "http://localhost:8678/mcp",
        poll_interval_seconds: int = 60,
        max_concurrent_ingests: int = 10,
    ) -> None:
        """Initialize code graph ingester.

        Args:
            hot_store: Hot store for code graph insertion
            session_buddy_endpoint: Session-Buddy MCP endpoint
            poll_interval_seconds: Polling interval (default 60s)
            max_concurrent_ingests: Maximum concurrent ingestion tasks
        """
        self.hot_store = hot_store
        self.session_buddy_endpoint = session_buddy_endpoint
        self.poll_interval_seconds = poll_interval_seconds
        self.max_concurrent_ingests = max_concurrent_ingests
        self._running = False
        self._poll_task: asyncio.Task[None] | None = None
        self._client: CommonMCPClient | None = None

        # Track last known code graphs to avoid duplicates
        self._known_graph_ids: set[str] = set()

        # Per-feed observability counters (see mcp-backend-wiring-discipline.md):
        # every feed must expose cycles_total, errors_total, last_poll_at,
        # last_error_at so the /health aggregator's ``is_healthy`` predicate
        # can escalate DEGRADED for fresh errors and HNSW hardening can
        # distinguish broken-before-first-success producers from
        # healthy-but-still-loading ones.
        self._cycles_total: int = 0
        self._errors_total: int = 0
        self._last_poll_at: float | None = None
        self._last_error_at: float | None = None

    async def start(self) -> None:
        """Start the code graph ingestion worker."""
        if self._running:
            logger.warning("Code graph ingester already running")
            return

        # Initialize MCP client (streamable-HTTP via CommonMCPClient).
        # Use a 30-second per-call timeout to match the prior httpx
        # behaviour; CommonMCPClient's constructor timeout governs the
        # session-establishment handshake.
        self._client = CommonMCPClient(
            base_url=self.session_buddy_endpoint,
            timeout=30.0,
        )

        self._running = True
        self._poll_task = asyncio.create_task(self._polling_loop())
        logger.info(
            f"Started code graph ingestion from Session-Buddy "
            f"(interval={self.poll_interval_seconds}s)"
        )

    async def stop(self) -> None:
        """Stop the code graph ingestion worker."""
        if not self._running:
            return

        self._running = False

        if self._poll_task:
            self._poll_task.cancel()
            import contextlib

            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task

        # Close MCP client
        if self._client:
            await self._client.aclose()
            self._client = None

        logger.info("Stopped code graph ingestion")

    async def _polling_loop(self) -> None:
        """Main polling loop for code graph ingestion.

        Polls Session-Buddy for newly indexed code graphs and ingests them.
        """
        try:
            while self._running:
                try:
                    # Phase 4: bump cycles_total at the START of each cycle
                    # (mirrors OtelTraceIngester) so the aggregator's HNSW
                    # hardening stops flagging this feed as
                    # broken-before-first-success after the first cycle.
                    self._cycles_total += 1
                    # Discover new code graphs
                    new_graphs = await self._discover_code_graphs()

                    if new_graphs:
                        logger.info(f"Discovered {len(new_graphs)} new code graphs")

                        # Process concurrently with semaphore protection
                        semaphore = asyncio.Semaphore(self.max_concurrent_ingests)

                        async def process_with_semaphore(
                            graph: dict[str, Any], _sem: asyncio.Semaphore = semaphore
                        ) -> None:
                            """Process graph with semaphore limiting."""
                            async with _sem:
                                await self._ingest_code_graph(graph)

                        # Create and execute tasks
                        tasks = [process_with_semaphore(graph) for graph in new_graphs]
                        results = await asyncio.gather(*tasks, return_exceptions=True)

                        # Log any errors
                        for i, result in enumerate(results):
                            if isinstance(result, Exception):
                                graph = new_graphs[i]
                                logger.error(
                                    f"Code graph ingestion failed for {graph.get('id', 'unknown')}: {result}",
                                    exc_info=result if logger.isEnabledFor(logging.DEBUG) else None,
                                )

                    # Phase 4: record the most recent successful poll so the
                    # aggregator can compute ``last_updated_timestamp`` and
                    # distinguish warming_up (cycle ran, no data) from
                    # broken-before-first-success (cycle never ran).
                    self._last_poll_at = time.time()
                    # Wait before next poll
                    await asyncio.sleep(self.poll_interval_seconds)

                except asyncio.CancelledError:
                    logger.info("Code graph polling loop cancelled")
                    break
                except Exception as e:
                    # Phase 4: track per-cycle errors + last-error timestamp
                    # so the aggregator's time-bounded decay predicate can
                    # escalate DEGRADED for fresh errors within halflife.
                    self._errors_total += 1
                    self._last_error_at = time.time()
                    logger.error(f"Error in code graph polling loop: {e}", exc_info=True)
                    # Continue running despite errors
                    await asyncio.sleep(self.poll_interval_seconds)

        finally:
            logger.info("Code graph polling loop terminated")

    async def _discover_code_graphs(self) -> list[dict[str, Any]]:
        """Discover newly indexed code graphs from Session-Buddy.

        Returns:
            List of new code graph dictionaries
        """
        if not self._client:
            logger.warning("MCP client not initialized")
            return []

        try:
            call_result = await self._client.call_tool(
                "list_code_graphs",
                {"limit": 100},
                timeout=30.0,
            )

            result = extract_mcp_payload(call_result)
            if not isinstance(result, dict):
                logger.warning("Unexpected list_code_graphs payload shape: %r", result)
                return []

            if result.get("status") != "success":
                logger.warning(f"Failed to list code graphs: {result.get('message')}")
                return []

            # Filter out already known graphs
            all_graphs = result.get("code_graphs", [])
            new_graphs = [
                graph
                for graph in all_graphs
                if graph.get("id") and graph["id"] not in self._known_graph_ids
            ]

            # Mark as known
            for graph in new_graphs:
                if graph.get("id"):
                    self._known_graph_ids.add(graph["id"])

            return new_graphs

        except Exception as e:
            logger.warning(f"Error discovering code graphs: {e}")
            return []

    async def _ingest_code_graph(self, graph_summary: dict[str, Any]) -> bool:
        """Ingest a code graph into Akosha's storage.

        Args:
            graph_summary: Summary from list_code_graphs (repo_path, commit_hash, etc.)

        Returns:
            True if ingestion successful
        """
        if not self._client:
            return False

        try:
            # Fetch full code graph data
            call_result = await self._client.call_tool(
                "get_code_graph",
                {
                    "repo_path": graph_summary["repo_path"],
                    "commit_hash": graph_summary["commit_hash"],
                },
                timeout=30.0,
            )

            result = extract_mcp_payload(call_result)
            if not isinstance(result, dict):
                logger.warning("Unexpected get_code_graph payload shape: %r", result)
                return False

            if result.get("status") != "success":
                logger.warning(f"Failed to get code graph: {result.get('message')}")
                return False

            # Extract full graph data
            graph_data = {
                "repo_path": result.get("repo_path"),
                "commit_hash": result.get("commit_hash"),
                "indexed_at": result.get("indexed_at"),
                "nodes_count": result.get("nodes_count", 0),
                "graph_data": result.get("graph_data", {}),
                "metadata": result.get("metadata", {}),
            }

            # Store in hot store. ``store_code_graph`` is only on the
            # DuckDB-backed ``HotStore``; the pgvector backend has no
            # equivalent surface, so we skip the write in that case.
            # The ``hasattr`` short-circuit keeps MagicMock-based
            # tests working — they patch ``store_code_graph`` directly
            # without going through the isinstance chain.
            if not isinstance(self.hot_store, HotStore) and not hasattr(
                self.hot_store, "store_code_graph"
            ):
                logger.debug("CodeGraphIngester: hot_store lacks store_code_graph; skipping write")
                return True
            await self.hot_store.store_code_graph(  # ty: ignore[call-non-callable]
                repo_path=graph_data["repo_path"],
                commit_hash=graph_data["commit_hash"],
                nodes_count=graph_data["nodes_count"],
                graph_data=graph_data["graph_data"],
                metadata=graph_data["metadata"],
            )

            logger.info(
                f"Ingested code graph: {graph_data['repo_path']} @ {graph_data['commit_hash'][:8]} "
                f"({graph_data['nodes_count']} nodes)"
            )

            return True

        except Exception as e:
            logger.warning(f"Error ingesting code graph: {e}")
            return False

    async def get_ingestion_status(self) -> dict[str, Any]:
        """Get the current ingestion status.

        Returns:
            Dict with ingestion statistics
        """
        return {
            "running": self._running,
            "known_graphs": len(self._known_graph_ids),
            "session_buddy_endpoint": self.session_buddy_endpoint,
            "poll_interval_seconds": self.poll_interval_seconds,
            "timestamp": datetime.now(UTC).isoformat(),
        }
