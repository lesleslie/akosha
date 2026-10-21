"""``spans_substrate`` browse tools (read-path R-2).

Thin MCP boundary over :class:`~akosha.storage.span_query.SpanQuery`. The SQL,
the keyset cursor, and the "table may not exist yet" contract all live in R-1;
this module contributes exactly four things the boundary needs and nothing
else:

* **the shared store.** The tool reads the lifespan's ``HotStore`` via
  ``get_shared_hot_store()`` and never opens a second connection (spec §4.1).
  A per-tool connection would be a second writer-facing handle on the same
  DuckDB file and would drift from whatever the receiver has already flushed.
* **the empty contract.** No store, or no table, is ``[]`` — not an error.
* **the limit clamp.** An unbounded page is the egress risk spec §7 Q8 raises.
* **the cursor wire format.** MCP has no tuple, so the keyset cursor
  ``(received_at_unix_nano, span_id)`` crosses as ``"<int>:<span_id>"``.

R-3 adds the aggregate sibling in this same registrar.
"""

from __future__ import annotations

from typing import Any

from akosha.storage.span_query import SpanQuery

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "register_otel_span_tools",
]


def _format_cursor(received_at_unix_nano: int, span_id: str) -> str:
    """Encode a keyset cursor for the MCP boundary.

    The inverse of :func:`_parse_cursor`. Clients build this from the last row
    of a page — the tool returns hits, not a next-cursor, so the format is
    part of this tool's documented contract.
    """
    return f"{int(received_at_unix_nano)}:{span_id}"


def _parse_cursor(cursor: str | None) -> tuple[int, str] | None:
    """Decode a keyset cursor. ``None`` means "first page".

    Raises:
        ValueError: the cursor is malformed. This is deliberately loud — a bad
            cursor that degrades to ``None`` would silently serve page one
            again, and the client would loop forever.
    """
    if cursor is None:
        return None
    # Split on the FIRST colon: the left side is a decimal integer and can
    # never contain one, while a span_id is opaque and may. rpartition would
    # hand the tail to the integer instead and reject every id with a colon.
    received, sep, span_id = cursor.partition(":")
    if not sep or not received or not span_id:
        raise ValueError(f"cursor must be '<received_at_unix_nano>:<span_id>', got {cursor!r}")
    try:
        return int(received), span_id
    except ValueError:
        raise ValueError(
            f"cursor's received_at_unix_nano must be an integer, got {received!r}"
        ) from None


def register_otel_span_tools(app: Any) -> None:
    """Register the ``spans_substrate`` read tools.

    Mirrors ``fitness_tools.register_fitness_tools``: ``@app.tool(name=...)``
    inside a ``register_*_tools(app)`` function, no module-level ``mcp``.
    """

    @app.tool(name="search_spans")
    async def search_spans(
        service_name: str | None = None,
        service_namespace: str | None = None,
        outcome: str | None = None,
        since_unix_nano: int | None = None,
        until_unix_nano: int | None = None,
        trace_id: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search stored OpenTelemetry spans, newest first.

        Filters are exact-match and all optional; an omitted filter is not
        applied. ``since_unix_nano`` is inclusive, ``until_unix_nano``
        exclusive, both against ``received_at_unix_nano`` (not span start).

        Pagination is keyset-based. To fetch the next page, pass a ``cursor``
        built from the last row of this one as
        ``"<received_at_unix_nano>:<span_id>"`` — for example
        ``"1700000000000000000:9f2c1a"``. A malformed cursor is an error;
        it never silently restarts at page one. Rows ingested after a page
        was served are newer than that page's cursor and will appear in a
        later call, not a repeated one.

        ``limit`` is clamped to [1, 500].

        Returns:
            The matching spans, newest first, as dicts. An empty list means
            nothing matched, which includes the case where no spans have been
            received yet — it is never an error.
        """
        # Imported here, not at module scope: ``akosha.mcp.server`` reaches
        # this module through the profile dispatch (profiles -> group_registers
        # -> here), so a top-level import closes the cycle.
        from akosha.mcp.server import get_shared_hot_store

        store = get_shared_hot_store()
        if store is None:
            # The receiver has not started, so the DDL has not run either.
            return []

        page = max(1, min(int(limit), MAX_PAGE_SIZE))
        hits = await SpanQuery(store).search(
            service_name=service_name,
            service_namespace=service_namespace,
            outcome=outcome,
            since_unix_nano=since_unix_nano,
            until_unix_nano=until_unix_nano,
            trace_id=trace_id,
            limit=page,
            cursor=_parse_cursor(cursor),
        )
        return [hit.model_dump() for hit in hits]
