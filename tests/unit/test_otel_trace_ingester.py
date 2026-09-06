"""Unit tests for OtelTraceIngester."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from akosha.ingestion.otel_ingester import OtelTraceIngester


def _make_ingester(**overrides: Any) -> OtelTraceIngester:
    """Build an ingester with mocked dependencies."""
    defaults: dict[str, Any] = {
        "hot_store": MagicMock(),
        "embedding_service": MagicMock(),
        "otlp_endpoint": "http://collector.local:4318/v1/traces",
        "poll_interval_seconds": 3600,  # long; we never wait
        "max_spans_per_poll": 500,
        "initial_lookback_seconds": 3600,
    }
    defaults.update(overrides)
    return OtelTraceIngester(**defaults)


@pytest.mark.asyncio
async def test_start_and_stop_lifecycle() -> None:
    """The ingester must start a poll task and stop it cleanly."""
    ingester = _make_ingester()

    await ingester.start()
    assert ingester._running is True
    assert ingester._poll_task is not None
    assert not ingester._poll_task.done()

    await ingester.stop()
    assert ingester._running is False
    assert ingester._http_client is None  # closed on stop


@pytest.mark.asyncio
async def test_stop_is_idempotent_when_not_running() -> None:
    """Stopping an ingester that was never started is a no-op."""
    ingester = _make_ingester()
    await ingester.stop()  # must not raise
    assert ingester._running is False


@pytest.mark.asyncio
async def test_start_is_idempotent_when_already_running() -> None:
    """Starting an already-running ingester is a no-op (logs a warning)."""
    ingester = _make_ingester()
    await ingester.start()
    first_task = ingester._poll_task
    await ingester.start()  # must not replace the running task
    assert ingester._poll_task is first_task
    await ingester.stop()


@pytest.mark.asyncio
async def test_fetch_spans_returns_otlp_resource_spans() -> None:
    """The HTTP fetch unwraps OTLP resourceSpans into a flat span list."""
    ingester = _make_ingester()
    await ingester.start()
    try:
        # Replace _http_client with a mock that returns canned OTLP
        canned_response = {
            "resourceSpans": [
                {
                    "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "akosha"}}]},
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": "0af7651916cd43dd8448eb211c80319c",
                                    "spanId": "b7ad6b7169203331",
                                    "name": "test.span",
                                    "startTimeUnixNano": "1700000000000000000",
                                    "endTimeUnixNano": "1700000000001000000",
                                    "attributes": [
                                        {"key": "task.class", "value": {"stringValue": "CODE_GENERATION"}}
                                    ],
                                }
                            ]
                        }
                    ],
                }
            ]
        }
        mock_response = MagicMock()
        mock_response.json.return_value = canned_response
        mock_response.raise_for_status = MagicMock()
        ingester._http_client.get = AsyncMock(return_value=mock_response)

        spans = await ingester._fetch_spans(since_unix_nano=0)
        assert len(spans) == 1
        assert spans[0]["name"] == "test.span"
    finally:
        await ingester.stop()


def test_normalize_span_produces_hot_record() -> None:
    """The normalizer maps OTel span fields to a HotRecord."""
    ingester = _make_ingester()
    span = {
        "traceId": "0af7651916cd43dd8448eb211c80319c",
        "spanId": "b7ad6b7169203331",
        "name": "test.span",
        "startTimeUnixNano": "1700000000000000000",
        "endTimeUnixNano": "1700000000001000000",
        "attributes": [{"key": "task.class", "value": {"stringValue": "CODE_GENERATION"}}],
    }
    system_id = "akosha"
    embedding = np.zeros(384, dtype=np.float32)
    record = ingester._normalize_span(span, system_id=system_id, embedding=embedding.tolist())

    assert record.system_id == "akosha"
    assert record.conversation_id == "akosha:b7ad6b7169203331"
    assert "test.span" in record.content
    # The JSON content preserves the raw "task.class" key (no escape) and
    # the "CODE_GENERATION" stringValue.
    assert '"task.class"' in record.content
    assert "CODE_GENERATION" in record.content
    assert record.metadata["attributes"]["task_class"] == "CODE_GENERATION"
    assert record.embedding == embedding.tolist()


@pytest.mark.asyncio
async def test_ingest_span_writes_to_hot_store_and_advances_watermark() -> None:
    """_ingest_span embeds, inserts, and advances the watermark."""
    ingester = _make_ingester(
        embedding_service=MagicMock(
            generate_embedding=AsyncMock(
                return_value=np.zeros(384, dtype=np.float32)
            )
        ),
    )
    hot_store = ingester.hot_store
    hot_store.insert = AsyncMock()

    span = {
        "traceId": "0af7651916cd43dd8448eb211c80319c",
        "spanId": "b7ad6b7169203331",
        "name": "test.span",
        "startTimeUnixNano": "1700000000000000000",
        "endTimeUnixNano": "1700000000001000000",
        "attributes": [],
    }
    system_id = "akosha"
    await ingester._ingest_span(span, system_id=system_id)

    hot_store.insert.assert_awaited_once()
    inserted_record = hot_store.insert.await_args.args[0]
    assert inserted_record.system_id == "akosha"
    assert ingester._watermarks["akosha"] == 1700000000000000000


@pytest.mark.asyncio
async def test_ingest_span_skips_when_embedding_fails() -> None:
    """A span whose embedding raises is propagated so the polling loop's
    outer try/except can log + skip it. Watermark does NOT advance."""
    ingester = _make_ingester(
        embedding_service=MagicMock(
            generate_embedding=AsyncMock(side_effect=RuntimeError("embedding down"))
        ),
    )
    hot_store = ingester.hot_store
    hot_store.insert = AsyncMock()

    span = {
        "traceId": "0af7651916cd43dd8448eb211c80319c",
        "spanId": "b7ad6b7169203331",
        "name": "test.span",
        "startTimeUnixNano": "1700000000000000000",
        "attributes": [],
    }
    with pytest.raises(RuntimeError, match="embedding down"):
        await ingester._ingest_span(span, system_id="akosha")

    hot_store.insert.assert_not_awaited()
    assert "akosha" not in ingester._watermarks
