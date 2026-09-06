"""End-to-end test: query_local_traces returns canned OTel data.

Wire-up discipline mandates a `tests/integration/test_<tool>_e2e.py`
for every registered MCP tool asserting non-empty results. This test
seeds a canned OTel HotRecord into a real HotStore, registers the
``query_local_traces`` tool against a stand-in MCP server shim, and
invokes the registered callable so the assertion path mirrors what
production dispatchers do.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import numpy as np
import pytest

from akosha.mcp.tools.otel_tools import register_otel_query_tools
from akosha.storage.hot_store import HotStore
from akosha.storage.models import HotRecord

from tests.fixtures.mock_bodai_mcp import make_canned_span


class _ToolRegistry:
    """Minimal MCP-server stand-in that captures ``app.tool()`` calls.

    Mirrors FastMCP's ``add_tool`` mechanism well enough for the
    tool-function body to be exercised end-to-end. Production code
    registers via ``@app.tool()`` decorating an async function
    inside ``register_otel_query_tools``; here we capture the inner
    function so we can invoke it directly.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Any] = {}

    def tool(self) -> Any:
        """Decorator factory that mimics FastMCP's ``@app.tool()``."""

        def _decorator(func: Any) -> Any:
            self._tools[func.__name__] = func
            return func

        return _decorator


@pytest.fixture
def hot_store() -> Any:
    """A real HotStore backed by an in-memory DuckDB."""
    store = HotStore(database_path=":memory:")
    return store


@pytest.mark.asyncio
@pytest.mark.slow
async def test_query_local_traces_returns_seeded_record(
    hot_store: HotStore,
) -> None:
    """End-to-end: HotRecord insert → query_local_traces tool → non-empty rows.

    Spins up just enough of the MCP tool registration machinery to
    invoke the registered ``query_local_traces`` callable, then
    verifies the canned HotRecord written into HotStore is returned
    with its metadata round-tripped.
    """
    # Seed the canned span with a recent timestamp so any downstream
    # ``since``-filter snapshots would pass it through. The HotStore
    # itself doesn't filter on timestamp; the timestamp on the
    # HotRecord is what we end up matching.
    recent_unix_nano = str(int(time.time_ns()))
    canned_span = make_canned_span(
        span_id="query_local_traces_e2e_canned",
        name="akosha.query_local_traces",
        start_unix_nano=recent_unix_nano,
        task_class="CODE_GENERATION",
    )

    await hot_store.initialize()

    start_unix_nano = int(recent_unix_nano)
    ts = datetime.fromtimestamp(start_unix_nano / 1_000_000_000, tz=UTC)
    record = HotRecord(
        system_id="akosha",
        conversation_id=f"akosha:{canned_span['spanId']}",
        content=json.dumps(
            {k: v for k, v in canned_span.items() if k not in ("traceId", "spanId")},
            sort_keys=True,
            default=str,
        ),
        embedding=np.zeros(384, dtype=np.float32).tolist(),
        timestamp=ts,
        metadata={
            "attributes": {"task_class": "CODE_GENERATION"},
            "otel": {
                "trace_id": canned_span["traceId"],
                "span_id": canned_span["spanId"],
            },
        },
    )
    await hot_store.insert(record)

    # Register the tool against our capture shim. The decorator
    # factory inside ``register_otel_query_tools`` calls ``app.tool()``
    # to register ``query_local_traces``.
    registry = _ToolRegistry()
    register_otel_query_tools(registry, hot_store)

    # Now invoke the registered tool. This exercises the same body
    # production dispatchers call. Filter only on system_id — the
    # task_class JSON filter path is exercised separately by
    # tests/unit/test_otel_trace_ingester.py + the query_traces
    # unit tests. The discipline-mandated assertion here is
    # "non-empty results" against a real HotStore.
    tool_fn = registry._tools["query_local_traces"]
    result = await tool_fn(
        system_id="akosha",
        limit=10,
    )

    # Assert non-empty results (discipline-mandated shape).
    assert result, "query_local_traces returned no rows; expected >=1"
    assert any(
        r["conversation_id"].endswith("query_local_traces_e2e_canned")
        for r in result
    ), (
        f"expected the canned span in the result; got: "
        f"{[r['conversation_id'] for r in result]}"
    )

    # Metadata round-trips — the tool returns the metadata the
    # ingester wrote, including the otel.trace_id and
    # attributes.task_class.
    sample = next(
        r for r in result
        if r["conversation_id"].endswith("query_local_traces_e2e_canned")
    )
    # query_traces returns metadata as a JSON string (DuckDB does
    # not auto-parse JSON columns into Python dicts); parse before
    # accessing keys.
    raw_metadata = sample["metadata"]
    metadata = (
        json.loads(raw_metadata) if isinstance(raw_metadata, str) else raw_metadata
    )
    assert metadata["attributes"]["task_class"] == "CODE_GENERATION"
    assert metadata["otel"]["span_id"] == "query_local_traces_e2e_canned"
    assert metadata["otel"]["trace_id"] == canned_span["traceId"]
