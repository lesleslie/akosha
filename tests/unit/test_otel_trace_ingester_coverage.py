"""Additional tests to lift coverage on OtelTraceIngester.

Targets the previously uncovered branches in:

- :meth:`OtelTraceIngester._attrs_to_dict` — every AnyValue variant
  (stringValue, intValue, doubleValue, boolValue, bytesValue,
  arrayValue, kvlistValue, unknown wrapper) plus defensive paths
  (None attrs, key=None, value=None, None arrayValue/kvlistValue).
- :meth:`OtelTraceIngester._fetch_spans` — service.name extraction
  across the four scalar wrappers, missing/null value entries,
  empty ``resourceSpans``, missing ``scopeSpans``/``spans``,
  missing/None ``attributes``, the ``HTTP client not initialized``
  guard, and the ``raise_for_status`` failure path.
- :meth:`OtelTraceIngester._polling_loop` — cold-start lookback
  branch, warm-cycle global watermark branch, per-span exception
  swallow + ``_errors_total`` increment, outer exception path,
  ``_last_poll_at`` and ``_cycles_total`` bookkeeping, cancellation.
- :meth:`OtelTraceIngester.stop` — the unexpected exception
  during shutdown path (non-CancelledError escaping the poll task).
- :meth:`OtelTraceIngester._now_unix_nano` — sanity.
- :meth:`OtelTraceIngester._normalize_span` — span with no
  ``task.class`` attribute and the metadata pivot when it is absent.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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
        "poll_interval_seconds": 3600,
        "max_spans_per_poll": 500,
        "initial_lookback_seconds": 3600,
    }
    defaults.update(overrides)
    return OtelTraceIngester(**defaults)


# ---------------------------------------------------------------------------
# _attrs_to_dict
# ---------------------------------------------------------------------------


def test_attrs_to_dict_unwraps_string_value() -> None:
    """stringValue wrapper becomes the underlying string."""
    attrs = [{"key": "foo", "value": {"stringValue": "bar"}}]
    assert OtelTraceIngester._attrs_to_dict(attrs) == {"foo": "bar"}


def test_attrs_to_dict_unwraps_int_value() -> None:
    """intValue is coerced to a Python int (the source may carry it as string)."""
    attrs = [{"key": "n", "value": {"intValue": "42"}}]
    assert OtelTraceIngester._attrs_to_dict(attrs) == {"n": 42}


def test_attrs_to_dict_unwraps_double_value() -> None:
    """doubleValue becomes a Python float."""
    attrs = [{"key": "x", "value": {"doubleValue": 1.25}}]
    assert OtelTraceIngester._attrs_to_dict(attrs) == {"x": 1.25}


def test_attrs_to_dict_unwraps_bool_value() -> None:
    """boolValue becomes a Python bool (and rejects truthy non-bools)."""
    attrs = [{"key": "b", "value": {"boolValue": True}}]
    assert OtelTraceIngester._attrs_to_dict(attrs) == {"b": True}


def test_attrs_to_dict_unwraps_bytes_value() -> None:
    """bytesValue is passed through verbatim (caller decodes)."""
    attrs = [{"key": "blob", "value": {"bytesValue": "aGVsbG8="}}]
    assert OtelTraceIngester._attrs_to_dict(attrs) == {"blob": "aGVsbG8="}


def test_attrs_to_dict_unwraps_array_value() -> None:
    """arrayValue recursively unwraps nested AnyValue elements."""
    attrs = [
        {
            "key": "arr",
            "value": {
                "arrayValue": {
                    "values": [
                        {"stringValue": "a"},
                        {"intValue": "1"},
                        {"doubleValue": 2.5},
                    ]
                }
            },
        }
    ]
    assert OtelTraceIngester._attrs_to_dict(attrs) == {
        "arr": ["a", 1, 2.5],
    }


def test_attrs_to_dict_handles_empty_array_value_dict() -> None:
    """A None/empty arrayValue entry must not crash — yields an empty list."""
    attrs = [{"key": "arr", "value": {"arrayValue": None}}]
    assert OtelTraceIngester._attrs_to_dict(attrs) == {"arr": []}


def test_attrs_to_dict_unwraps_kvlist_value() -> None:
    """kvlistValue flattens into a nested dict of unwrapped primitives."""
    attrs = [
        {
            "key": "kv",
            "value": {
                "kvlistValue": {
                    "values": [
                        {"key": "sub1", "value": {"stringValue": "v"}},
                        {"key": "sub2", "value": {"intValue": "7"}},
                    ]
                }
            },
        }
    ]
    assert OtelTraceIngester._attrs_to_dict(attrs) == {
        "kv": {"sub1": "v", "sub2": 7},
    }


def test_attrs_to_dict_handles_none_kvlist_inner_dict() -> None:
    """Crash-resistant when inner kvlistValue shape is None/empty."""
    attrs = [{"key": "kv", "value": {"kvlistValue": None}}]
    assert OtelTraceIngester._attrs_to_dict(attrs) == {"kv": {}}


def test_attrs_to_dict_skips_entries_without_key() -> None:
    """An entry with key=None is silently dropped (defensive)."""
    attrs = [
        {"value": {"stringValue": "orphan"}},
        {"key": "kept", "value": {"stringValue": "yes"}},
    ]
    assert OtelTraceIngester._attrs_to_dict(attrs) == {"kept": "yes"}


def test_attrs_to_dict_tolerates_none_attrs_argument() -> None:
    """None attrs returns an empty dict rather than raising."""
    assert OtelTraceIngester._attrs_to_dict(None) == {}


def test_attrs_to_dict_handles_none_value_entry() -> None:
    """A value entry of None yields None — never raises."""
    attrs = [{"key": "k", "value": None}]
    assert OtelTraceIngester._attrs_to_dict(attrs) == {"k": None}


def test_attrs_to_dict_tolerates_empty_value_dict() -> None:
    """An empty value dict yields None (no recognized variant)."""
    attrs = [{"key": "k", "value": {}}]
    assert OtelTraceIngester._attrs_to_dict(attrs) == {"k": None}


def test_attrs_to_dict_stringifies_unknown_wrapper() -> None:
    """Unknown AnyValue variants are stringified so embedding still has text."""
    attrs = [{"key": "exotic", "value": {"customValue": {"foo": "bar"}}}]
    result = OtelTraceIngester._attrs_to_dict(attrs)
    assert isinstance(result["exotic"], str)
    assert "customValue" in result["exotic"]


# ---------------------------------------------------------------------------
# _now_unix_nano
# ---------------------------------------------------------------------------


def test_now_unix_nano_returns_int_close_to_wall_clock() -> None:
    """_now_unix_nano should be int and within a generous bound of time.time_ns()."""
    before = time.time_ns()
    nano = OtelTraceIngester._now_unix_nano()
    after = time.time_ns()
    assert isinstance(nano, int)
    assert before - 1_000_000_000 <= nano <= after + 1_000_000_000


# ---------------------------------------------------------------------------
# _normalize_span
# ---------------------------------------------------------------------------


def test_normalize_span_without_task_class_attribute() -> None:
    """A span with no task.class attribute produces empty attributes metadata."""
    ingester = _make_ingester()
    span = {
        "traceId": "abc",
        "spanId": "def",
        "name": "n",
        "startTimeUnixNano": "1700000000000000000",
        "attributes": [],  # no task.class
    }
    record = ingester._normalize_span(span, system_id="svc", embedding=[0.0, 0.1])
    assert record.metadata["attributes"] == {}
    assert "task_class" not in record.metadata["attributes"]


def test_normalize_span_without_attributes_field_at_all() -> None:
    """A span with no 'attributes' key at all still produces a record."""
    ingester = _make_ingester()
    span = {
        "traceId": "abc",
        "spanId": "def",
        "name": "n",
        "startTimeUnixNano": "1700000000000000000",
    }
    record = ingester._normalize_span(span, system_id="svc", embedding=[0.0])
    assert record.metadata["attributes"] == {}


def test_normalize_span_drops_trace_and_span_id_from_content() -> None:
    """traceId/spanId are removed from the JSON content payload."""
    ingester = _make_ingester()
    span = {
        "traceId": "trace-xyz",
        "spanId": "span-123",
        "name": "op",
        "startTimeUnixNano": "1700000000000000000",
        "attributes": [],
    }
    record = ingester._normalize_span(span, system_id="svc", embedding=[])
    # Both ids live in metadata.otel.* — never in content JSON.
    assert "trace-xyz" not in record.content
    assert "span-123" not in record.content
    assert record.metadata["otel"]["trace_id"] == "trace-xyz"
    assert record.metadata["otel"]["span_id"] == "span-123"


# ---------------------------------------------------------------------------
# _fetch_spans edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_spans_raises_without_http_client() -> None:
    """Calling _fetch_spans before start() raises RuntimeError."""
    ingester = _make_ingester()
    # No start() call, so _http_client is None.
    with pytest.raises(RuntimeError, match="HTTP client not initialized"):
        await ingester._fetch_spans(since_unix_nano=0)


@pytest.mark.asyncio
async def test_fetch_spans_handles_empty_resource_spans() -> None:
    """An empty resourceSpans list returns an empty list."""
    ingester = _make_ingester()
    await ingester.start()
    try:
        mock_response = MagicMock()
        mock_response.json.return_value = {"resourceSpans": []}
        mock_response.raise_for_status = MagicMock()
        ingester._http_client.get = AsyncMock(return_value=mock_response)

        spans = await ingester._fetch_spans(since_unix_nano=0)
        assert spans == []
    finally:
        await ingester.stop()


@pytest.mark.asyncio
async def test_fetch_spans_defaults_system_id_to_unknown() -> None:
    """No service.name attribute → system_id becomes 'unknown'."""
    ingester = _make_ingester()
    await ingester.start()
    try:
        body = {
            "resourceSpans": [
                {
                    "resource": {"attributes": []},  # no service.name
                    "scopeSpans": [{"spans": [{"name": "x"}]}],
                }
            ]
        }
        mock_response = MagicMock()
        mock_response.json.return_value = body
        mock_response.raise_for_status = MagicMock()
        ingester._http_client.get = AsyncMock(return_value=mock_response)

        spans = await ingester._fetch_spans(since_unix_nano=0)
        assert len(spans) == 1
        assert spans[0][0] == "unknown"
    finally:
        await ingester.stop()


@pytest.mark.asyncio
async def test_fetch_spans_handles_missing_resource_attributes() -> None:
    """A resource with no 'attributes' key defaults to 'unknown'."""
    ingester = _make_ingester()
    await ingester.start()
    try:
        body = {
            "resourceSpans": [
                {
                    "resource": {},  # no attributes key at all
                    "scopeSpans": [{"spans": [{"name": "x"}]}],
                }
            ]
        }
        mock_response = MagicMock()
        mock_response.json.return_value = body
        mock_response.raise_for_status = MagicMock()
        ingester._http_client.get = AsyncMock(return_value=mock_response)

        spans = await ingester._fetch_spans(since_unix_nano=0)
        assert spans[0][0] == "unknown"
    finally:
        await ingester.stop()


@pytest.mark.asyncio
async def test_fetch_spans_handles_none_attributes_list() -> None:
    """A None attributes list is tolerated (uses `or []` fallback)."""
    ingester = _make_ingester()
    await ingester.start()
    try:
        body = {
            "resourceSpans": [
                {
                    "resource": {"attributes": None},
                    "scopeSpans": [{"spans": [{"name": "x"}]}],
                }
            ]
        }
        mock_response = MagicMock()
        mock_response.json.return_value = body
        mock_response.raise_for_status = MagicMock()
        ingester._http_client.get = AsyncMock(return_value=mock_response)

        spans = await ingester._fetch_spans(since_unix_nano=0)
        assert spans[0][0] == "unknown"
    finally:
        await ingester.stop()


@pytest.mark.asyncio
async def test_fetch_spans_extracts_service_name_int_value() -> None:
    """service.name with intValue wrapper is stringified."""
    ingester = _make_ingester()
    await ingester.start()
    try:
        body = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [{"key": "service.name", "value": {"intValue": "42"}}]
                    },
                    "scopeSpans": [{"spans": [{"name": "x"}]}],
                }
            ]
        }
        mock_response = MagicMock()
        mock_response.json.return_value = body
        mock_response.raise_for_status = MagicMock()
        ingester._http_client.get = AsyncMock(return_value=mock_response)

        spans = await ingester._fetch_spans(since_unix_nano=0)
        assert spans[0][0] == "42"
    finally:
        await ingester.stop()


@pytest.mark.asyncio
async def test_fetch_spans_extracts_service_name_bool_value() -> None:
    """service.name with boolValue wrapper is stringified."""
    ingester = _make_ingester()
    await ingester.start()
    try:
        body = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [{"key": "service.name", "value": {"boolValue": True}}]
                    },
                    "scopeSpans": [{"spans": [{"name": "x"}]}],
                }
            ]
        }
        mock_response = MagicMock()
        mock_response.json.return_value = body
        mock_response.raise_for_status = MagicMock()
        ingester._http_client.get = AsyncMock(return_value=mock_response)

        spans = await ingester._fetch_spans(since_unix_nano=0)
        assert spans[0][0] == "True"
    finally:
        await ingester.stop()


@pytest.mark.asyncio
async def test_fetch_spans_extracts_service_name_double_value() -> None:
    """service.name with doubleValue wrapper is stringified."""
    ingester = _make_ingester()
    await ingester.start()
    try:
        body = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [{"key": "service.name", "value": {"doubleValue": 1.5}}]
                    },
                    "scopeSpans": [{"spans": [{"name": "x"}]}],
                }
            ]
        }
        mock_response = MagicMock()
        mock_response.json.return_value = body
        mock_response.raise_for_status = MagicMock()
        ingester._http_client.get = AsyncMock(return_value=mock_response)

        spans = await ingester._fetch_spans(since_unix_nano=0)
        assert spans[0][0] == "1.5"
    finally:
        await ingester.stop()


@pytest.mark.asyncio
async def test_fetch_spans_skips_non_service_name_attributes() -> None:
    """A leading non-service.name attribute is iterated past, then
    service.name is found on a later attr. The `continue` branch is hit."""
    ingester = _make_ingester()
    await ingester.start()
    try:
        body = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "host.name",
                                "value": {"stringValue": "host-A"},
                            },
                            {
                                "key": "service.name",
                                "value": {"stringValue": "the-service"},
                            },
                        ]
                    },
                    "scopeSpans": [{"spans": [{"name": "x"}]}],
                }
            ]
        }
        mock_response = MagicMock()
        mock_response.json.return_value = body
        mock_response.raise_for_status = MagicMock()
        ingester._http_client.get = AsyncMock(return_value=mock_response)

        spans = await ingester._fetch_spans(since_unix_nano=0)
        assert spans[0][0] == "the-service"
    finally:
        await ingester.stop()


@pytest.mark.asyncio
async def test_fetch_spans_handles_service_name_with_none_value_entry() -> None:
    """service.name with a None value falls back to 'unknown'."""
    ingester = _make_ingester()
    await ingester.start()
    try:
        body = {
            "resourceSpans": [
                {
                    "resource": {"attributes": [{"key": "service.name", "value": None}]},
                    "scopeSpans": [{"spans": [{"name": "x"}]}],
                }
            ]
        }
        mock_response = MagicMock()
        mock_response.json.return_value = body
        mock_response.raise_for_status = MagicMock()
        ingester._http_client.get = AsyncMock(return_value=mock_response)

        spans = await ingester._fetch_spans(since_unix_nano=0)
        assert spans[0][0] == "unknown"
    finally:
        await ingester.stop()


@pytest.mark.asyncio
async def test_fetch_spans_handles_missing_scope_spans() -> None:
    """A resourceSpans with no scopeSpans returns no spans (not error)."""
    ingester = _make_ingester()
    await ingester.start()
    try:
        body = {
            "resourceSpans": [
                {
                    "resource": {"attributes": []},
                    # no scopeSpans key
                }
            ]
        }
        mock_response = MagicMock()
        mock_response.json.return_value = body
        mock_response.raise_for_status = MagicMock()
        ingester._http_client.get = AsyncMock(return_value=mock_response)

        spans = await ingester._fetch_spans(since_unix_nano=0)
        assert spans == []
    finally:
        await ingester.stop()


@pytest.mark.asyncio
async def test_fetch_spans_handles_scope_spans_without_spans() -> None:
    """A scopeSpans entry without 'spans' key contributes nothing."""
    ingester = _make_ingester()
    await ingester.start()
    try:
        body = {
            "resourceSpans": [
                {
                    "resource": {"attributes": []},
                    "scopeSpans": [{}],  # no 'spans' key
                }
            ]
        }
        mock_response = MagicMock()
        mock_response.json.return_value = body
        mock_response.raise_for_status = MagicMock()
        ingester._http_client.get = AsyncMock(return_value=mock_response)

        spans = await ingester._fetch_spans(since_unix_nano=0)
        assert spans == []
    finally:
        await ingester.stop()


@pytest.mark.asyncio
async def test_fetch_spans_unwraps_multiple_scope_spans() -> None:
    """Multiple scopeSpans/spans entries are flattened into one list."""
    ingester = _make_ingester()
    await ingester.start()
    try:
        body = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [{"key": "service.name", "value": {"stringValue": "svc"}}]
                    },
                    "scopeSpans": [
                        {"spans": [{"name": "a"}]},
                        {"spans": [{"name": "b"}, {"name": "c"}]},
                    ],
                }
            ]
        }
        mock_response = MagicMock()
        mock_response.json.return_value = body
        mock_response.raise_for_status = MagicMock()
        ingester._http_client.get = AsyncMock(return_value=mock_response)

        spans = await ingester._fetch_spans(since_unix_nano=0)
        assert len(spans) == 3
        assert [s[1]["name"] for s in spans] == ["a", "b", "c"]
        assert all(s[0] == "svc" for s in spans)
    finally:
        await ingester.stop()


@pytest.mark.asyncio
async def test_fetch_spans_uses_query_params_since() -> None:
    """The HTTP GET includes a `since` query parameter from the arg."""
    ingester = _make_ingester()
    await ingester.start()
    try:
        mock_response = MagicMock()
        mock_response.json.return_value = {"resourceSpans": []}
        mock_response.raise_for_status = MagicMock()
        ingester._http_client.get = AsyncMock(return_value=mock_response)

        await ingester._fetch_spans(since_unix_nano=1700000000000000000)

        ingester._http_client.get.assert_awaited_once()
        kwargs = ingester._http_client.get.await_args.kwargs
        assert kwargs["params"] == {"since": "1700000000000000000"}
    finally:
        await ingester.stop()


@pytest.mark.asyncio
async def test_fetch_spans_propagates_raise_for_status_failure() -> None:
    """A non-2xx response (raise_for_status raises) is propagated."""
    ingester = _make_ingester()
    await ingester.start()
    try:
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock()
        )
        ingester._http_client.get = AsyncMock(return_value=mock_response)

        with pytest.raises(httpx.HTTPStatusError):
            await ingester._fetch_spans(since_unix_nano=0)
    finally:
        await ingester.stop()


# ---------------------------------------------------------------------------
# _polling_loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_polling_loop_cold_start_uses_lookback_window() -> None:
    """First cycle with no watermark passes since = now - initial_lookback_seconds."""
    ingester = _make_ingester(initial_lookback_seconds=120)
    ingester._running = True
    captured_since: list[int] = []

    async def fake_fetch(since_unix_nano: int) -> list[tuple[str, dict[str, Any]]]:
        captured_since.append(since_unix_nano)
        # Stop the loop after one cycle.
        ingester._running = False
        return []

    ingester._fetch_spans = fake_fetch  # type: ignore[assignment]
    # Patch sleep so we don't wait.
    with patch("akosha.ingestion.otel_ingester.asyncio.sleep", new=AsyncMock()):
        await ingester._polling_loop()

    assert len(captured_since) == 1
    now = ingester._now_unix_nano()
    # Cold start: since should be roughly now - 120s in nanos.
    assert now - captured_since[0] == pytest.approx(120 * 1_000_000_000, rel=0.05)


@pytest.mark.asyncio
async def test_polling_loop_warm_cycle_uses_max_watermark() -> None:
    """Subsequent cycles use max(watermarks.values()) as `since`."""
    ingester = _make_ingester()
    ingester._running = True
    ingester._watermarks = {"a": 1000, "b": 2000, "c": 500}
    captured_since: list[int] = []

    async def fake_fetch(since_unix_nano: int) -> list[tuple[str, dict[str, Any]]]:
        captured_since.append(since_unix_nano)
        ingester._running = False
        return []

    ingester._fetch_spans = fake_fetch  # type: ignore[assignment]
    with patch("akosha.ingestion.otel_ingester.asyncio.sleep", new=AsyncMock()):
        await ingester._polling_loop()

    assert captured_since == [2000]


@pytest.mark.asyncio
async def test_polling_loop_embeds_and_inserts_spans() -> None:
    """A non-empty fetch iterates, embeds, inserts, and advances watermark."""
    ingester = _make_ingester(
        embedding_service=MagicMock(
            generate_embedding=AsyncMock(return_value=np.zeros(8, dtype=np.float32))
        ),
    )
    ingester._running = True
    ingester.hot_store.insert = AsyncMock()

    span_a = {
        "traceId": "t1",
        "spanId": "s1",
        "name": "op.a",
        "startTimeUnixNano": "1500",
        "attributes": [{"key": "task.class", "value": {"stringValue": "X"}}],
    }
    span_b = {
        "traceId": "t2",
        "spanId": "s2",
        "name": "op.b",
        "startTimeUnixNano": "1800",
        "attributes": [],
    }

    async def fake_fetch(since_unix_nano: int) -> list[tuple[str, dict[str, Any]]]:
        ingester._running = False
        return [("svc-a", span_a), ("svc-b", span_b)]

    ingester._fetch_spans = fake_fetch  # type: ignore[assignment]
    with patch("akosha.ingestion.otel_ingester.asyncio.sleep", new=AsyncMock()):
        await ingester._polling_loop()

    assert ingester.hot_store.insert.await_count == 2
    assert ingester._watermarks["svc-a"] == 1500
    assert ingester._watermarks["svc-b"] == 1800
    assert ingester._last_poll_at is not None
    assert ingester._cycles_total >= 1


@pytest.mark.asyncio
async def test_polling_loop_swallows_per_span_exception() -> None:
    """A per-span error is logged, _errors_total incremented, loop continues."""
    ingester = _make_ingester(
        embedding_service=MagicMock(generate_embedding=AsyncMock(side_effect=RuntimeError("boom"))),
    )
    ingester._running = True
    ingester.hot_store.insert = AsyncMock()

    bad_span = {
        "traceId": "t1",
        "spanId": "s1",
        "name": "op",
        "startTimeUnixNano": "1500",
        "attributes": [],
    }

    cycles = {"n": 0}

    async def fake_fetch(since_unix_nano: int) -> list[tuple[str, dict[str, Any]]]:
        cycles["n"] += 1
        ingester._running = False
        return [("svc", bad_span)]

    ingester._fetch_spans = fake_fetch  # type: ignore[assignment]
    with patch("akosha.ingestion.otel_ingester.asyncio.sleep", new=AsyncMock()):
        await ingester._polling_loop()

    assert ingester._errors_total == 1
    assert "svc" not in ingester._watermarks
    ingester.hot_store.insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_polling_loop_outer_exception_logs_and_continues() -> None:
    """An exception in the polling loop itself is caught; next cycle proceeds."""
    ingester = _make_ingester()
    ingester._running = True
    cycles = {"n": 0}

    async def fake_fetch(since_unix_nano: int) -> list[tuple[str, dict[str, Any]]]:
        cycles["n"] += 1
        if cycles["n"] == 1:
            raise RuntimeError("transport down")
        ingester._running = False
        return []

    ingester._fetch_spans = fake_fetch  # type: ignore[assignment]
    with patch("akosha.ingestion.otel_ingester.asyncio.sleep", new=AsyncMock()):
        await ingester._polling_loop()

    assert cycles["n"] == 2
    assert ingester._errors_total == 1


@pytest.mark.asyncio
async def test_polling_loop_breaker_on_cancellation() -> None:
    """CancelledError breaks the loop (cycle-level catch)."""
    ingester = _make_ingester()
    ingester._running = True

    async def fake_fetch(since_unix_nano: int) -> list[tuple[str, dict[str, Any]]]:
        raise asyncio.CancelledError()

    ingester._fetch_spans = fake_fetch  # type: ignore[assignment]
    # Must not raise — CancelledError inside the loop must be swallowed
    # by the outer try/except (it logs + breaks).
    await ingester._polling_loop()


@pytest.mark.asyncio
async def test_polling_loop_per_span_cancellation_re_raises() -> None:
    """CancelledError raised mid-batch (per-span) propagates so the
    cycle-level catch breaks the loop."""
    ingester = _make_ingester(
        embedding_service=MagicMock(
            generate_embedding=AsyncMock(side_effect=asyncio.CancelledError())
        ),
    )
    ingester._running = True
    ingester.hot_store.insert = AsyncMock()

    cancel_span = {
        "traceId": "t",
        "spanId": "s",
        "name": "op",
        "startTimeUnixNano": "1700000000000000000",
        "attributes": [],
    }

    async def fake_fetch(since_unix_nano: int) -> list[tuple[str, dict[str, Any]]]:
        ingester._running = False
        return [("svc", cancel_span)]

    ingester._fetch_spans = fake_fetch  # type: ignore[assignment]
    with patch("akosha.ingestion.otel_ingester.asyncio.sleep", new=AsyncMock()):
        # The per-span CancelledError is re-raised so the outer cycle-level
        # handler can catch it. The outer handler swallows + logs + breaks.
        await ingester._polling_loop()

    # Span-level CancelledError must NOT count as a span error.
    assert ingester._errors_total == 0
    ingester.hot_store.insert.assert_not_awaited()


# ---------------------------------------------------------------------------
# stop() — unexpected exception path during shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_swallows_unexpected_exception_from_poll_task() -> None:
    """If the poll task raises a non-CancelledError at shutdown, stop() recovers."""
    ingester = _make_ingester()
    ingester._running = True

    async def raise_runtime() -> None:
        raise RuntimeError("poll task exploded")

    ingester._poll_task = asyncio.ensure_future(raise_runtime())
    # Suppress the unraisable warning for the always-erroring coroutine.
    ingester._poll_task.add_done_callback(lambda _t: None)

    ingester._http_client = MagicMock()
    ingester._http_client.aclose = AsyncMock()

    # Must not raise — stop() awaits the task, catches the exception via
    # `except Exception:` + logger.exception, and continues.
    await ingester.stop()
    assert ingester._running is False
    assert ingester._http_client is None


# ---------------------------------------------------------------------------
# Initial lookback parameter is honored
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initial_lookback_parameter_is_honored_on_first_cycle() -> None:
    """Different initial_lookback_seconds produce a different first-cycle since."""
    ingester = _make_ingester(initial_lookback_seconds=10)
    ingester._running = True
    captured: list[int] = []

    async def fake_fetch(since_unix_nano: int) -> list[tuple[str, dict[str, Any]]]:
        captured.append(since_unix_nano)
        ingester._running = False
        return []

    ingester._fetch_spans = fake_fetch  # type: ignore[assignment]
    with patch("akosha.ingestion.otel_ingester.asyncio.sleep", new=AsyncMock()):
        await ingester._polling_loop()

    now = ingester._now_unix_nano()
    delta_ns = now - captured[0]
    # initial_lookback_seconds = 10 → delta ≈ 10s in nanos (±50ms slack).
    assert abs(delta_ns - 10 * 1_000_000_000) < 50_000_000


# ---------------------------------------------------------------------------
# Watermark advancement only on successful insert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watermark_only_advances_after_successful_insert() -> None:
    """Watermark advances iff hot_store.insert completes successfully."""
    ingester = _make_ingester(
        embedding_service=MagicMock(
            generate_embedding=AsyncMock(return_value=np.zeros(4, dtype=np.float32))
        ),
    )
    ingester.hot_store.insert = AsyncMock(side_effect=RuntimeError("store down"))

    span = {
        "traceId": "t",
        "spanId": "s",
        "name": "op",
        "startTimeUnixNano": "1234",
        "attributes": [],
    }
    with pytest.raises(RuntimeError, match="store down"):
        await ingester._ingest_span(span, system_id="svc")

    # Watermark must NOT advance since insert failed.
    assert "svc" not in ingester._watermarks
