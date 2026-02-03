"""Code graph ingestion worker from Session-Buddy.

This module provides a worker that pulls indexed code graphs from
Session-Buddy via MCP and stores them in Akosha's storage for
pattern analysis and cross-repo similarity detection.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from akosha.storage.hot_store import HotStore

logger = logging.getLogger(__name__)


class CodeGraphIngester:
    """Pull-based ingestion worker for code graphs from Session-Buddy.

    Polls Session-Buddy MCP endpoint for newly indexed code graphs
    and ingests them into Akosha's storage tiers for pattern analysis.
    """

    def __init__(
        self,
        hot_store: HotStore,
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
        self._http_client: httpx.AsyncClient | None = None

        # Track last known code graphs to avoid duplicates
        self._known_graph_ids: set[str] = set()

    async def start(self) -> None:
        """Start the code graph ingestion worker."""
        if self._running:
            logger.warning("Code graph ingester already running")
            return

        # Initialize HTTP client
        self._http_client = httpx.AsyncClient(timeout=30.0)

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

        # Close HTTP client
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        logger.info("Stopped code graph ingestion")

    async def _polling_loop(self) -> None:
        """Main polling loop for code graph ingestion.

        Polls Session-Buddy for newly indexed code graphs and ingests them.
        """
        try:
            while self._running:
                try:
                    # Discover new code graphs
                    new_graphs = await self._discover_code_graphs()

                    if new_graphs:
                        logger.info(f"Discovered {len(new_graphs)} new code graphs")

                        # Process concurrently with semaphore protection
                        semaphore = asyncio.Semaphore(self.max_concurrent_ingests)

                        async def process_with_semaphore(
                            graph: dict[str, Any], _sem=semaphore
                        ) -> None:
                            """Process graph with semaphore limiting."""
                            async with _sem:
                                return await self._ingest_code_graph(graph)

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

                    # Wait before next poll
                    await asyncio.sleep(self.poll_interval_seconds)

                except asyncio.CancelledError:
                    logger.info("Code graph polling loop cancelled")
                    break
                except Exception as e:
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
        if not self._http_client:
            logger.warning("HTTP client not initialized")
            return []

        try:
            url = f"{self.session_buddy_endpoint}/tools/call"
            payload = {
                "name": "list_code_graphs",
                "arguments": {"limit": 100},
            }

            response = await self._http_client.post(url, json=payload)
            response.raise_for_status()

            result = response.json()

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

        except httpx.HTTPError as e:
            logger.warning(f"HTTP error calling Session-Buddy: {e}")
            return []
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
        if not self._http_client:
            return False

        try:
            # Fetch full code graph data
            url = f"{self.session_buddy_endpoint}/tools/call"
            payload = {
                "name": "get_code_graph",
                "arguments": {
                    "repo_path": graph_summary["repo_path"],
                    "commit_hash": graph_summary["commit_hash"],
                },
            }

            response = await self._http_client.post(url, json=payload)
            response.raise_for_status()

            result = response.json()

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

            # Store in hot store
            await self.hot_store.store_code_graph(
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

        except httpx.HTTPError as e:
            logger.warning(f"HTTP error fetching code graph: {e}")
            return False
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
