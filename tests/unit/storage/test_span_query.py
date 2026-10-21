"""Tests for SpanQuery — the read surface over spans_substrate (read-path R-1).

The DDL below is a verbatim copy of spec §5.1's ``spans_substrate``. It is
duplicated here because the only production writer — the OTLP receiver, which
owns ``_ensure_operational_tables`` — is not built yet (v7 plan, PR-A2). When
that lands, these two copies must be updated together; ``SpanQuery`` reads
whatever shape the receiver creates, so a drift here shows up as a read path
that silently returns nothing.

Everything here writes through ``HotStore.conn`` directly rather than going
through the receiver, because there is no receiver yet to ingest through.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from akosha.storage.hot_store import HotStore
from akosha.storage.span_query import SpanQuery

if TYPE_CHECKING:
    from pathlib import Path

# Spec §5.1, verbatim. Kept in sync with the receiver's DDL (v7 PR-A2).
SPANS_SUBSTRATE_DDL = """
CREATE TABLE IF NOT EXISTS spans_substrate (
    span_id                   TEXT PRIMARY KEY,
    trace_id                  TEXT,
    parent_span_id            TEXT,
    service_name              TEXT         NOT NULL,
    service_namespace         TEXT         NOT NULL,
    deployment_environment    TEXT,
    telemetry_source          TEXT,
    telemetry_source_type     TEXT,
    span_name                 TEXT         NOT NULL,
    start_time_unix_nano      BIGINT       NOT NULL,
    end_time_unix_nano        BIGINT,
    received_at_unix_nano     BIGINT       NOT NULL,
    duration_ms               REAL,
    outcome                   TEXT,
    selector                  TEXT,
    task_class                TEXT,
    latency_ms                REAL,
    gen_ai_provider_name      TEXT,
    gen_ai_request_model      TEXT,
    gen_ai_response_model     TEXT,
    gen_ai_usage_input_tokens INTEGER,
    gen_ai_usage_output_tokens INTEGER,
    gen_ai_usage_cost         NUMERIC(10, 6),
    status_code               TEXT,
    status_message            TEXT,
    attributes                JSON         NOT NULL,
    events                    JSON,
    resource                  JSON         NOT NULL,
    has_gen_ai_attrs          BOOLEAN      NOT NULL DEFAULT FALSE,
    has_dispatch_attrs        BOOLEAN      NOT NULL DEFAULT FALSE,
    inserted_at               TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at                TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_span_identity UNIQUE (trace_id, span_id)
);
"""

BASE_RECEIVED = 1_700_000_000_000_000_000


@pytest.fixture
async def store(tmp_path: Path) -> HotStore:
    """A HotStore carrying the spans_substrate DDL.

    ``embedding_dim`` is required: omitting it makes ``__init__`` call the live
    ``resolve_embedding_dim(get_embedding_service())`` path.
    """
    hs = HotStore(database_path=tmp_path / "q.duckdb", embedding_dim=384)
    await hs.initialize()
    hs.conn.execute(SPANS_SUBSTRATE_DDL)
    return hs


def _insert(
    hs: HotStore,
    span_id: str,
    *,
    selector: str | None = None,
    task_class: str | None = None,
    outcome: str = "success",
    latency: float | None = 10.0,
    received: int | None = None,
) -> None:
    """Insert one span. ``conn.execute`` is synchronous — never await it."""
    hs.conn.execute(
        "INSERT INTO spans_substrate (span_id, trace_id, service_name, "
        "service_namespace, span_name, start_time_unix_nano, "
        "received_at_unix_nano, outcome, selector, task_class, latency_ms, "
        "attributes, resource) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            span_id,
            f"trace-{span_id}",
            "mahavishnu",
            "bodai",
            "n",
            1,
            received if received is not None else BASE_RECEIVED,
            outcome,
            selector,
            task_class,
            latency,
            json.dumps({"k": "v"}),
            json.dumps({"service.name": "mahavishnu"}),
        ],
    )


@pytest.mark.asyncio
async def test_search_returns_hits(store: HotStore) -> None:
    _insert(store, "s1")
    hits = await SpanQuery(store).search(limit=10)
    assert [h.span_id for h in hits] == ["s1"]
    assert hits[0].attributes == {"k": "v"}


@pytest.mark.asyncio
async def test_search_missing_table_is_empty_not_error(tmp_path: Path) -> None:
    """Review Focus #1: the receiver creates the DDL. Before it has run once,
    a read must not raise — it returns empty (spec §5.5)."""
    hs = HotStore(database_path=tmp_path / "bare.duckdb", embedding_dim=384)
    await hs.initialize()
    hs.conn.execute("DROP TABLE IF EXISTS spans_substrate")
    assert await SpanQuery(hs).search(limit=10) == []


@pytest.mark.asyncio
async def test_summary_missing_table_is_zeroed(tmp_path: Path) -> None:
    """The same missing-table case for summary: zero counts, no p99."""
    hs = HotStore(database_path=tmp_path / "bare2.duckdb", embedding_dim=384)
    await hs.initialize()
    got = await SpanQuery(hs).summary(
        selector="x", task_class="agent", cutoff_unix_nano=BASE_RECEIVED
    )
    assert got.span_count == 0
    assert got.failure_count == 0
    assert got.latency_p99_ms is None


@pytest.mark.asyncio
async def test_search_keyset_pages_without_duplicates_or_gaps(
    store: HotStore,
) -> None:
    """Review Focus #4: an insert above the cursor mid-pagination.

    OFFSET would return sp00 twice and never return sp99. Keyset must not.
    """
    for i in range(6):
        _insert(store, f"sp{i:02d}", received=BASE_RECEIVED + i)
    q = SpanQuery(store)

    first = await q.search(limit=3)
    _insert(store, "sp99", received=BASE_RECEIVED - 1)  # newest, above cursor
    second = await q.search(
        limit=3,
        cursor=(first[-1].received_at_unix_nano, first[-1].span_id),
    )

    ids = [h.span_id for h in first] + [h.span_id for h in second]
    assert len(ids) == len(set(ids)), f"duplicate across pages: {ids}"
    assert "sp99" not in ids, "a row inserted above the cursor leaked in"


@pytest.mark.asyncio
async def test_search_ties_on_received_at_still_page_completely(
    store: HotStore,
) -> None:
    """A batch stamps the SAME received_at_unix_nano on every span.

    Ties are the normal case, not an edge case — without span_id in the
    ORDER BY, the keyset cursor is not total and rows are lost or repeated.
    """
    for i in range(6):
        _insert(store, f"tie{i}", received=BASE_RECEIVED)
    q = SpanQuery(store)

    seen: list[str] = []
    cursor: tuple[int, str] | None = None
    for _ in range(5):
        page = await q.search(limit=2, cursor=cursor)
        if not page:
            break
        seen.extend(h.span_id for h in page)
        cursor = (page[-1].received_at_unix_nano, page[-1].span_id)

    assert sorted(seen) == [f"tie{i}" for i in range(6)], f"got {seen}"


@pytest.mark.asyncio
async def test_search_filters_are_applied(store: HotStore) -> None:
    _insert(store, "keep", outcome="error", received=BASE_RECEIVED + 2)
    _insert(store, "drop", outcome="success", received=BASE_RECEIVED + 1)
    hits = await SpanQuery(store).search(outcome="error", limit=10)
    assert [h.span_id for h in hits] == ["keep"]


@pytest.mark.asyncio
async def test_search_rejects_non_positive_limit(store: HotStore) -> None:
    with pytest.raises(ValueError, match="limit"):
        await SpanQuery(store).search(limit=0)


@pytest.mark.asyncio
async def test_summary_matches_null_selector(store: HotStore) -> None:
    """Review Focus #2: `= ?` drops NULL selector; IS NOT DISTINCT FROM matches.

    Verified against DuckDB: `selector = NULL` -> 0 rows,
    `selector IS NOT DISTINCT FROM NULL` -> matches.
    """
    for i in range(3):
        _insert(store, f"s{i}", selector=None, task_class="agent")
    got = await SpanQuery(store).summary(
        selector=None,
        task_class="agent",
        cutoff_unix_nano=BASE_RECEIVED - 1,
    )
    assert got.span_count == 3, "IS NOT DISTINCT FROM must match NULL selector"


@pytest.mark.asyncio
async def test_summary_all_null_latency_yields_none_not_zero(
    store: HotStore,
) -> None:
    """Review Focus #3: PERCENTILE_CONT excludes NULLs, so the whole-window
    NULL case must be None, never 0.0 — that distinction is what separates
    'no data' from 'fast'."""
    for i in range(3):
        _insert(store, f"s{i}", selector="x", task_class="agent", latency=None)
    got = await SpanQuery(store).summary(
        selector="x", task_class="agent", cutoff_unix_nano=BASE_RECEIVED - 1
    )
    assert got.latency_p99_ms is None
    assert got.failure_count == 0, "COALESCE must keep failure_count an int"
    assert got.span_count == 3


@pytest.mark.asyncio
async def test_summary_counts_failures_and_respects_cutoff(store: HotStore) -> None:
    """Review Focus #5: span_count=0 must be distinguishable from a quiet
    window, and a failure inside the window must be counted."""
    _insert(store, "old", selector="x", task_class="agent", outcome="error")
    _insert(store, "new", selector="x", task_class="agent", outcome="error")
    _insert(
        store,
        "recent",
        selector="x",
        task_class="agent",
        outcome="error",
        received=BASE_RECEIVED + 1000,
    )
    got = await SpanQuery(store).summary(
        selector="x", task_class="agent", cutoff_unix_nano=BASE_RECEIVED
    )
    # Cutoff is exclusive (received > cutoff), so "old"/"new" at BASE_RECEIVED
    # fall out and only the strictly-newer row survives.
    assert got.span_count == 1
    assert got.failure_count == 1
