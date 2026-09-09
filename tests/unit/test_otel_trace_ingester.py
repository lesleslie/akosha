"""Unit tests for OtelTraceIngester."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx2 as httpx
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
                    "resource": {
                        "attributes": [{"key": "service.name", "value": {"stringValue": "akosha"}}]
                    },
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
                                        {
                                            "key": "task.class",
                                            "value": {"stringValue": "CODE_GENERATION"},
                                        }
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
        mock_response.status_code = 200
        mock_response.content = b'{"resourceSpans":[]}'
        ingester._http_client.post = AsyncMock(return_value=mock_response)

        spans = await ingester._fetch_spans(since_unix_nano=0)
        assert len(spans) == 1
        # _fetch_spans returns (system_id, span) tuples
        system_id, span = spans[0]
        assert system_id == "akosha"  # extracted from resource.service.name
        assert span["name"] == "test.span"
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
            generate_embedding=AsyncMock(return_value=np.zeros(384, dtype=np.float32))
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


@pytest.mark.asyncio
async def test_fetch_spans_uses_post_not_get() -> None:
    """_fetch_spans must POST with an empty resourceSpans envelope and JSON
    Content-Type; it must NOT issue a GET against the endpoint."""
    ingester = _make_ingester()
    await ingester.start()
    try:
        mock_response = MagicMock()
        mock_response.json.return_value = {"resourceSpans": []}
        mock_response.raise_for_status = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b'{"resourceSpans": []}'
        ingester._http_client.post = AsyncMock(return_value=mock_response)
        # Track GET calls to assert they never happen.
        get_mock = AsyncMock()
        ingester._http_client.get = get_mock

        await ingester._fetch_spans(since_unix_nano=12345)

        ingester._http_client.post.assert_awaited_once()
        post_args, post_kwargs = ingester._http_client.post.await_args
        # First positional arg is the URL.
        assert post_args[0] == "http://collector.local:4318/v1/traces"
        # JSON body is the empty envelope; Content-Type is application/json.
        assert post_kwargs["json"] == {"resourceSpans": []}
        assert post_kwargs["headers"] == {"Content-Type": "application/json"}
        # GET must never be called (the previous bug surface was a GET-with-since).
        get_mock.assert_not_called()
    finally:
        await ingester.stop()


@pytest.mark.asyncio
async def test_fetch_spans_unwraps_resource_spans() -> None:
    """Multiple resourceSpans, multiple scopeSpans, multiple spans
    flatten into (system_id, span) tuples with correct service.name attribution.
    Spans under a resource without service.name fall back to 'unknown'."""
    ingester = _make_ingester()
    await ingester.start()
    try:
        canned_response = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": "alpha"}}
                        ]
                    },
                    "scopeSpans": [
                        {
                            "spans": [
                                {"spanId": "alpha-span-1", "name": "alpha.a"},
                                {"spanId": "alpha-span-2", "name": "alpha.b"},
                            ]
                        },
                        {
                            "spans": [
                                {"spanId": "alpha-span-3", "name": "alpha.c"},
                            ]
                        },
                    ],
                },
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"intValue": 42}}
                        ]
                    },
                    "scopeSpans": [
                        {
                            "spans": [
                                {"spanId": "beta-span-1", "name": "beta.a"},
                            ]
                        }
                    ],
                },
                {
                    # No service.name attribute -> spans fall back to "unknown".
                    "resource": {"attributes": []},
                    "scopeSpans": [
                        {
                            "spans": [
                                {"spanId": "unknown-span-1", "name": "x"},
                            ]
                        }
                    ],
                },
            ]
        }
        mock_response = MagicMock()
        mock_response.json.return_value = canned_response
        mock_response.raise_for_status = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b'{"resourceSpans":[]}'
        ingester._http_client.post = AsyncMock(return_value=mock_response)

        spans = await ingester._fetch_spans(since_unix_nano=0)

        # 3 spans under alpha + 1 under beta + 1 unknown = 5 total
        assert len(spans) == 5
        by_span_id = {span["spanId"]: system_id for system_id, span in spans}
        assert by_span_id["alpha-span-1"] == "alpha"
        assert by_span_id["alpha-span-2"] == "alpha"
        assert by_span_id["alpha-span-3"] == "alpha"
        assert by_span_id["beta-span-1"] == "42"  # intValue stringified
        assert by_span_id["unknown-span-1"] == "unknown"
    finally:
        await ingester.stop()


@pytest.mark.asyncio
async def test_fetch_spans_returns_empty_on_404() -> None:
    """A 404 from the receiver is treated as an empty poll: returns [],
    bumps _errors_total so per-feed observability surfaces the gap."""
    ingester = _make_ingester()
    await ingester.start()
    try:
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.content = b""
        mock_response.json = MagicMock()  # never called
        mock_response.raise_for_status = MagicMock()
        ingester._http_client.post = AsyncMock(return_value=mock_response)

        errors_before = ingester._errors_total
        result = await ingester._fetch_spans(since_unix_nano=0)

        assert result == []
        # Empty body must NOT raise; .json() must not have been called.
        mock_response.json.assert_not_called()
        # _errors_total increments by 1 for the 404 (receiver-not-export-capable).
        assert ingester._errors_total == errors_before + 1
        # 5xx delegation path: never reached, so raise_for_status is a no-op.
        mock_response.raise_for_status.assert_not_called()
    finally:
        await ingester.stop()


@pytest.mark.asyncio
async def test_fetch_spans_raises_on_5xx() -> None:
    """On 5xx, _fetch_spans delegates to raise_for_status so the
    _polling_loop outer try/except can log + bump errors + sleep.
    _errors_total is NOT incremented here — the loop handler owns that."""
    ingester = _make_ingester()
    await ingester.start()
    try:
        # Build a synthetic 500 by raising from raise_for_status, mirroring
        # what httpx does when called against a 500 response.
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.content = b"internal server error"
        mock_response.json = MagicMock()
        request = httpx.Request("POST", "http://collector.local:4318/v1/traces")
        response_obj = httpx.Response(500, request=request)
        mock_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "Server Error", request=request, response=response_obj
            )
        )
        ingester._http_client.post = AsyncMock(return_value=mock_response)

        errors_before = ingester._errors_total
        with pytest.raises(httpx.HTTPStatusError):
            await ingester._fetch_spans(since_unix_nano=0)

        # _errors_total NOT bumped here — that's the loop handler's job.
        assert ingester._errors_total == errors_before
        mock_response.raise_for_status.assert_called_once()
    finally:
        await ingester.stop()
