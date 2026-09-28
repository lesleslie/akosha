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

    @app.tool(name="akosha_query_local_traces")
    async def query_local_traces(
        system_id: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        task_class: str | None = None,
        limit: int = 100,
        dry_run: bool = False,
    ) -> list[dict[str, Any]]:
        """Query OTel traces by system_id and optional attribute filters.

        Fetches traces for a given system_id within an optional time range,
        and optionally filtered by task.class attribute. Uses HotStore
        search_similar with dummy embedding + threshold=0 (attribute-based
        filtering, HNSW not used).

        Args:
            system_id: Source system identifier (e.g., 'mahavishnu', 'akosha').
                Pass ``None`` (or omit) to query across ALL systems — useful
                for diagnostic probes when the stored system_id is unknown
                or doesn't match the caller's expectation.
            start_time: ISO8601 start time (optional)
            end_time: ISO8601 end time (optional)
            task_class: Task classification tag to filter on (optional)
            limit: Maximum number of traces to return (default 100)
            dry_run: When True, bypasses BOTH the system_id and task_class
                SQL filters and returns raw metadata so operators can
                inspect the actual JSON shape stored in the conversations
                table. Diagnostic only — strips the bulky ``content`` blob
                and clamps ``limit`` to 20 so a wide probe stays readable.

        Returns:
            List of trace records matching the filter criteria. When
            ``dry_run`` is True, records include the raw ``metadata`` dict
            (with ``attributes.task_class`` etc.) and omit the bulky
            ``content`` blob so the operator can read the structure.
        """
        try:
            # When dry_run is set, force system_id=None AND task_class=None
            # so the no-filter SQL path runs (where_clause = "1=1") and we
            # can see every row's metadata. Also clamp the limit so a wide
            # probe doesn't drag back thousands of records.
            effective_system_id: str | None = (
                None if (dry_run or system_id in (None, "", "*")) else system_id
            )
            effective_task_class: str | None = None if dry_run else task_class
            effective_limit = min(limit, 20) if dry_run else limit

            # Use query_traces for SQL-native attribute filtering (Phase 1.2)
            # HNSW index is NOT used; WHERE clause pushes filters into SQL
            results = await hot_store.query_traces(
                system_id=effective_system_id,
                start_time=start_time,
                end_time=end_time,
                task_class=effective_task_class,
                limit=effective_limit,
            )

            out: list[dict[str, Any]] = []
            for r in results:
                row: dict[str, Any] = {
                    "system_id": r.get("system_id"),
                    "conversation_id": r.get("conversation_id"),
                    "timestamp": str(r.get("timestamp", "")),
                    "metadata": r.get("metadata", {}),
                }
                if not dry_run:
                    # Production path: include content for downstream consumers
                    row["content"] = r.get("content")
                out.append(row)
            return out

        except Exception as e:
            logger.exception(f"Error querying traces: {e}")
            return []

    logger.info("Registered OTel trace query tools")
