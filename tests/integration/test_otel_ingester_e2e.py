"""End-to-end test: OtelTraceIngester polls a mock OTLP collector and ingests spans.

Pins the full lifecycle:
- MockBodaiEcosystem owns the canonical MockOtelCollector from
  tests/fixtures/mock_bodai_mcp.py (the canonical fixture contract).
- OtelTraceIngester polls the fixture, fetches, embeds, normalizes,
  inserts, advances watermark.
- A real HotStore receives the HotRecord written by the ingester.

This test focuses on the OTel data plane end-to-end. The concurrent
producer behavior (CodeGraphIngester + OTel writing to the same
HotStore) is exercised by tests/integration/test_live_mcp_smoke.py.
"""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import AsyncMock

import numpy as np
import pytest

from akosha.ingestion.otel_ingester import OtelTraceIngester
from akosha.storage.hot_store import HotStore

from tests.fixtures.mock_bodai_mcp import MockBodaiEcosystem, make_canned_span


@pytest.fixture
def stub_embedding_service() -> Any:
    """An EmbeddingService stand-in that returns a zero-vector."""
    service = AsyncMock()
    service.generate_embedding = AsyncMock(return_value=np.zeros(384, dtype=np.float32))
    return service


@pytest.mark.asyncio
@pytest.mark.slow
async def test_otel_ingester_e2e_polls_and_ingests(
    stub_embedding_service: Any,
) -> None:
    """A single poll cycle ingests the canned span into a real HotStore.

    Verifies the full path: HTTP fetch -> service.name extraction ->
    embed -> normalize -> insert -> watermark advance.
    """
    # The canned span's startTimeUnixNano must be >= the request's
    # ``since`` filter, so we anchor it to ``now``. The 2023 default
    # in make_canned_span would be filtered out by the collector's
    # ``since > startTimeUnixNano`` check.
    recent_unix_nano = str(int(time.time_ns()))
    spans = [make_canned_span(task_class="CODE_GENERATION", start_unix_nano=recent_unix_nano)]

    async with MockBodaiEcosystem(otel_spans=spans) as eco:
        hot_store = HotStore(database_path=":memory:")
        await hot_store.initialize()

        ingester = OtelTraceIngester(
            hot_store=hot_store,
            embedding_service=stub_embedding_service,
            otlp_endpoint=eco.otel_endpoint,
            poll_interval_seconds=3600,
            initial_lookback_seconds=0,
        )

        await ingester.start()
        try:
            # Cancel the polling task immediately — we drive one cycle
            # manually so the test completes in seconds rather than
            # waiting ``poll_interval_seconds`` for the loop to fire.
            if ingester._poll_task is not None:
                ingester._poll_task.cancel()

            spans_by_system = await ingester._fetch_spans(since_unix_nano=0)
            for system_id, span in spans_by_system:
                await ingester._ingest_span(span, system_id=system_id)

            # The collector's HTTP layer was polled.
            assert eco.otel_request_count >= 1, (
                f"OTel mock was polled {eco.otel_request_count} times; expected >=1"
            )

            # The span landed in the hot store. query_traces returns the
            # metadata column as a JSON string (DuckDB does not auto-parse);
            # parse it before reading attributes.
            traces = await hot_store.query_traces(system_id="akosha")
            assert traces, "expected >=1 trace for akosha; hot store empty"
            metadata_raw = traces[0]["metadata"]
            metadata = json.loads(metadata_raw) if isinstance(metadata_raw, str) else metadata_raw
            task_class = (metadata.get("attributes") or {}).get("task_class")
            assert task_class == "CODE_GENERATION", (
                f"expected CODE_GENERATION; got metadata={metadata}"
            )

            # The watermark advanced past the canned span's start time.
            assert ingester._watermarks["akosha"] > 0, (
                f"expected watermark advance; got {ingester._watermarks}"
            )
        finally:
            await ingester.stop()
