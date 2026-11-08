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

    @app.tool(name="query_local_traces")
    async def query_local_traces(
        task_class: str,
        system_id: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        limit: int = 100,
        dry_run: bool = False,
    ) -> list[dict[str, Any]]:
        """Query OTel traces for a task class from the fleet trace store.

        Fetches traces for a given task_class within an optional absolute
        ISO8601 time range, and optionally filtered by source system. Do NOT
        use to fetch one trace by id — use `get_otel_trace` on Mahavishnu for
        that. Returns a list of trace records; an empty list means no trace
        matched the filter, or the store was unreachable, so an empty result
        does not distinguish "no matches" from "query failed". Pass
        ``system_id`` to restrict the search to one source system, or omit it
        to query across all systems. Set `dry_run` to get the raw stored
        metadata instead of the bulky `content` blob; that mode clamps
        `limit` to 20 and keeps your `system_id` and `task_class` filters in
        force. Side effect: none; reads the trace store only.

        Args:
            task_class: Task classification tag to filter on (e.g. "code_generation")
            system_id: Optional source system identifier (e.g., 'mahavishnu').
                Pass ``None`` (or omit, or pass "" or "*") to query across ALL
                systems — useful for diagnostic probes when the stored
                system_id is unknown or doesn't match the caller's expectation.
            start_time: ISO8601 start time (optional)
            end_time: ISO8601 end time (optional)
            limit: Maximum number of traces to return (default 100)
            dry_run: When True, returns the raw stored metadata instead of
                the bulky ``content`` blob so operators can inspect the
                actual JSON shape held in the conversations table. The
                ``system_id`` and ``task_class`` filters stay in force —
                this flag changes the output shape, not the scope. Clamps
                ``limit`` to 20 so a wide probe stays readable.

        Returns:
            List of trace records matching the filter criteria. When
            ``dry_run`` is True, records include the raw ``metadata`` dict
            (with ``attributes.task_class`` etc.) and omit the bulky
            ``content`` blob so the operator can read the structure.
        """
        # Implements: REQ-MCPTD-003
        if not task_class or not isinstance(task_class, str):
            logger.error("query_local_traces: task_class must be a non-empty string")
            return []

        try:
            # ``dry_run`` is a *shape* switch, not a scope switch: it returns
            # the raw stored metadata instead of the bulky content blob and
            # clamps the limit so a wide probe stays readable. It deliberately
            # does NOT disable the caller's filters -- a caller-supplied flag
            # that switches off scoping is an escape hatch on a public tool.
            # To inspect rows outside a filter, omit ``system_id`` (or pass
            # "*"), which is the documented all-systems query.
            effective_system_id: str | None = None if system_id in (None, "", "*") else system_id
            effective_task_class: str | None = task_class
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
