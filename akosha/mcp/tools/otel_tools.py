"""OTel trace query tools for Akosha.

This module provides MCP tools for querying OTel traces by system_id and
attribute filters (time range, task_class). Used by the Bodai feedback loop
so Akosha can poll traces from all Bodai components.

Uses HotStore search_similar with threshold=0 to retrieve all traces,
then filters in Python on JSON attributes. HNSW index is NOT used.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def register_otel_query_tools(
    app: Any,
    hot_store: Any,
) -> None:
    """Register OTel trace query tools with MCP server.

    Args:
        app: FastMCP application
        hot_store: HotStore instance for data access
    """

    @app.tool()
    async def query_local_traces(
        system_id: str,
        start_time: str | None = None,
        end_time: str | None = None,
        task_class: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query OTel traces by system_id and optional attribute filters.

        Fetches traces for a given system_id within an optional time range,
        and optionally filtered by task.class attribute. Uses HotStore
        search_similar with dummy embedding + threshold=0 (attribute-based
        filtering, HNSW not used).

        Args:
            system_id: Source system identifier (e.g., 'mahavishnu', 'akosha')
            start_time: ISO8601 start time (optional)
            end_time: ISO8601 end time (optional)
            task_class: Task classification tag to filter on (optional)
            limit: Maximum number of traces to return (default 100)

        Returns:
            List of trace records matching the filter criteria
        """
        try:
            # Use query_traces for SQL-native attribute filtering (Phase 1.2)
            # HNSW index is NOT used; WHERE clause pushes filters into SQL
            results = await hot_store.query_traces(
                system_id=system_id,
                start_time=start_time,
                end_time=end_time,
                task_class=task_class,
                limit=limit,
            )

            return [
                {
                    "conversation_id": r.get("conversation_id"),
                    "content": r.get("content"),
                    "timestamp": str(r.get("timestamp", "")),
                    "metadata": r.get("metadata", {}),
                }
                for r in results
            ]

        except Exception as e:
            logger.exception(f"Error querying traces: {e}")
            return []

    logger.info("Registered OTel trace query tools")
