"""Read-only count over ``metrics_substrate`` (the metric path's read side).

Borrowed from :class:`~akosha.storage.hot_store.HotStore` the way
:class:`~akosha.storage.span_query.SpanQuery` borrows it: no second
connection, no ORM, and the store's own lock so a concurrent write cannot
tear the read. It lives in its own module because
``akosha/storage/hot_store.py`` is deliberately small — the CI test
``tests/unit/test_hot_store_subclass.py`` caps it at 500 lines, and the
module is "the code-graph overlay", not a query surface.

Why the count exists at all: the ``/health`` metrics feed reports
``entities_count``, and the DURABLE row count is the ground truth. The
Prometheus exposure is a separate path that keeps working when the
durable write does not, so a live series on ``/metrics`` says nothing
about whether anything is being stored. The count is what tells the two
apart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .hot_store import HotStore

__all__ = ["count_metrics_points"]


async def count_metrics_points(store: HotStore, limit: int = 1000) -> int:
    """The number of metric points currently persisted, bounded by ``limit``.

    Bounded for the same reason ``query_traces`` is: the health probe runs
    on the request path, and an unbounded COUNT over a table that grows
    for thirty days is a latency risk on ``/health``. Past the limit the
    count is a LOWER BOUND, which is what the feed needs — "is anything
    there", not "exactly how much".

    Raises when the table does not exist: a oneiric that predates the
    metrics substrate has no such table, and the caller treats that as
    "no metrics persisted" rather than as an outage of its own.
    """
    async with store._lock:
        if not store.conn:
            raise RuntimeError("Hot store not initialized")
        row = store.conn.execute(
            "SELECT count(*) FROM (SELECT 1 FROM metrics_substrate LIMIT ?)",
            [limit],
        ).fetchone()
        return int(row[0]) if row else 0
