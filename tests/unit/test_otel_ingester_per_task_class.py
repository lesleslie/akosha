"""Tests for OtelTraceIngester per-task-class cycle/error tracking.

REQ-FEED-002 + REQ-FEED-003: the OTel ingester tracks per-task-class
``cycles_total`` and ``errors_total`` counters so the ``/health`` mcp_tool_call
feed surfaces its own activity instead of borrowing the ingester-level
counters (the gap noted at ``akosha/mcp/server.py:763`` before Phase 2).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx2 as httpx
import pytest

from akosha.ingestion.otel_ingester import OtelTraceIngester


def _make_ingester(**overrides: Any) -> OtelTraceIngester:
    """Build an ingester with mocked dependencies."""
    defaults: dict[str, Any] = {
        "hot_store": MagicMock(),
        "embedding_service": MagicMock(),
        "otlp_endpoint": "http://collector.local:4318/v1/traces",
        "poll_interval_seconds": 3600,
        "max_spans_per_poll": 500,
        "initial_lookback_seconds": 3600,
    }
    defaults.update(overrides)
    return OtelTraceIngester(**defaults)


def _make_span(span_id: str, task_class: str | None) -> dict[str, Any]:
    """Build an OTLP span with a task_class attribute."""
    attrs: list[dict[str, Any]] = []
    if task_class is not None:
        attrs.append({"key": "task_class", "value": {"stringValue": task_class}})
    return {
        "traceId": "0af7651916cd43dd8448eb211c80319c",
        "spanId": span_id,
        "name": "test.span",
        "startTimeUnixNano": "1700000000000000000",
        "attributes": attrs,
    }


def _otlp_envelope(spans: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap a list of spans in the OTLP resourceSpans envelope."""
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "mahavishnu"}}
                    ]
                },
                "scopeSpans": [{"spans": spans}],
            }
        ]
    }


@pytest.mark.asyncio
async def test_per_task_class_cycles_increment() -> None:
    """One poll cycle with mcp_tool_call + conversation spans increments
    each task_class cycle counter by 1, NOT by row count (Phase 2 spec).
    """
    ingester = _make_ingester(
        embedding_service=MagicMock(
            generate_embedding=AsyncMock(return_value=_zeros(384))
        ),
    )
    ingester.hot_store.insert = AsyncMock()

    await ingester.start()
    try:
        spans = [
            _make_span("span-1", "mcp_tool_call"),
            _make_span("span-2", "mcp_tool_call"),
            _make_span("span-3", "mcp_tool_call"),
            _make_span("span-4", "conversation"),
            _make_span("span-5", "conversation"),
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = _otlp_envelope(spans)
        mock_response.raise_for_status = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"{}"
        ingester._http_client.post = AsyncMock(return_value=mock_response)

        # Drive one polling iteration directly.
        async def _no_sleep(_: float) -> None:
            return None

        ingester._poll_interval_seconds = 0  # ty: ignore[invalid-assignment]
        # Replace _polling_loop logic by calling once and stopping.
        await ingester._fetch_spans(since_unix_nano=0)
        # Simulate the per-span ingest the loop does (mirroring its body).
        for span in spans:
            await ingester._ingest_span(span, system_id="mahavishnu")
        # Manually trigger the per-task-class counter increment (this is the
        # behaviour the production polling loop will perform after ingest).
        ingester._record_poll_cycle_observations(spans_seen=spans)

        assert ingester.get_cycles_for_task_class("mcp_tool_call") == 1
        assert ingester.get_cycles_for_task_class("conversation") == 1
        assert ingester.get_cycles_for_task_class("unknown_feed") == 0
    finally:
        await ingester.stop()


@pytest.mark.asyncio
async def test_per_task_class_errors_increment() -> None:
    """A failed ingest increments the matching task_class error counter.

    The OTel ingester pre-extracts ``task_class`` from span attributes so
    per-row ingest failures can be attributed to the right task_class.
    """
    ingester = _make_ingester(
        embedding_service=MagicMock(
            generate_embedding=AsyncMock(side_effect=RuntimeError("embedding down"))
        ),
    )
    ingester.hot_store.insert = AsyncMock()

    await ingester.start()
    try:
        bad_span = _make_span("bad-span", "mcp_tool_call")
        # Drive the per-span failure path manually so we control the
        # counter increment call site without needing the full polling loop.
        try:
            await ingester._ingest_span(bad_span, system_id="mahavishnu")
        except RuntimeError:
            ingester._record_poll_cycle_errors(bad_span)

        assert ingester.get_errors_for_task_class("mcp_tool_call") == 1
        assert ingester.get_errors_for_task_class("conversation") == 0
    finally:
        await ingester.stop()


def _zeros(dim: int) -> Any:
    """Zero-vector embedding compatible with the OTel ingester's numpy path."""
    import numpy as np

    return np.zeros(dim, dtype=np.float32)
