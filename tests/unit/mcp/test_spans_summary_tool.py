"""Tests for the ``spans_summary`` MCP tool (read-path R-3).

R-3 is the aggregate sibling of R-2's ``search_spans``, sharing the registrar.
The two things R-3 must get right that R-2 does not have to think about:

* **NULL is a value, not an absence.** A window where every span has NULL
  latency and a window with no spans at all both report ``span_count``, and
  only ``latency_p99_ms is None`` distinguishes them from "everything was
  fast". ``COALESCE`` and ``PERCENTILE_CONT``'s NULL-exclusion are what keep
  that distinction alive across the MCP boundary.
* **NULL selector/task_class match NULL rows.** The aggregate keys on a pair,
  and the pair is frequently half-NULL. ``selector = NULL`` evaluates to NULL
  and silently drops every such row, under-reporting the count.

Real ``FastMCP``, same as R-2's suite.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from fastmcp import FastMCP

from akosha.mcp.server import clear_shared_services, set_shared_hot_store
from akosha.mcp.tools.otel_span_tools import register_otel_span_tools
from akosha.storage.hot_store import HotStore
from tests._support.spans_ddl import SPANS_SUBSTRATE_DDL

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

BASE_RECEIVED = 1_700_000_000_000_000_000


@pytest.fixture(autouse=True)
def _no_leaked_shared_store() -> Iterator[None]:
    clear_shared_services()
    yield
    clear_shared_services()


@pytest.fixture
async def store(tmp_path: Path) -> HotStore:
    hs = HotStore(database_path=tmp_path / "summary.duckdb", embedding_dim=384)
    await hs.initialize()
    hs.conn.execute(SPANS_SUBSTRATE_DDL)
    set_shared_hot_store(hs)
    return hs


@pytest.fixture
def app() -> FastMCP:
    mcp = FastMCP("test-spans-summary")
    register_otel_span_tools(mcp)
    return mcp


def _insert(
    hs: HotStore,
    span_id: str,
    *,
    selector: str | None,
    task_class: str | None,
    outcome: str = "success",
    latency_ms: float | None = 10.0,
    received: int = BASE_RECEIVED,
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
            "op",
            1,
            received,
            outcome,
            selector,
            task_class,
            latency_ms,
            json.dumps({}),
            json.dumps({}),
        ],
    )


async def _call(app: FastMCP, **kwargs: Any) -> dict[str, Any]:
    """Invoke through the MCP boundary and unwrap the structured payload.

    A ``dict`` return type comes back *unwrapped* — FastMCP only nests a list
    return under a ``result`` key, because a bare array has no name to carry
    the structured-output schema.
    """
    result = await app.call_tool("spans_summary", kwargs)
    return dict(result.structured_content)


# --- registration ---------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_is_registered(app: FastMCP) -> None:
    names = [t.name for t in await app.list_tools()]
    assert "spans_summary" in names


# --- §5.2: quiet is not broken -------------------------------------------


@pytest.mark.asyncio
async def test_no_store_returns_zeroes_not_error(app: FastMCP) -> None:
    """Same contract as R-2: a substrate with no receiver is zeroed, not an
    error. An exception here would page on a healthy starting service."""
    assert await _call(app, selector="s", task_class="t", cutoff_unix_nano=0) == {
        "span_count": 0,
        "failure_count": 0,
        "latency_p99_ms": None,
    }


@pytest.mark.asyncio
async def test_missing_table_returns_zeroes_not_error(
    app: FastMCP,
    store: HotStore,
) -> None:
    store.conn.execute("DROP TABLE IF EXISTS spans_substrate")
    assert (await _call(app, selector="s", task_class="t", cutoff_unix_nano=0))["span_count"] == 0


# --- the aggregate --------------------------------------------------------


@pytest.mark.asyncio
async def test_summarizes_a_populated_pair(app: FastMCP, store: HotStore) -> None:
    for i in range(4):
        _insert(store, f"s{i}", selector="x", task_class="t", latency_ms=float(i + 1))
    _insert(store, "err", selector="x", task_class="t", outcome="error", latency_ms=99.0)

    summary = await _call(app, selector="x", task_class="t", cutoff_unix_nano=0)

    assert summary["span_count"] == 5
    assert summary["failure_count"] == 1
    # PERCENTILE_CONT excludes nothing here, so p99 lands at the top of the
    # distribution rather than at 99.0's exact neighbourhood -- the point is
    # that it is a number, not None.
    assert summary["latency_p99_ms"] is not None
    assert 90.0 <= summary["latency_p99_ms"] <= 99.0


@pytest.mark.asyncio
async def test_window_is_scoped_to_pairs_and_cutoff(app: FastMCP, store: HotStore) -> None:
    _insert(store, "mine", selector="x", task_class="t")
    _insert(store, "other-pair", selector="y", task_class="t")
    _insert(store, "before", selector="x", task_class="t", received=BASE_RECEIVED - 10)

    summary = await _call(
        app,
        selector="x",
        task_class="t",
        cutoff_unix_nano=BASE_RECEIVED - 5,
    )
    assert summary["span_count"] == 1


# --- Review Focus #5: silence vs absence ---------------------------------


@pytest.mark.asyncio
async def test_configured_pair_with_no_spans_is_zero_not_absent(
    app: FastMCP,
    store: HotStore,
) -> None:
    """§5.7's distinction, at the tool level: a pair with rows that all fall
    outside the cutoff reports 0 — it is *present and quiet*, which is what
    lets R-6 tell it apart from a pair it was never configured for."""
    _insert(store, "old", selector="x", task_class="t", received=BASE_RECEIVED - 1000)

    summary = await _call(
        app,
        selector="x",
        task_class="t",
        cutoff_unix_nano=BASE_RECEIVED,
    )

    assert summary["span_count"] == 0
    assert summary["failure_count"] == 0
    assert summary["latency_p99_ms"] is None


# --- Review Focus #3: no data is not fast --------------------------------


@pytest.mark.asyncio
async def test_all_null_latency_reports_none_not_zero(app: FastMCP, store: HotStore) -> None:
    """Every span in the window has NULL latency. The window is real -- rows
    were seen -- but there is no latency signal to report. Zero would be a lie
    telling the caller the work was instant."""
    for i in range(3):
        _insert(store, f"s{i}", selector="x", task_class="t", latency_ms=None)

    summary = await _call(app, selector="x", task_class="t", cutoff_unix_nano=0)

    assert summary["span_count"] == 3, "the rows are real even though latency is not"
    assert summary["latency_p99_ms"] is None


@pytest.mark.asyncio
async def test_partial_null_latency_excludes_them_from_p99(
    app: FastMCP,
    store: HotStore,
) -> None:
    """PERCENTILE_CONT excludes NULLs, so a handful of NULL-latency rows must
    not drag the p99 toward zero -- and must not inflate span_count either."""
    for i in range(5):
        _insert(store, f"ok{i}", selector="x", task_class="t", latency_ms=50.0)
    for i in range(100):
        _insert(store, f"null{i}", selector="x", task_class="t", latency_ms=None)

    summary = await _call(app, selector="x", task_class="t", cutoff_unix_nano=0)

    assert summary["span_count"] == 105
    assert summary["latency_p99_ms"] == 50.0


# --- nullable pair components --------------------------------------------


@pytest.mark.asyncio
async def test_null_selector_matches_null_rows(app: FastMCP, store: HotStore) -> None:
    """``IS NOT DISTINCT FROM``, not ``=``: ``selector = NULL`` is NULL, not
    TRUE, so a plain equality would drop every half-NULL pair and report a
    healthy zero."""
    _insert(store, "half-null", selector=None, task_class="t")
    _insert(store, "full", selector="x", task_class="t")

    summary = await _call(app, selector=None, task_class="t", cutoff_unix_nano=0)

    assert summary["span_count"] == 1, "only the NULL-selector row matches"


@pytest.mark.asyncio
async def test_null_task_class_matches_null_rows(app: FastMCP, store: HotStore) -> None:
    _insert(store, "half-null", selector="x", task_class=None)
    _insert(store, "full", selector="x", task_class="t")

    summary = await _call(app, selector="x", task_class=None, cutoff_unix_nano=0)

    assert summary["span_count"] == 1


# --- outcome population ---------------------------------------------------


@pytest.mark.asyncio
async def test_outcomes_outside_the_scored_set_are_not_counted(
    app: FastMCP,
    store: HotStore,
) -> None:
    """The aggregate scores success/error only. A third outcome (a span that
    was still running when it landed) is neither a success nor a failure and
    must not pad the denominator."""
    _insert(store, "ok", selector="x", task_class="t", outcome="success")
    _insert(store, "pending", selector="x", task_class="t", outcome="unsettled")

    summary = await _call(app, selector="x", task_class="t", cutoff_unix_nano=0)

    assert summary["span_count"] == 1
    assert summary["failure_count"] == 0
