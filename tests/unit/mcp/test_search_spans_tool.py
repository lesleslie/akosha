"""Tests for the ``search_spans`` MCP tool (read-path R-2).

R-2's job is the *boundary* work: R-1 proved ``SpanQuery.search`` reads spans
correctly, and these tests pin what the MCP layer adds on top — registration,
the empty-is-not-an-error contract, the shared-store seam, the limit clamp, and
the cursor wire format.

Real ``FastMCP`` throughout (not ``DummyFastMCP``). This tool is the read path
for the whole substrate; a test that only proves "a function got called on a
stub" would not catch a name that FastMCP rejects or a return type it cannot
serialise. FastMCP 4.1.1 is the installed API: ``list_tools()`` / ``call_tool()``
returning a ``ToolResult`` whose ``structured_content`` wraps the payload.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from fastmcp import FastMCP

from akosha.mcp.server import clear_shared_services, set_shared_hot_store
from akosha.mcp.tools.otel_span_tools import (
    MAX_PAGE_SIZE,
    _format_cursor,
    _parse_cursor,
    register_otel_span_tools,
)
from akosha.storage.hot_store import HotStore
from tests._support.spans_ddl import SPANS_SUBSTRATE_DDL

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

BASE_RECEIVED = 1_700_000_000_000_000_000


@pytest.fixture(autouse=True)
def _no_leaked_shared_store() -> Iterator[None]:
    """The shared HotStore is a module-level global in ``akosha.mcp.server``.

    Publish one and forget to clear it and every later test in the session
    silently reads this test's database — a leak that shows up as
    cross-test coupling, not as a failure here.
    """
    clear_shared_services()
    yield
    clear_shared_services()


@pytest.fixture
async def store(tmp_path: Path) -> HotStore:
    """A HotStore carrying the DDL, published the way the lifespan does."""
    hs = HotStore(database_path=tmp_path / "spans.duckdb", embedding_dim=384)
    await hs.initialize()
    hs.conn.execute(SPANS_SUBSTRATE_DDL)
    set_shared_hot_store(hs)
    return hs


@pytest.fixture
def app() -> FastMCP:
    mcp = FastMCP("test-spans")
    register_otel_span_tools(mcp)
    return mcp


def _insert(
    hs: HotStore,
    span_id: str,
    *,
    service_name: str = "mahavishnu",
    outcome: str = "success",
    received: int = BASE_RECEIVED,
) -> None:
    """Insert one span. ``conn.execute`` is synchronous — never await it."""
    hs.conn.execute(
        "INSERT INTO spans_substrate (span_id, trace_id, service_name, "
        "service_namespace, span_name, start_time_unix_nano, "
        "received_at_unix_nano, outcome, attributes, resource) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            span_id,
            f"trace-{span_id}",
            service_name,
            "bodai",
            "op",
            1,
            received,
            outcome,
            json.dumps({"k": "v"}),
            json.dumps({"service.name": service_name}),
        ],
    )


async def _call(app: FastMCP, **kwargs: Any) -> list[dict[str, Any]]:
    """Invoke through the MCP boundary and unwrap the structured payload."""
    result = await app.call_tool("search_spans", kwargs)
    return list(result.structured_content["result"])


# --- registration ---------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_is_registered(app: FastMCP) -> None:
    names = [t.name for t in await app.list_tools()]
    assert "search_spans" in names


# --- §5.2: quiet is not broken -------------------------------------------


@pytest.mark.asyncio
async def test_no_store_returns_empty_not_error(app: FastMCP) -> None:
    """No shared store means the receiver has not started, so the DDL has not
    run either. That is "no data yet", not a failure — conflating the two
    turns a healthy starting service into a page (spec §5.2)."""
    assert await _call(app) == []


@pytest.mark.asyncio
async def test_missing_table_returns_empty_not_error(app: FastMCP, store: HotStore) -> None:
    """Review Focus #1: a store can exist while the table does not."""
    store.conn.execute("DROP TABLE IF EXISTS spans_substrate")
    assert await _call(app) == []


# --- happy path -----------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_stored_spans(app: FastMCP, store: HotStore) -> None:
    _insert(store, "s1")
    hits = await _call(app)
    assert [h["span_id"] for h in hits] == ["s1"]
    # Review Focus #6: `attributes` is JSON in DuckDB; the boundary hands
    # back a real dict, not a string the caller has to re-parse.
    assert hits[0]["attributes"] == {"k": "v"}


@pytest.mark.asyncio
async def test_filters_are_applied(app: FastMCP, store: HotStore) -> None:
    _insert(store, "s1", service_name="mahavishnu", outcome="success")
    _insert(store, "s2", service_name="mahavishnu", outcome="error")
    _insert(store, "s3", service_name="session-buddy", outcome="success")

    assert [h["span_id"] for h in await _call(app, outcome="error")] == ["s2"]
    assert [h["span_id"] for h in await _call(app, service_name="session-buddy")] == ["s3"]


@pytest.mark.asyncio
async def test_time_window_bounds_are_inclusive_then_exclusive(
    app: FastMCP,
    store: HotStore,
) -> None:
    _insert(store, "old", received=BASE_RECEIVED - 10)
    _insert(store, "edge", received=BASE_RECEIVED)
    _insert(store, "new", received=BASE_RECEIVED + 10)

    hits = await _call(
        app,
        since_unix_nano=BASE_RECEIVED,
        until_unix_nano=BASE_RECEIVED + 10,
    )
    assert [h["span_id"] for h in hits] == ["edge"]


# --- pagination -----------------------------------------------------------


@pytest.mark.asyncio
async def test_cursor_pages_without_repeats_or_gaps(
    app: FastMCP,
    store: HotStore,
) -> None:
    """Review Focus #4: the receiver is ingesting while you read, and a batch
    stamps one ``received_at_unix_nano`` across every span in it — so ties are
    the normal case and ``span_id`` is what makes the cursor total."""
    for sid in ("s1", "s2", "s3"):
        _insert(store, sid)

    first = await _call(app, limit=2)
    assert [h["span_id"] for h in first] == ["s3", "s2"]

    last = first[-1]
    second = await _call(
        app,
        limit=2,
        cursor=_format_cursor(int(last["received_at_unix_nano"]), last["span_id"]),
    )
    assert [h["span_id"] for h in second] == ["s1"]

    assert not {h["span_id"] for h in first} & {h["span_id"] for h in second}


@pytest.mark.asyncio
async def test_rows_inserted_above_the_cursor_do_not_shift_the_page(
    app: FastMCP,
    store: HotStore,
) -> None:
    """A span ingested mid-pagination is newer than the cursor, so it belongs
    to a page the client already passed — it must not reappear or push a
    not-yet-seen row off the end."""
    for sid in ("s1", "s2", "s3"):
        _insert(store, sid)

    first = await _call(app, limit=2)
    last = first[-1]
    _insert(store, "s0-newest", received=BASE_RECEIVED + 1)

    second = await _call(
        app,
        limit=2,
        cursor=_format_cursor(int(last["received_at_unix_nano"]), last["span_id"]),
    )
    assert [h["span_id"] for h in second] == ["s1"]


# --- limit clamp ----------------------------------------------------------


@pytest.mark.asyncio
async def test_limit_is_clamped_to_the_ceiling(app: FastMCP, store: HotStore) -> None:
    """An unbounded page is the egress risk spec §7 Q8 raises."""
    for i in range(3):
        _insert(store, f"s{i}", received=BASE_RECEIVED + i)
    hits = await _call(app, limit=10**9)
    assert len(hits) == 3  # everything inserted, nothing invented

    # A limit below 1 clamps up rather than raising or returning a page the
    # keyset cannot express.
    assert len(await _call(app, limit=0)) == 1
    assert len(await _call(app, limit=-5)) == 1


@pytest.mark.asyncio
async def test_page_size_ceiling_is_positive() -> None:
    """Not a behaviour test — a tripwire on the constant itself."""
    assert MAX_PAGE_SIZE >= 1


# --- cursor wire format ---------------------------------------------------


def test_cursor_round_trips() -> None:
    assert _parse_cursor(_format_cursor(BASE_RECEIVED, "abc123")) == (
        BASE_RECEIVED,
        "abc123",
    )


def test_cursor_none_means_first_page() -> None:
    assert _parse_cursor(None) is None


def test_cursor_splits_on_the_first_colon_only() -> None:
    """The integer side is colon-free and the span_id is opaque, so the split
    has to happen at the first colon. Splitting at the last one would hand
    ``1700...:aa`` to ``int()`` and reject every id containing a colon."""
    assert _parse_cursor("1700000000000000000:aa:bb") == (BASE_RECEIVED, "aa:bb")


@pytest.mark.parametrize(
    "bad",
    ["garbage", "", ":", "1700", ":1700", "notanumber:abc", "1700:"],
)
def test_malformed_cursor_raises(bad: str) -> None:
    """Loudly, not by silently returning page one.

    A bad cursor that degrades to "no cursor" makes the client loop forever
    on the first page; the failure has to surface.
    """
    with pytest.raises(ValueError):
        _parse_cursor(bad)
