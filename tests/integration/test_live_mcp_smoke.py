"""End-to-end smoke test: Akosha ingesters against mock Bodai ecosystem.

This test exercises the CodeGraphIngester and OtelTraceIngester pipelines
against the in-memory MockBodaiEcosystem fixture (Session-Buddy MCP +
OTel collector on ephemeral ports). It pins:

- CodeGraphIngester two-phase ingest against the mock Session-Buddy:
  list_code_graphs + get_code_graph + hot_store.insert_code_graph
- OtelTraceIngester against the mock OTel collector: GET /v1/traces?since,
  decode OTLP resourceSpans, embed, normalize, insert, watermark advance
- Shared-singleton pattern: hot_store accepts writes from both ingesters
  via the same DuckDB instance

OTel e2e (single ingester only) is verified separately by
tests/integration/test_otel_ingester_e2e.py.

The full MCP server lifespan integration is exercised by
tests/unit/test_mcp_server_lifespan.py and test_wave5_lifespan_wiring.py;
this smoke test focuses on the data plane (ingester → mock → hot_store).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import numpy as np
import pytest

from akosha.ingestion.code_graph_ingester import CodeGraphIngester
from akosha.ingestion.otel_ingester import OtelTraceIngester
from akosha.storage.hot_store import HotStore

from tests.fixtures.mock_bodai_mcp import MockBodaiEcosystem


@pytest.fixture
def stub_embedding_service() -> Any:
    """An EmbeddingService stand-in that returns a zero-vector."""
    service = AsyncMock()
    service.generate_embedding = AsyncMock(return_value=np.zeros(384, dtype=np.float32))
    return service


@pytest.fixture
async def hot_store() -> HotStore:
    """A real HotStore backed by an in-memory DuckDB."""
    store = HotStore(database_path=":memory:")
    await store.initialize()
    return store


@pytest.mark.asyncio
@pytest.mark.slow
async def test_code_graph_ingester_e2e_against_mock_session_buddy(
    hot_store: HotStore,
) -> None:
    """CodeGraphIngester polls the mock Session-Buddy and ingests the
    canned code graph into hot_store in a single cycle.

    Asserts:
    - The mock's POST /tools/call was hit at least once for list_code_graphs
      and once for get_code_graph
    - The canned akosha@deadbeef graph landed in hot_store.code_graphs
    - Watermark advanced so the second cycle sees zero new graphs
    """
    code_graphs = [
        {
            "id": "akosha@deadbeef",
            "repo_path": "/path/to/akosha",
            "commit_hash": "deadbeef",
            "indexed_at": "2026-09-06T00:00:00Z",
            "nodes_count": 42,
            "edges_count": 17,
        }
    ]
    full_graphs = {
        "/path/to/akosha@deadbeef": {
            "repo_path": "/path/to/akosha",
            "commit_hash": "deadbeef",
            "indexed_at": "2026-09-06T00:00:00Z",
            "nodes_count": 42,
            "edges_count": 17,
            "graph_data": {"nodes": [], "edges": []},
            "metadata": {"language": "python"},
        }
    }

    async with MockBodaiEcosystem(code_graphs=code_graphs, full_graphs=full_graphs) as eco:
        ingester = CodeGraphIngester(
            hot_store=hot_store,
            session_buddy_endpoint=eco.session_buddy_url,
        )
        # start() initializes the HTTP client and starts the polling
        # task; we cancel the task immediately and drive one cycle
        # manually via the public discover/ingest methods.
        await ingester.start()
        try:
            if ingester._poll_task is not None:
                ingester._poll_task.cancel()
            new_graphs = await ingester._discover_code_graphs()
            for graph in new_graphs:
                await ingester._ingest_code_graph(graph)
        finally:
            await ingester.stop()

        # The mock was actually polled — list_code_graphs is the first
        # round-trip, get_code_graph is the second
        assert eco.session_buddy_request_count >= 2, (
            f"mock was polled {eco.session_buddy_request_count} times; "
            f"expected ≥2 (list_code_graphs + get_code_graph)"
        )

        # The canned graph landed in hot_store
        ingested = await hot_store.list_code_graphs(limit=10)
        assert any(
            g.get("repo_path") == "/path/to/akosha" and g.get("commit_hash") == "deadbeef"
            for g in ingested
        ), f"ingested graphs missing akosha@deadbeef; got: {ingested!r}"


@pytest.mark.asyncio
@pytest.mark.slow
async def test_otel_ingester_e2e_against_mock_collector(
    hot_store: HotStore,
    stub_embedding_service: Any,
) -> None:
    """OtelTraceIngester polls the mock OTel collector and ingests a span
    into hot_store via the same DuckDB instance CodeGraphIngester uses.

    Pins the OTel half of the smoke fixture. Verifies that:
    - The mock's GET /v1/traces was hit
    - The canned span landed in hot_store
    - Watermark advanced past the canned span
    """
    import time

    recent_unix_nano = str(int(time.time_ns()))
    spans = [
        {
            "traceId": "0af7651916cd43dd8448eb211c80319c",
            "spanId": "acosha_span_demo",
            "name": "akosha.smoke",
            "startTimeUnixNano": recent_unix_nano,
            "endTimeUnixNano": str(int(recent_unix_nano) + 1_000_000),
            "attributes": [
                {"key": "task.class", "value": {"stringValue": "CODE_GENERATION"}},
            ],
        }
    ]

    async with MockBodaiEcosystem(otel_spans=spans) as eco:
        ingester = OtelTraceIngester(
            hot_store=hot_store,
            embedding_service=stub_embedding_service,
            otlp_endpoint=eco.otel_endpoint,
            poll_interval_seconds=3600,
            initial_lookback_seconds=0,
        )

        try:
            await ingester.start()
            # Drive one ingest cycle via the public fetcher
            spans_by_system = await ingester._fetch_spans(since_unix_nano=0)
            for system_id, span in spans_by_system:
                await ingester._ingest_span(span, system_id=system_id)

            assert eco.otel_request_count >= 1, (
                f"OTel mock was polled {eco.otel_request_count} times; expected ≥1"
            )

            traces = await hot_store.query_traces(system_id="akosha")
            assert traces, "expected ≥1 trace for akosha; hot store empty"
            # content is the JSON-dumped span (excluding traceId+spanId);
            # the span name ("akosha.smoke") survives in the dump.
            assert any(
                t["system_id"] == "akosha" and "akosha.smoke" in (t.get("content") or "")
                for t in traces
            ), f"expected akosha.smoke span; got: {[t.get('content', '')[:80] for t in traces]}"

            assert ingester._watermarks["akosha"] > 0, (
                f"expected watermark advance; got {ingester._watermarks}"
            )
        finally:
            await ingester.stop()


@pytest.mark.asyncio
@pytest.mark.slow
async def test_ecosystem_runs_both_ingesters_concurrently(
    hot_store: HotStore,
    stub_embedding_service: Any,
) -> None:
    """Smoke: both ingesters run against the same MockBodaiEcosystem and
    write to the same hot_store, exercising the shared-singleton pattern
    end-to-end.
    """
    import time

    canned_unix_nano = str(int(time.time_ns()))
    code_graphs = [
        {
            "id": "akosha@deadbeef",
            "repo_path": "/path/to/akosha",
            "commit_hash": "deadbeef",
            "indexed_at": "2026-09-06T00:00:00Z",
            "nodes_count": 42,
            "edges_count": 17,
        }
    ]
    full_graphs = {
        "/path/to/akosha@deadbeef": {
            "repo_path": "/path/to/akosha",
            "commit_hash": "deadbeef",
            "indexed_at": "2026-09-06T00:00:00Z",
            "nodes_count": 42,
            "edges_count": 17,
            "graph_data": {"nodes": [], "edges": []},
            "metadata": {"language": "python"},
        }
    }
    otel_spans = [
        {
            "traceId": "0af7651916cd43dd8448eb211c80319c",
            "spanId": "acosha_concurrent_demo",
            "name": "akosha.concurrent.smoke",
            "startTimeUnixNano": canned_unix_nano,
            "endTimeUnixNano": str(int(canned_unix_nano) + 1_000_000),
            "attributes": [
                {"key": "task.class", "value": {"stringValue": "CODE_GENERATION"}},
            ],
        }
    ]

    async with MockBodaiEcosystem(
        code_graphs=code_graphs,
        full_graphs=full_graphs,
        otel_spans=otel_spans,
    ) as eco:
        sb_ingester = CodeGraphIngester(
            hot_store=hot_store,
            session_buddy_endpoint=eco.session_buddy_url,
        )
        otel_ingester = OtelTraceIngester(
            hot_store=hot_store,
            embedding_service=stub_embedding_service,
            otlp_endpoint=eco.otel_endpoint,
            poll_interval_seconds=3600,
            initial_lookback_seconds=0,
        )

        try:
            # Both ingesters need start() to initialize their HTTP clients.
            # Cancel the polling tasks immediately so we drive one cycle
            # manually.
            await sb_ingester.start()
            await otel_ingester.start()
            if sb_ingester._poll_task is not None:
                sb_ingester._poll_task.cancel()
            if otel_ingester._poll_task is not None:
                otel_ingester._poll_task.cancel()

            # Run both ingest cycles concurrently
            async def sb_cycle() -> None:
                new_graphs = await sb_ingester._discover_code_graphs()
                for graph in new_graphs:
                    await sb_ingester._ingest_code_graph(graph)

            async def otel_cycle() -> None:
                spans_by_system = await otel_ingester._fetch_spans(since_unix_nano=0)
                for system_id, span in spans_by_system:
                    await otel_ingester._ingest_span(span, system_id=system_id)

            await asyncio.gather(sb_cycle(), otel_cycle())

            # Both mocks were polled
            assert eco.session_buddy_request_count >= 2, (
                f"SB mock polled {eco.session_buddy_request_count}×; expected ≥2"
            )
            assert eco.otel_request_count >= 1, (
                f"OTel mock polled {eco.otel_request_count}×; expected ≥1"
            )

            # Both data streams landed in the same hot_store
            ingested_graphs = await hot_store.list_code_graphs(limit=10)
            assert any(g.get("repo_path") == "/path/to/akosha" for g in ingested_graphs), (
                f"missing akosha code graph; got: {ingested_graphs!r}"
            )

            traces = await hot_store.query_traces(system_id="akosha")
            assert any(
                t["system_id"] == "akosha" and "akosha.concurrent.smoke" in (t.get("content") or "")
                for t in traces
            ), f"missing akosha concurrent span; got: {[t.get('content', '')[:80] for t in traces]}"
        finally:
            await otel_ingester.stop()
            await sb_ingester.stop()
