"""Tests for :mod:`akosha.ingestion.bodai_event_subscriber`.

Coverage was at ~68% before these tests. The subscriber is a Redis
Streams consumer with exponential backoff, a watermark-based resume
mechanism, and a per-event timeout pipeline. We mock the redis client,
the embedding service, and the HotStore so each branch is reachable in
isolation.

Branch matrix pinned by these tests:

* ``_create_redis_client`` — ImportError path returns ``None``
* ``start`` — early-returns when ``_running``, ``hot_store=None``, redis
  import failure, ``xgroup_create`` non-BUSYGROUP failure, resume-id
  read failure (falls back to ``">"``)
* ``stop`` — idempotent (running=False, no task, no client)
* ``_ensure_consumer_group`` — first-time create + BUSYGROUP tolerance
* ``_resume_id`` / ``_resume_id_async`` — bump ms-seq, malformed
  watermark, missing watermark
* ``_read_watermark_async`` — hot_store=None, search_similar exception,
  str metadata decoded, bytes last_message_id, non-dict row, non-dict
  metadata, missing last_message_id
* ``_run_loop`` — xreadgroup exception → backoff, empty response, normal
  process path
* ``_process_entry`` — normalize None, decode None + xack, topic mismatch,
  per-event timeout, generic exception, indexed=True (watermark+xack),
  indexed=False (xack only)
* ``_normalize_stream_entry`` — bytes vs str, bytes-vs-bytearray values,
  non-dict fields, unicode decode failure, malformed entry
* ``_decode_envelope`` — envelope=JSON path, direct triplet path,
  missing topic/payload_json, malformed JSON, payload not a dict
* ``_index_envelope`` — schema mismatch, empty content, success path
* ``_build_content`` — formats all fields with sensible defaults
* ``_parse_timestamp`` — ISO-8601 string, missing → ``now()``
* ``_update_watermark`` — conn missing raises, exception swallowed,
  success path
* ``_safe_xack`` — happy path + exception swallowed
* ``_sleep_with_cancel`` / ``_wait_until_stopped`` — sleep + cancel loops
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from akosha.ingestion.bodai_event_subscriber import (
    DEFAULT_RECONNECT_BACKOFF_MAX_SECONDS,
    DEFAULT_RECONNECT_BACKOFF_SECONDS,
    STREAM_NAME,
    SUPPORTED_SCHEMA_VERSION,
    SYSTEM_ID_MAHAVISHNU,
    TOOL_INVOCATION_TOPIC,
    WATERMARK_CONTENT,
    WATERMARK_CONVERSATION_ID,
    BodaiToolInvocationSubscriber,
    _create_redis_client,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeRedisClient:
    """Tiny stub mimicking ``redis.asyncio.Redis``.

    Provides async methods used by the subscriber (``xgroup_create``,
    ``xreadgroup``, ``xack``, ``aclose``, ``close``). Each method is
    individually configurable so each test can decide what it returns
    or raises.
    """

    def __init__(self) -> None:
        self.xgroup_create = AsyncMock()
        self.xreadgroup = AsyncMock(return_value=[])
        self.xack = AsyncMock()
        self.aclose = AsyncMock()
        self.close = AsyncMock()


_HOT_STORE_SENTINEL = object()


def _make_subscriber(
    *,
    hot_store: Any | None = _HOT_STORE_SENTINEL,  # type: ignore[assignment]
    redis_url: str = "redis://localhost:6379/0",
    consumer_group: str = "akosha-tool-invocation-indexers",
    **kwargs: Any,
) -> BodaiToolInvocationSubscriber:
    """Build a subscriber with sane test defaults.

    Pass ``hot_store=None`` to disable the HotStore; omit ``hot_store``
    entirely to get a MagicMock default. The sentinel distinguishes
    "caller passed None" from "caller passed nothing".
    """
    if hot_store is _HOT_STORE_SENTINEL:
        chosen: Any | None = MagicMock()
    else:
        chosen = hot_store
    return BodaiToolInvocationSubscriber(
        redis_url=redis_url,
        consumer_group=consumer_group,
        hot_store=chosen,
        **kwargs,
    )


def _envelope_triplet(
    *,
    topic: str = TOOL_INVOCATION_TOPIC,
    payload: dict[str, Any] | None = None,
    event_id: str | None = "evt-1",
    headers: dict[str, Any] | None = None,
    source: str | None = "mahavishnu",
) -> dict[str, Any]:
    """Build a Phase 1 wire-shape stream payload (direct triplet)."""
    body = (
        payload
        if payload is not None
        else {
            "version": SUPPORTED_SCHEMA_VERSION,
            "tool": "search_all_systems",
            "surface": "claude_code",
            "result": "ok",
            "duration_ms": 12,
            "error": "",
        }
    )
    return {
        "topic": topic,
        "payload_json": json.dumps(body),
        "headers_json": json.dumps(headers or {}),
        "event_id": event_id,
        "source": source,
    }


def _envelope_blob_envelope(
    *,
    topic: str = TOOL_INVOCATION_TOPIC,
    payload: dict[str, Any] | None = None,
    event_id: str = "evt-1",
) -> dict[str, Any]:
    """Build an envelope=JSON-shape stream payload (canonical)."""
    return {
        "envelope": json.dumps(
            {
                "topic": topic,
                "payload": payload or {"k": "v"},
                "event_id": event_id,
                "headers": {"event_id": event_id},
            }
        ),
    }


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


class TestCreateRedisClient:
    """``_create_redis_client`` returns ``None`` when redis is unavailable."""

    def test_returns_none_when_redis_asyncio_import_fails(self) -> None:
        """An ImportError on ``redis.asyncio`` returns ``None`` (fail-soft)."""
        import builtins

        original_import = builtins.__import__

        def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "redis.asyncio" or name.startswith("redis"):
                raise ImportError("simulated missing redis")
            return original_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=fake_import):
            result = _create_redis_client("redis://localhost:6379/0")
        assert result is None

    def test_returns_client_when_redis_asyncio_is_available(self) -> None:
        """When redis.asyncio is importable, the helper returns a client."""
        fake_module = MagicMock()
        fake_aioredis = MagicMock()
        fake_aioredis.from_url = MagicMock(return_value="FAKE_CLIENT")
        fake_module.asyncio = fake_aioredis
        with patch.dict("sys.modules", {"redis": fake_module, "redis.asyncio": fake_aioredis}):
            result = _create_redis_client("redis://localhost:6379/0")
        assert result == "FAKE_CLIENT"
        fake_aioredis.from_url.assert_called_once()


# ---------------------------------------------------------------------------
# Constructor / properties
# ---------------------------------------------------------------------------


class TestConstructor:
    """The constructor captures init args verbatim."""

    def test_stores_redis_url_and_consumer_group(self) -> None:
        s = _make_subscriber(
            redis_url="redis://other:9999/1",
            consumer_group="custom-group",
        )
        assert s._redis_url == "redis://other:9999/1"
        assert s._consumer_group == "custom-group"

    def test_defaults(self) -> None:
        s = _make_subscriber()
        assert s._xreadgroup_block_ms == 1500
        assert s._per_event_timeout_seconds == 30.0
        assert s._consumer_name is None
        assert s._running is False
        assert s._task is None
        assert s._redis_client is None

    def test_running_property_reflects_internal_state(self) -> None:
        s = _make_subscriber()
        assert s.running is False
        s._running = True
        assert s.running is True

    def test_embedding_dim_reads_hot_store_attr(self) -> None:
        hot_store = MagicMock()
        hot_store._embedding_dim = 768
        s = _make_subscriber(hot_store=hot_store)
        assert s.embedding_dim == 768

    def test_embedding_dim_defaults_to_384_when_hot_store_lacks_attr(self) -> None:
        s = _make_subscriber(hot_store=MagicMock(spec=[]))
        assert s.embedding_dim == 384


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


class TestStartNoopBranches:
    """``start`` short-circuits when prerequisites are unmet."""

    @pytest.mark.asyncio
    async def test_returns_when_already_running(self) -> None:
        s = _make_subscriber()
        s._running = True
        await s.start()
        # No client was constructed; no task was started.
        assert s._task is None
        assert s._redis_client is None

    @pytest.mark.asyncio
    async def test_noop_when_hot_store_is_none(self, caplog: pytest.LogCaptureFixture) -> None:
        s = _make_subscriber(hot_store=None)
        with caplog.at_level(logging.DEBUG, logger="akosha.ingestion.bodai_event_subscriber"):
            await s.start()
        assert s._running is False
        assert any("no hot_store" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_falls_back_when_redis_import_fails(self) -> None:
        """``_create_redis_client`` returning ``None`` keeps the subscriber non-running."""
        s = _make_subscriber()
        with patch(
            "akosha.ingestion.bodai_event_subscriber._create_redis_client",
            return_value=None,
        ):
            await s.start()
        assert s._running is False
        assert s._task is None

    @pytest.mark.asyncio
    async def test_falls_back_when_xgroup_create_raises_non_busygroup(
        self,
    ) -> None:
        """A non-BUSYGROUP error during group creation logs WARNING and exits."""
        s = _make_subscriber()
        client = _FakeRedisClient()
        client.xgroup_create.side_effect = RuntimeError("connection refused")
        with patch(
            "akosha.ingestion.bodai_event_subscriber._create_redis_client",
            return_value=client,
        ):
            await s.start()
        assert s._running is False
        # The failed client is closed so we don't leak the connection.
        client.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_resume_id_failure_falls_back_to_greater_than(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When ``_resume_id_async`` raises, the loop still starts from ``>``."""
        s = _make_subscriber()
        client = _FakeRedisClient()
        client.xreadgroup.side_effect = [
            # First call returns empty so the loop exits immediately.
            [],
            asyncio.CancelledError(),
        ]
        with (
            patch(
                "akosha.ingestion.bodai_event_subscriber._create_redis_client",
                return_value=client,
            ),
            patch.object(
                s, "_resume_id_async", AsyncMock(side_effect=RuntimeError("watermark db down"))
            ),
            caplog.at_level(
                logging.INFO,
                logger="akosha.ingestion.bodai_event_subscriber",
            ),
        ):
            await s.start()
            await asyncio.sleep(0)
            s._running = False
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await s._task
        # The subscriber should be running and logged resume_id=">".
        assert any("resume_id=>" in rec.message for rec in caplog.records)


class TestStartHappyPath:
    """``start`` connects, ensures the group, resumes from watermark, spawns loop."""

    @pytest.mark.asyncio
    async def test_starts_loop_and_logs_resume_id(self, caplog: pytest.LogCaptureFixture) -> None:
        s = _make_subscriber()
        client = _FakeRedisClient()
        # Empty response + cancel so the loop terminates immediately.
        client.xreadgroup.side_effect = [
            [],
            asyncio.CancelledError(),
        ]
        with (
            patch(
                "akosha.ingestion.bodai_event_subscriber._create_redis_client",
                return_value=client,
            ),
            caplog.at_level(
                logging.INFO,
                logger="akosha.ingestion.bodai_event_subscriber",
            ),
        ):
            await s.start()
            # Yield so the loop iteration runs and CancelledError fires.
            await asyncio.sleep(0)
            s._running = False
            try:
                if s._task is not None:
                    await s._task
            except asyncio.CancelledError, Exception:
                pass
        assert s._redis_client is client
        # Confirm xgroup_create + xreadgroup both ran.
        client.xgroup_create.assert_awaited_once()
        client.xreadgroup.assert_awaited()
        assert any("started" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


class TestStop:
    """``stop`` is idempotent and tolerates missing task / client."""

    @pytest.mark.asyncio
    async def test_stop_when_never_started(self) -> None:
        s = _make_subscriber()
        await s.stop()
        assert s._running is False
        assert s._redis_client is None

    @pytest.mark.asyncio
    async def test_stop_cancels_running_task(self) -> None:
        s = _make_subscriber()
        s._running = True

        async def _loop() -> None:
            await asyncio.sleep(60)

        s._task = asyncio.create_task(_loop())
        await asyncio.sleep(0)  # let the task start
        await s.stop()
        assert s._task is None
        assert s._running is False

    @pytest.mark.asyncio
    async def test_stop_closes_redis_client(self) -> None:
        s = _make_subscriber()
        client = _FakeRedisClient()
        s._redis_client = client
        await s.stop()
        client.aclose.assert_awaited_once()
        assert s._redis_client is None

    @pytest.mark.asyncio
    async def test_stop_falls_back_to_close_when_aclose_raises(self) -> None:
        """If ``aclose`` raises, the subscriber swallows and tries ``close``."""
        s = _make_subscriber()
        client = _FakeRedisClient()
        client.aclose.side_effect = RuntimeError("aclose unavailable")
        s._redis_client = client
        await s.stop()
        client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_swallows_cancellation_error(self) -> None:
        s = _make_subscriber()
        s._running = True

        async def _loop() -> None:
            await asyncio.sleep(60)

        s._task = asyncio.create_task(_loop())
        await asyncio.sleep(0)
        # Just call stop — it must not raise even though the task was cancelled.
        await s.stop()
        assert s._task is None

    @pytest.mark.asyncio
    async def test_stop_logs_warning_when_task_raises(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A non-CancelledError raised by the task is logged at WARNING.

        ``stop()`` only reaches the ``except Exception`` branch when the
        task is not yet done at call time; once a task is ``done()`` with
        an exception, awaiting it re-raises immediately without entering
        the handler. So we mount a task that swallows ``CancelledError``
        and converts it into a ``RuntimeError`` instead — that way
        ``await self._task`` raises RuntimeError, the WARNING logs, and
        ``stop()`` returns cleanly.
        """
        s = _make_subscriber()
        s._running = False

        async def _loop() -> None:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError as exc:
                # Convert the cancellation into a non-cancellation error
                # so stop()'s exception branch fires.
                raise RuntimeError("task body failed post-cancel") from exc

        s._task = asyncio.create_task(_loop())
        await asyncio.sleep(0)  # let the task reach its sleep
        assert not s._task.done()
        with caplog.at_level(logging.WARNING, logger="akosha.ingestion.bodai_event_subscriber"):
            await s.stop()
        assert any("task raised" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# _ensure_consumer_group
# ---------------------------------------------------------------------------


class TestEnsureConsumerGroup:
    """``xgroup_create`` is idempotent: BUSYGROUP is tolerated."""

    @pytest.mark.asyncio
    async def test_creates_group_on_first_call(self, caplog: pytest.LogCaptureFixture) -> None:
        s = _make_subscriber()
        client = _FakeRedisClient()
        with caplog.at_level(logging.INFO, logger="akosha.ingestion.bodai_event_subscriber"):
            await s._ensure_consumer_group(client)
        client.xgroup_create.assert_awaited_once_with(
            name=STREAM_NAME,
            groupname="akosha-tool-invocation-indexers",
            id="0",
            mkstream=True,
        )
        assert any("Created consumer group" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_busygroup_is_tolerated(self, caplog: pytest.LogCaptureFixture) -> None:
        s = _make_subscriber()
        client = _FakeRedisClient()
        client.xgroup_create.side_effect = Exception("BUSYGROUP Consumer Group already exists")
        with caplog.at_level(logging.DEBUG, logger="akosha.ingestion.bodai_event_subscriber"):
            await s._ensure_consumer_group(client)
        assert any("already exists" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_other_errors_propagate(self) -> None:
        """A non-BUSYGROUP error is re-raised so ``start`` falls back to polling."""
        s = _make_subscriber()
        client = _FakeRedisClient()
        client.xgroup_create.side_effect = ConnectionError("refused")
        with pytest.raises(ConnectionError):
            await s._ensure_consumer_group(client)


# ---------------------------------------------------------------------------
# Watermark resolution
# ---------------------------------------------------------------------------


class TestReadWatermarkAsync:
    """``_read_watermark_async`` parses HotStore rows into a stream id."""

    @pytest.mark.asyncio
    async def test_returns_none_when_hot_store_is_none(self) -> None:
        s = _make_subscriber(hot_store=None)
        assert await s._read_watermark_async() is None

    @pytest.mark.asyncio
    async def test_returns_none_on_search_exception(self, caplog: pytest.LogCaptureFixture) -> None:
        hot_store = MagicMock()
        hot_store.search_similar = AsyncMock(side_effect=RuntimeError("duckdb fail"))
        s = _make_subscriber(hot_store=hot_store)
        with caplog.at_level(logging.WARNING, logger="akosha.ingestion.bodai_event_subscriber"):
            result = await s._read_watermark_async()
        assert result is None
        assert any("watermark read failed" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_returns_last_message_id_from_str_metadata(self) -> None:
        hot_store = MagicMock()
        hot_store.search_similar = AsyncMock(
            return_value=[
                {
                    "conversation_id": WATERMARK_CONVERSATION_ID,
                    "metadata": {"last_message_id": "1700000000000-0"},
                }
            ]
        )
        s = _make_subscriber(hot_store=hot_store)
        result = await s._read_watermark_async()
        assert result == "1700000000000-0"

    @pytest.mark.asyncio
    async def test_returns_last_message_id_from_bytes_metadata(self) -> None:
        hot_store = MagicMock()
        hot_store.search_similar = AsyncMock(
            return_value=[
                {
                    "conversation_id": WATERMARK_CONVERSATION_ID,
                    "metadata": {"last_message_id": b"1700000000000-1"},
                }
            ]
        )
        s = _make_subscriber(hot_store=hot_store)
        result = await s._read_watermark_async()
        assert result == "1700000000000-1"

    @pytest.mark.asyncio
    async def test_decodes_json_metadata_string(self) -> None:
        """DuckDB returns JSON columns as strings — decode them before parsing."""
        hot_store = MagicMock()
        hot_store.search_similar = AsyncMock(
            return_value=[
                {
                    "conversation_id": WATERMARK_CONVERSATION_ID,
                    "metadata": json.dumps({"last_message_id": "1700000000000-2"}),
                }
            ]
        )
        s = _make_subscriber(hot_store=hot_store)
        result = await s._read_watermark_async()
        assert result == "1700000000000-2"

    @pytest.mark.asyncio
    async def test_skips_non_watermark_rows(self) -> None:
        hot_store = MagicMock()
        hot_store.search_similar = AsyncMock(
            return_value=[
                {
                    "conversation_id": "some-real-conversation",
                    "metadata": {"last_message_id": "1-0"},
                },
                {
                    "conversation_id": WATERMARK_CONVERSATION_ID,
                    "metadata": {"last_message_id": "9-9"},
                },
            ]
        )
        s = _make_subscriber(hot_store=hot_store)
        result = await s._read_watermark_async()
        assert result == "9-9"

    @pytest.mark.asyncio
    async def test_skips_non_dict_rows(self) -> None:
        hot_store = MagicMock()
        hot_store.search_similar = AsyncMock(return_value=["not-a-dict", 42, None])
        s = _make_subscriber(hot_store=hot_store)
        assert await s._read_watermark_async() is None

    @pytest.mark.asyncio
    async def test_skips_non_dict_metadata(self) -> None:
        hot_store = MagicMock()
        hot_store.search_similar = AsyncMock(
            return_value=[
                {
                    "conversation_id": WATERMARK_CONVERSATION_ID,
                    "metadata": "not-json-at-all",
                },
            ]
        )
        s = _make_subscriber(hot_store=hot_store)
        assert await s._read_watermark_async() is None

    @pytest.mark.asyncio
    async def test_skips_missing_last_message_id(self) -> None:
        hot_store = MagicMock()
        hot_store.search_similar = AsyncMock(
            return_value=[
                {
                    "conversation_id": WATERMARK_CONVERSATION_ID,
                    "metadata": {"other_field": "value"},
                },
            ]
        )
        s = _make_subscriber(hot_store=hot_store)
        assert await s._read_watermark_async() is None


class TestResumeId:
    """``_resume_id`` / ``_resume_id_async`` bump the seq component."""

    def test_sync_resume_id_bumps_seq(self) -> None:
        s = _make_subscriber()
        with patch.object(s, "_read_watermark", return_value="1700000000000-7"):
            assert s._resume_id() == "1700000000000-8"

    def test_sync_resume_id_returns_greater_than_when_no_watermark(self) -> None:
        s = _make_subscriber()
        with patch.object(s, "_read_watermark", return_value=None):
            assert s._resume_id() == ">"

    def test_sync_resume_id_handles_malformed_watermark(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        s = _make_subscriber()
        with (
            patch.object(s, "_read_watermark", return_value="malformed-no-dash"),
            caplog.at_level(
                logging.WARNING,
                logger="akosha.ingestion.bodai_event_subscriber",
            ),
        ):
            assert s._resume_id() == ">"
        assert any("malformed watermark" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_async_resume_id_bumps_seq(self) -> None:
        s = _make_subscriber()
        with patch.object(s, "_read_watermark_async", AsyncMock(return_value="1700000000000-3")):
            assert await s._resume_id_async() == "1700000000000-4"

    @pytest.mark.asyncio
    async def test_async_resume_id_returns_greater_than_when_no_watermark(self) -> None:
        s = _make_subscriber()
        with patch.object(s, "_read_watermark_async", AsyncMock(return_value=None)):
            assert await s._resume_id_async() == ">"

    @pytest.mark.asyncio
    async def test_async_resume_id_handles_malformed_watermark(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        s = _make_subscriber()
        with (
            patch.object(s, "_read_watermark_async", AsyncMock(return_value="not-a-stream-id")),
            caplog.at_level(
                logging.WARNING,
                logger="akosha.ingestion.bodai_event_subscriber",
            ),
        ):
            assert await s._resume_id_async() == ">"
        assert any("malformed watermark" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# _normalize_stream_entry
# ---------------------------------------------------------------------------


class TestNormalizeStreamEntry:
    """``_normalize_stream_entry`` decodes redis stream entry shapes."""

    def test_returns_none_for_non_tuple_entry(self) -> None:
        assert BodaiToolInvocationSubscriber._normalize_stream_entry("nope") is None
        assert BodaiToolInvocationSubscriber._normalize_stream_entry([1]) is None
        assert BodaiToolInvocationSubscriber._normalize_stream_entry(()) is None

    def test_returns_none_when_fields_not_a_dict(self) -> None:
        assert BodaiToolInvocationSubscriber._normalize_stream_entry(["id", "not-a-dict"]) is None

    def test_decodes_bytes_keys_and_values(self) -> None:
        entry = [
            b"1700000000000-0",
            {b"topic": b"websocket_tool_invocation", b"foo": b"bar"},
        ]
        msg_id, payload = BodaiToolInvocationSubscriber._normalize_stream_entry(entry)
        assert msg_id == "1700000000000-0"
        assert payload == {"topic": "websocket_tool_invocation", "foo": "bar"}

    def test_passes_through_string_keys_and_values(self) -> None:
        entry = ["1700000000000-0", {"topic": "websocket_tool_invocation"}]
        msg_id, payload = BodaiToolInvocationSubscriber._normalize_stream_entry(entry)
        assert msg_id == "1700000000000-0"
        assert payload["topic"] == "websocket_tool_invocation"

    def test_skips_bytes_value_with_unicode_decode_error(self) -> None:
        entry = ["1700000000000-0", {b"good": b"ok", b"bad": b"\xff\xfe\xfd"}]
        msg_id, payload = BodaiToolInvocationSubscriber._normalize_stream_entry(entry)
        assert msg_id == "1700000000000-0"
        assert payload == {"good": "ok"}

    def test_passes_through_non_bytes_non_str_values(self) -> None:
        """Non-bytes/str values are kept verbatim (e.g. ints, dicts)."""
        entry = ["1700000000000-0", {"count": 42, "list": [1, 2, 3]}]
        _, payload = BodaiToolInvocationSubscriber._normalize_stream_entry(entry)
        assert payload == {"count": 42, "list": [1, 2, 3]}


# ---------------------------------------------------------------------------
# _decode_envelope
# ---------------------------------------------------------------------------


class TestDecodeEnvelope:
    """``_decode_envelope`` accepts two wire shapes."""

    @pytest.fixture
    def subscriber(self) -> BodaiToolInvocationSubscriber:
        return _make_subscriber()

    def test_returns_none_when_payload_is_not_a_dict(
        self, subscriber: BodaiToolInvocationSubscriber
    ) -> None:
        assert subscriber._decode_envelope("not-a-dict") is None  # type: ignore[arg-type]
        assert subscriber._decode_envelope(None) is None  # type: ignore[arg-type]

    def test_envelope_blob_path(self, subscriber: BodaiToolInvocationSubscriber) -> None:
        payload = _envelope_blob_envelope(
            topic="pattern.detected",
            event_id="evt-42",
            payload={"k": "v"},
        )
        result = subscriber._decode_envelope(payload)
        assert result == {
            "topic": "pattern.detected",
            "payload": {"k": "v"},
            "event_id": "evt-42",
            "headers": {"event_id": "evt-42"},
        }

    def test_envelope_blob_with_event_type_fallback(
        self, subscriber: BodaiToolInvocationSubscriber
    ) -> None:
        payload = {
            "envelope": json.dumps(
                {
                    "event_type": "from.event_type",
                    "payload": {"a": 1},
                    "headers": {},
                }
            )
        }
        result = subscriber._decode_envelope(payload)
        assert result is not None
        assert result["topic"] == "from.event_type"

    def test_envelope_blob_handles_none_topic(
        self, subscriber: BodaiToolInvocationSubscriber
    ) -> None:
        payload = {
            "envelope": json.dumps({"payload": {}, "headers": {}}),
        }
        result = subscriber._decode_envelope(payload)
        assert result is not None
        assert result["topic"] == ""
        assert result["event_id"] == ""

    def test_envelope_blob_returns_none_on_invalid_json(
        self,
        subscriber: BodaiToolInvocationSubscriber,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        payload = {"envelope": "{not-json"}
        with caplog.at_level(logging.WARNING, logger="akosha.ingestion.bodai_event_subscriber"):
            result = subscriber._decode_envelope(payload)
        assert result is None
        assert any("failed to parse envelope" in rec.message for rec in caplog.records)

    def test_envelope_blob_returns_none_when_decoded_not_dict(
        self, subscriber: BodaiToolInvocationSubscriber
    ) -> None:
        payload = {"envelope": json.dumps([1, 2, 3])}
        assert subscriber._decode_envelope(payload) is None

    def test_envelope_blob_empty_blob_skipped_to_triplet_path(
        self, subscriber: BodaiToolInvocationSubscriber
    ) -> None:
        payload = {"envelope": "", "topic": "x"}
        # Empty envelope blob is treated as "no envelope field"; falls through
        # to the triplet path, which fails (no payload_json) and returns None.
        assert subscriber._decode_envelope(payload) is None

    def test_direct_triplet_path(self, subscriber: BodaiToolInvocationSubscriber) -> None:
        body = _envelope_triplet(
            topic="websocket_tool_invocation",
            payload={"version": "1.0.0"},
            event_id="evt-7",
            headers={"traceparent": "abc"},
        )
        result = subscriber._decode_envelope(body)
        assert result == {
            "topic": "websocket_tool_invocation",
            "payload": {"version": "1.0.0"},
            "event_id": "evt-7",
            "headers": {"traceparent": "abc"},
            "source": "mahavishnu",
        }

    def test_direct_triplet_payload_as_bytes(
        self, subscriber: BodaiToolInvocationSubscriber
    ) -> None:
        body = _envelope_triplet()
        body["payload_json"] = json.dumps({"version": "1.0.0"}).encode("utf-8")
        result = subscriber._decode_envelope(body)
        assert result is not None
        assert result["payload"] == {"version": "1.0.0"}

    def test_direct_triplet_missing_topic_returns_none(
        self, subscriber: BodaiToolInvocationSubscriber
    ) -> None:
        body = {"payload_json": "{}"}
        assert subscriber._decode_envelope(body) is None

    def test_direct_triplet_missing_payload_json_returns_none(
        self, subscriber: BodaiToolInvocationSubscriber
    ) -> None:
        body = {"topic": "x"}
        assert subscriber._decode_envelope(body) is None

    def test_direct_triplet_invalid_json(
        self,
        subscriber: BodaiToolInvocationSubscriber,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        body = {"topic": "x", "payload_json": "{bad"}
        with caplog.at_level(logging.WARNING, logger="akosha.ingestion.bodai_event_subscriber"):
            assert subscriber._decode_envelope(body) is None
        assert any("failed to parse triplet" in rec.message for rec in caplog.records)

    def test_direct_triplet_non_dict_payload_defaulted_to_empty(
        self, subscriber: BodaiToolInvocationSubscriber
    ) -> None:
        body = {
            "topic": "x",
            "payload_json": json.dumps("a string"),
            "headers_json": "{}",
        }
        result = subscriber._decode_envelope(body)
        assert result is not None
        assert result["payload"] == {}

    def test_direct_triplet_non_dict_headers_defaulted_to_empty(
        self, subscriber: BodaiToolInvocationSubscriber
    ) -> None:
        """``headers`` defaults to ``{}`` when ``headers_json`` parses to a non-dict."""
        body = {
            "topic": "x",
            "payload_json": json.dumps({"k": "v"}),
            "headers_json": json.dumps([1, 2, 3]),  # valid JSON, but a list
        }
        result = subscriber._decode_envelope(body)
        assert result is not None
        assert result["headers"] == {}


# ---------------------------------------------------------------------------
# _build_content / _parse_timestamp
# ---------------------------------------------------------------------------


class TestBuildContent:
    """``_build_content`` formats a row for the embedding service."""

    def test_formats_all_fields(self) -> None:
        payload = {
            "tool": "search_all_systems",
            "surface": "claude_code",
            "result": "ok",
            "duration_ms": 42,
            "error": "boom",
        }
        content = BodaiToolInvocationSubscriber._build_content(payload)
        assert content == (
            "websocket tool invocation: "
            "tool=search_all_systems "
            "surface=claude_code "
            "result=ok "
            "duration_ms=42 "
            "error='boom'"
        )

    def test_uses_question_marks_for_missing_fields(self) -> None:
        content = BodaiToolInvocationSubscriber._build_content({})
        assert content == (
            "websocket tool invocation: tool=? surface=? result=? duration_ms=? error=''"
        )


class TestParseTimestamp:
    """``_parse_timestamp`` parses ISO strings and falls back to now()."""

    def test_parses_iso_string(self) -> None:
        payload = {"timestamp": "2026-09-06T04:00:00+00:00"}
        ts = BodaiToolInvocationSubscriber._parse_timestamp(payload)
        assert ts.year == 2026 and ts.month == 9 and ts.day == 6

    def test_falls_back_to_now_when_missing(self) -> None:
        ts = BodaiToolInvocationSubscriber._parse_timestamp({})
        assert ts.tzinfo is not None

    def test_falls_back_to_now_on_invalid_string(self) -> None:
        ts = BodaiToolInvocationSubscriber._parse_timestamp({"timestamp": "garbage"})
        assert ts.tzinfo is not None


# ---------------------------------------------------------------------------
# _index_envelope
# ---------------------------------------------------------------------------


class TestIndexEnvelope:
    """``_index_envelope`` embeds + inserts; returns False on version mismatch."""

    @pytest.mark.asyncio
    async def test_returns_false_on_schema_mismatch(self) -> None:
        s = _make_subscriber()
        assert await s._index_envelope({"version": "0.0.1"}, event_id="evt") is False

    @pytest.mark.asyncio
    async def test_returns_false_on_empty_content(self) -> None:
        """A row with no extractable tool/surface produces empty content."""
        s = _make_subscriber()
        payload = {"version": SUPPORTED_SCHEMA_VERSION}
        # All the fields the content builder reads default to '?'; not empty.
        # Force empty by patching _build_content.
        with patch.object(BodaiToolInvocationSubscriber, "_build_content", return_value=""):
            assert await s._index_envelope(payload, event_id="evt") is False

    @pytest.mark.asyncio
    async def test_returns_true_on_successful_insert(self) -> None:
        hot_store = MagicMock()
        hot_store.insert = AsyncMock()
        s = _make_subscriber(hot_store=hot_store)
        mock_embedding = MagicMock()
        mock_embedding.tolist = MagicMock(return_value=[0.1] * 384)
        mock_service = MagicMock()
        mock_service.generate_embedding = AsyncMock(return_value=mock_embedding)
        with patch(
            "akosha.ingestion.bodai_event_subscriber.get_embedding_service",
            return_value=mock_service,
        ):
            payload = {
                "version": SUPPORTED_SCHEMA_VERSION,
                "tool": "x",
                "surface": "y",
                "result": "ok",
                "duration_ms": 1,
                "error": "",
                "timestamp": "2026-09-06T04:00:00+00:00",
            }
            result = await s._index_envelope(payload, event_id="evt-99")
        assert result is True
        hot_store.insert.assert_awaited_once()
        record = hot_store.insert.await_args.args[0]
        assert record.system_id == SYSTEM_ID_MAHAVISHNU
        assert record.conversation_id == "evt-99"

    @pytest.mark.asyncio
    async def test_embedding_list_fallback_when_no_tolist(self) -> None:
        """If the embedding object lacks ``tolist``, fall back to ``list(...)``."""
        hot_store = MagicMock()
        hot_store.insert = AsyncMock()
        s = _make_subscriber(hot_store=hot_store)
        mock_service = MagicMock()
        mock_service.generate_embedding = AsyncMock(return_value=[0.2] * 384)
        with patch(
            "akosha.ingestion.bodai_event_subscriber.get_embedding_service",
            return_value=mock_service,
        ):
            payload = {
                "version": SUPPORTED_SCHEMA_VERSION,
                "tool": "x",
                "surface": "y",
                "result": "ok",
                "duration_ms": 1,
                "error": "",
            }
            assert await s._index_envelope(payload, event_id="evt") is True
        hot_store.insert.assert_awaited_once()


# ---------------------------------------------------------------------------
# _process_response / _process_entry
# ---------------------------------------------------------------------------


def _decode_envelope_payload(
    *,
    topic: str = TOOL_INVOCATION_TOPIC,
    event_id: str = "evt-1",
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a complete redis-stream entry in the direct-triplet shape."""
    if body is None:
        body = {
            "version": SUPPORTED_SCHEMA_VERSION,
            "tool": "x",
            "surface": "y",
            "result": "ok",
            "duration_ms": 1,
            "error": "",
        }
    return [
        "1700000000000-0",
        {
            "topic": topic,
            "payload_json": json.dumps(body),
            "headers_json": "{}",
            "event_id": event_id,
        },
    ]


class TestProcessResponse:
    """``_process_response`` iterates streams and entries safely."""

    @pytest.mark.asyncio
    async def test_skips_non_list_streams(self) -> None:
        s = _make_subscriber()
        client = _FakeRedisClient()
        # First element is a string (not a stream tuple); second is the good
        # one. We use a non-tool topic so the entry takes the topic-mismatch
        # short-circuit (which still XACKs) without invoking the embedding
        # service.
        entry = _decode_envelope_payload(topic="pattern.detected")
        await s._process_response(["bad", [STREAM_NAME, [entry]]], client=client)
        client.xack.assert_awaited()

    @pytest.mark.asyncio
    async def test_skips_short_streams(self) -> None:
        s = _make_subscriber()
        client = _FakeRedisClient()
        await s._process_response([[STREAM_NAME]], client=client)
        client.xack.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_non_list_entries(self) -> None:
        s = _make_subscriber()
        client = _FakeRedisClient()
        await s._process_response([[STREAM_NAME, "not-a-list-of-entries"]], client=client)
        client.xack.assert_not_awaited()


class TestProcessEntry:
    """``_process_entry`` is the workhorse — every branch gets a test."""

    @pytest.mark.asyncio
    async def test_returns_when_normalize_yields_none(self) -> None:
        s = _make_subscriber(hot_store=MagicMock())
        client = _FakeRedisClient()
        await s._process_entry("not-a-tuple", client=client)
        client.xack.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_xacks_and_returns_when_decode_fails(self) -> None:
        s = _make_subscriber(hot_store=MagicMock())
        client = _FakeRedisClient()
        # Build a normal-shape entry whose payload_json is invalid JSON.
        bad_entry = [
            "1700000000000-0",
            {"topic": "x", "payload_json": "{not-json", "headers_json": "{}"},
        ]
        await s._process_entry(bad_entry, client=client)
        # XACK even though decode failed (poison message; ack to avoid loop).
        client.xack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_xacks_and_skips_non_tool_invocation_topic(self) -> None:
        s = _make_subscriber(hot_store=MagicMock())
        client = _FakeRedisClient()
        entry = _decode_envelope_payload(topic="pattern.detected")
        await s._process_entry(entry, client=client)
        client.xack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_indexes_and_xacks_on_success(self) -> None:
        hot_store = MagicMock()
        hot_store.insert = AsyncMock()
        hot_store.search_similar = AsyncMock(return_value=[])
        hot_store.conn = MagicMock()
        s = _make_subscriber(hot_store=hot_store)
        client = _FakeRedisClient()
        mock_embedding = MagicMock()
        mock_embedding.tolist = MagicMock(return_value=[0.0] * 384)
        mock_service = MagicMock()
        mock_service.generate_embedding = AsyncMock(return_value=mock_embedding)
        with patch(
            "akosha.ingestion.bodai_event_subscriber.get_embedding_service",
            return_value=mock_service,
        ):
            await s._process_entry(_decode_envelope_payload(), client=client)
        client.xack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_xacks_without_watermark_when_indexed_false(self) -> None:
        """A schema-mismatch envelope is XACKed but never bumps the watermark."""
        hot_store = MagicMock()
        hot_store.insert = AsyncMock()
        hot_store.search_similar = AsyncMock(return_value=[])
        hot_store.conn = MagicMock()
        s = _make_subscriber(hot_store=hot_store)
        client = _FakeRedisClient()
        entry = _decode_envelope_payload(body={"version": "99.0.0"})
        await s._process_entry(entry, client=client)
        client.xack.assert_awaited_once()
        hot_store.insert.assert_not_awaited()  # no row indexed

    @pytest.mark.asyncio
    async def test_does_not_ack_on_timeout(self, caplog: pytest.LogCaptureFixture) -> None:
        s = _make_subscriber(hot_store=MagicMock())
        client = _FakeRedisClient()

        async def slow_index(*_a: Any, **_kw: Any) -> bool:
            await asyncio.sleep(60)
            return True

        with (
            patch.object(s, "_index_envelope", side_effect=slow_index),
            caplog.at_level(
                logging.WARNING,
                logger="akosha.ingestion.bodai_event_subscriber",
            ),
        ):
            await s._process_entry(_decode_envelope_payload(), client=client)
        client.xack.assert_not_awaited()
        assert any("indexing timed out" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_does_not_ack_on_generic_exception(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        s = _make_subscriber(hot_store=MagicMock())
        client = _FakeRedisClient()

        async def boom(*_a: Any, **_kw: Any) -> bool:
            raise RuntimeError("hot_store down")

        with (
            patch.object(s, "_index_envelope", side_effect=boom),
            caplog.at_level(
                logging.WARNING,
                logger="akosha.ingestion.bodai_event_subscriber",
            ),
        ):
            await s._process_entry(_decode_envelope_payload(), client=client)
        client.xack.assert_not_awaited()
        assert any("indexing failed" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# _safe_xack
# ---------------------------------------------------------------------------


class TestSafeXack:
    """``_safe_xack`` swallows transport errors."""

    @pytest.mark.asyncio
    async def test_calls_xack_on_success(self) -> None:
        s = _make_subscriber()
        client = _FakeRedisClient()
        await s._safe_xack(client, "1700000000000-0")
        client.xack.assert_awaited_once_with(STREAM_NAME, s._consumer_group, "1700000000000-0")

    @pytest.mark.asyncio
    async def test_swallows_exception(self, caplog: pytest.LogCaptureFixture) -> None:
        s = _make_subscriber()
        client = _FakeRedisClient()
        client.xack.side_effect = ConnectionError("transport down")
        with caplog.at_level(logging.WARNING, logger="akosha.ingestion.bodai_event_subscriber"):
            await s._safe_xack(client, "1700000000000-0")
        assert any("xack failed" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# _update_watermark
# ---------------------------------------------------------------------------


class TestUpdateWatermark:
    """``_update_watermark`` upserts the reserved watermark row."""

    @pytest.mark.asyncio
    async def test_writes_watermark_row_when_conn_present(self) -> None:
        hot_store = MagicMock()
        hot_store.insert = AsyncMock()
        conn = MagicMock()
        hot_store.conn = conn
        s = _make_subscriber(hot_store=hot_store)
        await s._update_watermark("1700000000000-9")
        conn.execute.assert_called_once()
        # The reserved watermark row was inserted.
        hot_store.insert.assert_awaited_once()
        record = hot_store.insert.await_args.args[0]
        assert record.conversation_id == WATERMARK_CONVERSATION_ID
        assert record.content == WATERMARK_CONTENT
        assert record.metadata == {"last_message_id": "1700000000000-9"}

    @pytest.mark.asyncio
    async def test_logs_warning_when_conn_missing(self, caplog: pytest.LogCaptureFixture) -> None:
        hot_store = MagicMock(spec=[])  # no `.conn` attribute
        s = _make_subscriber(hot_store=hot_store)
        with caplog.at_level(logging.WARNING, logger="akosha.ingestion.bodai_event_subscriber"):
            await s._update_watermark("1700000000000-9")
        assert any("watermark update failed" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# _auto_consumer_name / _sleep_with_cancel / _wait_until_stopped
# ---------------------------------------------------------------------------


class TestAutoConsumerName:
    """``_auto_consumer_name`` returns a unique, prefixed name."""

    def test_returns_string_with_akosha_prefix(self) -> None:
        name = BodaiToolInvocationSubscriber._auto_consumer_name()
        assert name.startswith("akosha-")
        # The suffix is 12 hex chars (uuid4().hex[:12]).
        assert len(name) == len("akosha-") + 12

    def test_unique_per_call(self) -> None:
        assert (
            BodaiToolInvocationSubscriber._auto_consumer_name()
            != BodaiToolInvocationSubscriber._auto_consumer_name()
        )


class TestSleepWithCancel:
    """``_sleep_with_cancel`` respects ``_running`` for prompt cancellation."""

    @pytest.mark.asyncio
    async def test_returns_after_timeout_when_running(self) -> None:
        s = _make_subscriber()
        s._running = True
        start = asyncio.get_event_loop().time()
        await s._sleep_with_cancel(0.05)
        elapsed = asyncio.get_event_loop().time() - start
        # Should have waited at least ~50 ms but no more than ~150 ms.
        assert 0.04 < elapsed < 0.3

    @pytest.mark.asyncio
    async def test_returns_immediately_when_already_stopped(self) -> None:
        s = _make_subscriber()
        s._running = False
        start = asyncio.get_event_loop().time()
        await s._sleep_with_cancel(5.0)
        elapsed = asyncio.get_event_loop().time() - start
        # Should return near-instantly.
        assert elapsed < 0.2


# ---------------------------------------------------------------------------
# _run_loop
# ---------------------------------------------------------------------------


class TestRunLoop:
    """``_run_loop`` handles errors with exponential backoff and yields on empty."""

    @pytest.mark.asyncio
    async def test_yields_on_empty_response(self) -> None:
        s = _make_subscriber()
        client = _FakeRedisClient()
        # First call returns []; second raises CancelledError so the loop exits.
        client.xreadgroup.side_effect = [[], asyncio.CancelledError()]
        s._redis_client = client
        s._running = True
        with pytest.raises(asyncio.CancelledError):
            await s._run_loop()

    @pytest.mark.asyncio
    async def test_processes_response_when_non_empty(self) -> None:
        s = _make_subscriber(hot_store=MagicMock(insert=AsyncMock(), conn=MagicMock()))
        client = _FakeRedisClient()
        entry = _decode_envelope_payload()
        client.xreadgroup.side_effect = [
            [[STREAM_NAME, [entry]]],  # first call returns 1 message
            asyncio.CancelledError(),  # second call cancels the loop
        ]
        s._redis_client = client
        s._running = True
        with pytest.raises(asyncio.CancelledError):
            await s._run_loop()
        # The XACK happened for the indexed message.
        client.xack.assert_awaited()

    @pytest.mark.asyncio
    async def test_handles_xreadgroup_exception_with_backoff(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An xreadgroup exception logs WARNING, sleeps, then retries."""
        s = _make_subscriber()
        client = _FakeRedisClient()
        call_count = 0

        async def flaky(*_a: Any, **_kw: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("broker hiccup")
            raise asyncio.CancelledError()

        client.xreadgroup.side_effect = flaky
        s._redis_client = client
        s._running = True
        # Patch the sleep so the test doesn't actually wait DEFAULT_RECONNECT_BACKOFF_SECONDS.
        with (
            patch.object(s, "_sleep_with_cancel", AsyncMock()) as mock_sleep,
            caplog.at_level(
                logging.WARNING,
                logger="akosha.ingestion.bodai_event_subscriber",
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await s._run_loop()
        mock_sleep.assert_awaited_once()
        # The first call's backoff equals the default; the next would be 2x.
        assert any("xreadgroup error" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_backoff_doubles_each_iteration(self) -> None:
        """Three consecutive failures double the backoff each time (capped)."""
        s = _make_subscriber()
        client = _FakeRedisClient()
        call_count = 0

        async def always_fail(*_a: Any, **_kw: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count >= 4:
                raise asyncio.CancelledError()
            raise ConnectionError(f"fail {call_count}")

        client.xreadgroup.side_effect = always_fail
        s._redis_client = client
        s._running = True
        sleeps: list[float] = []

        async def track_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        with (
            patch.object(s, "_sleep_with_cancel", side_effect=track_sleep),
            pytest.raises(asyncio.CancelledError),
        ):
            await s._run_loop()
        # 3 sleeps recorded; each should double, capped at MAX.
        assert len(sleeps) == 3
        assert sleeps[0] == DEFAULT_RECONNECT_BACKOFF_SECONDS
        assert sleeps[1] == DEFAULT_RECONNECT_BACKOFF_SECONDS * 2
        assert sleeps[2] == DEFAULT_RECONNECT_BACKOFF_SECONDS * 4
        # None exceed the cap.
        assert all(sleep <= DEFAULT_RECONNECT_BACKOFF_MAX_SECONDS for sleep in sleeps)

    @pytest.mark.asyncio
    async def test_backoff_resets_after_successful_read(self) -> None:
        """After a successful (empty) read, the backoff resets to the default."""
        s = _make_subscriber()
        client = _FakeRedisClient()
        call_count = 0

        async def sequence(*_a: Any, **_kw: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("fail once")
            if call_count == 2:
                return []  # successful empty read resets backoff
            if call_count == 3:
                raise ConnectionError("fail again")
            raise asyncio.CancelledError()

        client.xreadgroup.side_effect = sequence
        s._redis_client = client
        s._running = True
        sleeps: list[float] = []

        async def track_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        with (
            patch.object(s, "_sleep_with_cancel", side_effect=track_sleep),
            pytest.raises(asyncio.CancelledError),
        ):
            await s._run_loop()
        # After a successful empty read (call #2), backoff resets, so the next
        # sleep after a fail is back to the default.
        assert sleeps[0] == DEFAULT_RECONNECT_BACKOFF_SECONDS
        assert sleeps[1] == DEFAULT_RECONNECT_BACKOFF_SECONDS


# ---------------------------------------------------------------------------
# _read_watermark sync shim
# ---------------------------------------------------------------------------


class TestReadWatermarkSyncShim:
    """The sync shim always returns ``None`` (callers must use the async one)."""

    def test_returns_none(self) -> None:
        s = _make_subscriber()
        assert s._read_watermark() is None
