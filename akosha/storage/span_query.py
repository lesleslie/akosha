"""Read-only query surface over ``spans_substrate`` (read-path R-1).

Borrows the receiver's :class:`~akosha.storage.hot_store.HotStore` connection.
Opens no second connection, introduces no ORM, and never writes — the only
writer is the OTLP receiver (spec §5.1).

This module registers nothing. R-2 exposes :meth:`SpanQuery.search` and R-3
exposes :meth:`SpanQuery.summary` as MCP tools; until then the only callers are
tests.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from .hot_store import HotStore

__all__ = ["SpanHit", "SpanQuery", "SpanSummary"]


class SpanHit(BaseModel):
    """One span, as stored. Field order matches the read projection."""

    span_id: str
    trace_id: str | None  # DDL: `trace_id TEXT`, nullable
    parent_span_id: str | None
    service_name: str
    service_namespace: str  # DDL: NOT NULL
    span_name: str
    start_time_unix_nano: int
    received_at_unix_nano: int
    duration_ms: float | None
    outcome: str | None
    attributes: dict[str, Any]


class SpanSummary(BaseModel):
    """Aggregate over a (selector, task_class) pair since a cutoff."""

    span_count: int
    failure_count: int
    latency_p99_ms: float | None


_SELECT_COLS = (
    "span_id, trace_id, parent_span_id, service_name, service_namespace, "
    "span_name, start_time_unix_nano, received_at_unix_nano, duration_ms, "
    "outcome, attributes"
)

# ORDER BY includes span_id so the keyset cursor is total. A batch stamps the
# same received_at_unix_nano on every span, so ties are the NORMAL case, not an
# edge case — without the tiebreaker, paging repeats and drops rows.
_ORDER = "ORDER BY received_at_unix_nano DESC, span_id DESC"


def _coerce_json(value: Any) -> dict[str, Any]:
    """Normalise a JSON column to a dict.

    DuckDB's JSON type surfaces as ``str`` on some builds and as a decoded
    object on others, so the read path accepts either rather than binding the
    read path to one build's representation.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str | bytes):
        return json.loads(value)
    return {}


def _to_hit(row: tuple) -> SpanHit:
    return SpanHit(
        span_id=row[0],
        trace_id=row[1],
        parent_span_id=row[2],
        service_name=row[3],
        service_namespace=row[4],
        span_name=row[5],
        start_time_unix_nano=row[6],
        received_at_unix_nano=row[7],
        duration_ms=row[8],
        outcome=row[9],
        attributes=_coerce_json(row[10]),
    )


class SpanQuery:
    """Read-only queries over spans_substrate.

    Borrows the receiver's HotStore. Does not open its own connection and
    never writes -- see spec §4.1 and §5.1.
    """

    def __init__(self, hot_store: HotStore) -> None:
        self._store = hot_store

    def _table_exists(self) -> bool:
        """Whether the receiver has created the DDL yet.

        The table belongs to the receiver (``_ensure_operational_tables``). A
        read issued before that has ever run must return empty rather than
        raise a CatalogError -- spec §5.5.
        """
        return bool(
            self._store.conn.execute(
                "SELECT 1 FROM duckdb_tables() WHERE table_name = ?",
                ["spans_substrate"],
            ).fetchone()
        )

    async def search(
        self,
        *,
        service_name: str | None = None,
        service_namespace: str | None = None,
        outcome: str | None = None,
        since_unix_nano: int | None = None,
        until_unix_nano: int | None = None,
        trace_id: str | None = None,
        limit: int = 50,
        cursor: tuple[int, str] | None = None,
    ) -> list[SpanHit]:
        """Newest-first keyset page over spans.

        Args:
            service_name: Exact match, or ``None`` to leave unfiltered.
            service_namespace: Exact match.
            outcome: Exact match, e.g. ``"error"``.
            since_unix_nano: Inclusive lower bound on ``received_at_unix_nano``.
            until_unix_nano: Exclusive upper bound.
            trace_id: Exact match.
            limit: Page size; must be >= 1.
            cursor: ``(received_at_unix_nano, span_id)`` from the previous
                page's last row. Omit for the first page.

        Returns:
            At most ``limit`` hits, newest first, or ``[]`` when the table does
            not exist yet.
        """
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        if not self._table_exists():
            return []

        conditions: list[str] = []
        params: list[Any] = []
        if service_name is not None:
            conditions.append("service_name = ?")
            params.append(service_name)
        if service_namespace is not None:
            conditions.append("service_namespace = ?")
            params.append(service_namespace)
        if outcome is not None:
            conditions.append("outcome = ?")
            params.append(outcome)
        if since_unix_nano is not None:
            conditions.append("received_at_unix_nano >= ?")
            params.append(int(since_unix_nano))
        if until_unix_nano is not None:
            conditions.append("received_at_unix_nano < ?")
            params.append(int(until_unix_nano))
        if trace_id is not None:
            conditions.append("trace_id = ?")
            params.append(trace_id)
        if cursor is not None:
            # Row-value comparison: total order over (received, span_id), so
            # ties on received_at page correctly instead of looping forever.
            conditions.append("(received_at_unix_nano, span_id) < (?, ?)")
            params.extend([int(cursor[0]), cursor[1]])

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = f"SELECT {_SELECT_COLS} FROM spans_substrate {where} {_ORDER} LIMIT ?"
        params.append(limit)

        async with self._store._lock:
            rows = self._store.conn.execute(sql, params).fetchall()
        return [_to_hit(r) for r in rows]

    async def summary(
        self,
        *,
        selector: str | None,
        task_class: str | None,
        cutoff_unix_nano: int,
    ) -> SpanSummary:
        """Aggregate over rows matching the pair with ``received_at`` > cutoff.

        ``selector`` and ``task_class`` accept ``None`` to mean "NULL in the
        column", which is why the predicate is ``IS NOT DISTINCT FROM`` rather
        than ``=``: ``selector = NULL`` evaluates to NULL and silently drops
        every row, under-reporting ``span_count``.
        """
        if not self._table_exists():
            return SpanSummary(span_count=0, failure_count=0, latency_p99_ms=None)

        # COALESCE: SUM(CASE ...) returns NULL on zero rows, which would fail
        # the int field. PERCENTILE_CONT excludes NULLs, so p99 is None only
        # when the whole window lacks latency -- that is the distinction between
        # "no data" and "fast".
        sql = """
            SELECT
                COUNT(*) AS span_count,
                COALESCE(SUM(CASE WHEN outcome = 'error' THEN 1 ELSE 0 END), 0)
                    AS failure_count,
                PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY latency_ms)
                    AS latency_p99_ms
            FROM spans_substrate
            WHERE selector IS NOT DISTINCT FROM $selector
              AND task_class IS NOT DISTINCT FROM $task_class
              AND outcome IN ('success', 'error')
              AND received_at_unix_nano > $cutoff
        """
        async with self._store._lock:
            row = self._store.conn.execute(
                sql,
                {
                    "selector": selector,
                    "task_class": task_class,
                    "cutoff": int(cutoff_unix_nano),
                },
            ).fetchone()

        return SpanSummary(
            span_count=int(row[0]),
            failure_count=int(row[1]),
            latency_p99_ms=None if row[2] is None else float(row[2]),
        )
