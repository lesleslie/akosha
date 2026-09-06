"""End-to-end test: OtelTraceIngester polls a mock OTLP collector and ingests spans.

Pins the full lifecycle:
- mock OTLP collector (Starlette ASGI app) served on an ephemeral port
- OtelTraceIngester polls, fetches, embeds, normalizes, inserts, advances watermark
- a real HotStore receives the HotRecord written by the ingester
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock

import numpy as np
import pytest
import uvicorn

from akosha.ingestion.otel_ingester import OtelTraceIngester
from akosha.storage.hot_store import HotStore

from tests.integration.mock_otlp_collector import MockOtelCollector, make_canned_span


@pytest.fixture
async def mock_collector_server() -> Any:
    """Start a mock OTLP collector on an ephemeral port.

    The canned span uses ``now`` (rather than 2023) as its timestamp so the
    collector's ``since`` filter passes it through on the very first poll.
    """
    recent_unix_nano = str(int(time.time_ns()))
    spans = [make_canned_span(task_class="CODE_GENERATION", start_unix_nano=recent_unix_nano)]
    collector = MockOtelCollector(spans=spans)
    config = uvicorn.Config(collector, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started or not server.servers:
        await asyncio.sleep(0.05)
    try:
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}/v1/traces", collector
    finally:
        server.should_exit = True
        await task


@pytest.fixture
def stub_embedding_service() -> Any:
    """An EmbeddingService stand-in that returns a zero-vector."""
    service = AsyncMock()
    service.generate_embedding = AsyncMock(
        return_value=np.zeros(384, dtype=np.float32)
    )
    return service


@pytest.mark.asyncio
@pytest.mark.slow
async def test_otel_ingester_e2e_polls_and_ingests(
    mock_collector_server: Any,
    stub_embedding_service: Any,
) -> None:
    """A single poll cycle ingests the canned span into a real HotStore.

    Verifies the full path: HTTP fetch -> service.name extraction -> embed ->
    normalize -> insert -> watermark advance.

    ``query_traces`` returns the metadata column as a raw JSON string
    (DuckDB does not auto-parse), so this test parses it before reading
    ``metadata.attributes.task_class``.
    """
    endpoint, collector = mock_collector_server
    hot_store = HotStore(database_path=":memory:")
    await hot_store.initialize()

    ingester = OtelTraceIngester(
        hot_store=hot_store,
        embedding_service=stub_embedding_service,
        otlp_endpoint=endpoint,
        poll_interval_seconds=3600,  # long; we only care about one cycle
        initial_lookback_seconds=0,
    )

    await ingester.start()
    try:
        # Manually trigger a single ingest cycle so we don't race the
        # background task. This exercises the same code path the
        # polling loop invokes per cycle.
        spans_by_system = await ingester._fetch_spans(since_unix_nano=0)
        for system_id, span in spans_by_system:
            await ingester._ingest_span(span, system_id=system_id)

        # The mock's HTTP layer was definitely polled
        assert collector.request_count >= 1, (
            f"OTel mock was polled {collector.request_count} times; expected >=1"
        )

        # The span landed in the hot store. query_traces returns metadata
        # as a JSON string; parse it to inspect attributes.
        traces = await hot_store.query_traces(system_id="akosha")
        assert traces, "expected >=1 trace for akosha; hot store empty"
        metadata = json.loads(traces[0]["metadata"]) if isinstance(
            traces[0]["metadata"], str
        ) else traces[0]["metadata"]
        task_class = (metadata.get("attributes") or {}).get("task_class")
        assert task_class == "CODE_GENERATION", (
            f"expected CODE_GENERATION; got metadata={metadata}"
        )

        # The watermark advanced
        assert ingester._watermarks["akosha"] > 0, (
            f"expected watermark advance; got {ingester._watermarks}"
        )
    finally:
        await ingester.stop()
