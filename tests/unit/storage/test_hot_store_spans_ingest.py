"""``HotStore.insert_spans_substrate`` / ``insert_dlq_rows`` — PR-A3.

These are the only two methods that write to the substrate on the ingest
path, and both run against a real DuckDB rather than a stubbed connection.
Both are statement-shaped code: the ``ON CONFLICT`` clause and the JSON
column binding only fail at parse time, and a fake connection would happily
accept either.

The counts returned by ``insert_spans_substrate`` are the load-bearing part
of this file. They are the only signal an operator has that distinguishes
"the receiver is receiving new spans" from "the receiver is receiving spans
it has already seen", and both were wrong in a plausible first
implementation.
"""

from __future__ import annotations

from typing import Any

import pytest

from akosha.storage.hot_store import HotStore


@pytest.fixture
async def store() -> HotStore:
    hs = HotStore(database_path=":memory:", embedding_dim=384)
    await hs.initialize()
    return hs


def _span(
    span_id: str,
    trace_id: str = "t1",
    *,
    service_name: str = "mahavishnu",
) -> dict[str, Any]:
    """A minimal row satisfying every NOT NULL column of the substrate."""
    return {
        "span_id": span_id,
        "trace_id": trace_id,
        "service_name": service_name,
        "service_namespace": "bodai",
        "span_name": "unit-test-span",
        "start_time_unix_nano": 1,
        "received_at_unix_nano": 2,
        "attributes": "{}",
        "resource": "{}",
    }


def _count(store: HotStore, sql: str) -> int:
    row = store.conn.execute(sql).fetchone()  # type: ignore[union-attr]
    return int(row[0])


# ── insert_spans_substrate ───────────────────────────────────────────────


async def test_empty_batch_is_a_noop_not_an_error(store: HotStore) -> None:
    """A producer that exports zero spans must not cost an empty INSERT."""
    assert await store.insert_spans_substrate([]) == (0, 0)
    assert _count(store, "SELECT count(*) FROM spans_substrate") == 0


async def test_fresh_rows_all_count_as_inserted(store: HotStore) -> None:
    rows = [_span("a"), _span("b"), _span("c", trace_id="t2")]
    inserted, deduped = await store.insert_spans_substrate(rows)
    assert (inserted, deduped) == (3, 0)
    assert _count(store, "SELECT count(*) FROM spans_substrate") == 3


async def test_replaying_a_batch_counts_as_deduped_not_inserted(
    store: HotStore,
) -> None:
    """The regression this file exists for.

    Reading the affected identities back AFTER the INSERT cannot tell "I
    wrote this row" from "this row was already present", so a naive
    implementation reports a pure replay as 3 inserts and 0 dedups. An
    operator watching that counter sees a healthy-looking ingest forever.
    """
    rows = [_span("a"), _span("b"), _span("c")]
    await store.insert_spans_substrate(rows)
    inserted, deduped = await store.insert_spans_substrate(rows)
    assert (inserted, deduped) == (0, 3)
    assert _count(store, "SELECT count(*) FROM spans_substrate") == 3


async def test_a_span_id_collision_across_trace_ids_is_deduped_not_fatal(
    store: HotStore,
) -> None:
    """Two spans sharing a ``span_id`` under different ``trace_id``s.

    This is the case §5.1's DDL gets wrong. The table declares BOTH
    ``span_id TEXT PRIMARY KEY`` and ``UNIQUE (trace_id, span_id)``. The PK
    is narrower, so it fires first and the composite constraint can never be
    the one that rejects anything — which means the spec's own prescribed
    ``ON CONFLICT (trace_id, span_id)`` clause handles only a constraint that
    is unreachable, and this case raises ``ConstraintException`` on the hot
    ingest path instead of being a no-op.

    The untargeted clause used here honors both. The count must agree with
    what the database actually did: probing the composite pair would predict
    "inserted" while the row was silently dropped, and the two numbers would
    disagree — which is worse than either number being wrong alone.
    """
    rows = [_span("shared", "t1"), _span("shared", "t2")]
    inserted, deduped = await store.insert_spans_substrate(rows)
    assert (inserted, deduped) == (1, 1)
    assert _count(store, "SELECT count(*) FROM spans_substrate") == 1


async def test_duplicates_within_one_batch_are_counted_as_deduped(
    store: HotStore,
) -> None:
    """The pre-insert existence check alone would miss an in-batch duplicate.

    Neither copy is in the table when the batch starts, so sampling existence
    before the INSERT reports both as fresh — but only one can win. Folding
    each classified ``span_id`` into a ``seen`` set is what catches the second.
    """
    rows = [_span("a"), _span("a"), _span("b")]
    inserted, deduped = await store.insert_spans_substrate(rows)
    assert (inserted, deduped) == (2, 1)
    assert _count(store, "SELECT count(*) FROM spans_substrate") == 2


async def test_a_null_trace_id_still_dedups(store: HotStore) -> None:
    """``trace_id`` is nullable, and ``NULL = NULL`` is never true in SQL.

    A probe written ``WHERE trace_id = ?`` would report every null-trace span
    as new on every batch, so the dedup counter would sit at zero for a
    producer that never sets ``trace_id`` — the receiver would look healthy
    while its dedup total never moved. ``IS NOT DISTINCT FROM`` is null-safe.
    """
    span = _span("a")
    span["trace_id"] = None
    assert await store.insert_spans_substrate([span]) == (1, 0)
    assert await store.insert_spans_substrate([span]) == (0, 1)
    assert _count(store, "SELECT count(*) FROM spans_substrate") == 1


async def test_a_row_missing_a_not_null_column_raises(store: HotStore) -> None:
    """Validation is the receiver's job; the constraint is the backstop.

    If ``pre_validate`` ever lets a row through, DuckDB must refuse it rather
    than storing a span the read path will later choke on.
    """
    bad = _span("bad")
    del bad["service_name"]
    with pytest.raises(Exception, match="(?i)not null"):
        await store.insert_spans_substrate([bad])


# ── insert_dlq_rows ──────────────────────────────────────────────────────


async def test_dlq_empty_batch_is_a_noop(store: HotStore) -> None:
    assert await store.insert_dlq_rows([]) == 0


async def test_dlq_accepts_two_rows_sharing_an_identity(store: HotStore) -> None:
    """The DLQ table has no primary key, and must not grow one.

    These rows already failed a uniqueness constraint on their way into
    ``spans_substrate``. Quarantining them under a key that enforces the same
    uniqueness would make the second rejection uncatchable — the insert would
    raise, the receiver's DLQ wrapper would log it, and the second span would
    be lost with no record anywhere.
    """
    row = {
        "reason": "not_null",
        "field": "service_name",
        "row_json": "{}",
        "trace_id": "t1",
        "span_id": "s1",
    }
    assert await store.insert_dlq_rows([dict(row), dict(row)]) == 2
    assert _count(store, "SELECT count(*) FROM _dlq_rows") == 2


async def test_dlq_defaults_rejected_at_when_absent(store: HotStore) -> None:
    """The retention sweep filters on ``rejected_at``; it must be server-set.

    A receiver that had to supply its own timestamp could skew it, and a
    clock-skewed quarantine row would be deleted early or kept forever.
    """
    await store.insert_dlq_rows([{"reason": "enum", "row_json": "{}"}])
    nulls = _count(store, "SELECT count(*) FROM _dlq_rows WHERE rejected_at IS NULL")
    assert nulls == 0


async def test_dlq_failure_raises_so_the_receiver_can_decide(store: HotStore) -> None:
    """A substrate failure propagates.

    The receiver wraps this call (§15: "DLQ failure does NOT abort the batch")
    — but that wrapping is the receiver's contract. Swallowing the error here
    would remove the receiver's ability to notice it at all.
    """
    with pytest.raises(Exception):
        await store.insert_dlq_rows([{"reason": None, "row_json": "{}"}])