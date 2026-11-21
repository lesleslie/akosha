"""Tests for :mod:`akosha.ingestion.bodai_event_subscriber`.

Coverage was at ~68% before these tests. The subscriber is a Redis
Streams consumer with exponential backoff, a group-positioned resume
mechanism, and a per-event timeout pipeline. We mock the redis client,
the embedding service, and the HotStore so each branch is reachable in
isolation.

Branch matrix pinned by these tests:

* ``_create_redis_client`` — ImportError path returns ``None``
* ``start`` — early-returns when ``_running``, ``hot_store=None``, redis
  import failure, ``xgroup_create`` non-BUSYGROUP failure, watermark
  read failure (the group then starts at ``"0"``)
* ``stop`` — idempotent (running=False, no task, no client)
* ``_ensure_consumer_group`` — first-time create, start at the
  watermark, BUSYGROUP tolerance
* ``_read_watermark_id`` — hot_store=None, no conn, query exception,
  absent row, JSON/dict/bytes metadata, non-JSON metadata, missing
  last_message_id, malformed watermark
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
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from akosha.ingestion.bodai_event_subscriber import (
    DEFAULT_RECONNECT_BACKOFF_MAX_SECONDS,
    DEFAULT_RECONNECT_BACKOFF_SECONDS,
    MAX_INDEX_RETRIES,
    STREAM_NAME,
    SUPPORTED_SCHEMA_VERSION,
    SYSTEM_ID_MAHAVISHNU,
    TOOL_INVOCATION_TOPIC,
    WATERMARK_CONTENT,
    WATERMARK_CONVERSATION_ID,
    BodaiToolInvocationSubscriber,
    _create_redis_client,
    _is_duplicate_key_error,
    _is_newer,
    _stream_id_key,
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
        self.xgroup_delconsumer = AsyncMock(return_value=0)
        self.xpending_range = AsyncMock(return_value=[])
        self.xreadgroup = AsyncMock(return_value=[])
        self.xautoclaim = AsyncMock(return_value=["0-0", [], []])
        self.xack = AsyncMock()
        self.aclose = AsyncMock()
        self.close = AsyncMock()


_HOT_STORE_SENTINEL = object()

#: The captured live wire shape, kept as a data file so the
#: producer/consumer contract is pinned by DATA rather than by a
#: hand-edited dict that can drift. The path is derived from this test
#: file so the fixture is always the one in this tree.
LIVE_ENTRY_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "bodai_events_stream_entry.json"
)


@pytest.fixture
def live_entry() -> dict[str, Any]:
    """The live stream entry, in the byte layout the stream actually uses.

    The stream stores the payload as a JSON STRING under the ``payload``
    field, so the fixture's dict is re-serialised on the way in.
    """
    fixture = json.loads(LIVE_ENTRY_FIXTURE.read_text())
    return {
        "channel": fixture["channel"],
        "headers": fixture["headers"],
        "payload": json.dumps(fixture["payload"]),
    }


def _make_subscriber(
    *,
    hot_store: Any | None = _HOT_STORE_SENTINEL,
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
    """Build a stream payload in the LIVE wire shape.

    ``oneiric``'s Redis Streams adapter writes ``channel`` (the bus
    topic), ``payload`` (the JSON body) and ``headers`` (the JSON
    headers). The name is historical: the direct-triplet shape this
    helper used to build is gone -- nothing in the fleet writes it.
    """
    body = (
        payload
        if payload is not None
        else {
            "version": SUPPORTED_SCHEMA_VERSION,
            "tool": "akosha_search_all_systems",
            "surface": "claude_code",
            "result": "ok",
            "duration_ms": 12,
            "error": "",
        }
    )
    header_map = dict(headers or {})
    if event_id is not None:
        header_map.setdefault("event_id", event_id)
    return {
        "channel": topic,
        "payload": json.dumps(body),
        "headers": json.dumps(header_map),
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
            result = _create_redis_client("redis://localhost:6379/0", socket_timeout=30.0)
        assert result is None

    def test_returns_client_when_redis_asyncio_is_available(self) -> None:
        """When redis.asyncio is importable, the helper returns a client."""
        fake_module = MagicMock()
        fake_aioredis = MagicMock()
        fake_aioredis.from_url = MagicMock(return_value="FAKE_CLIENT")
        fake_module.asyncio = fake_aioredis
        with patch.dict("sys.modules", {"redis": fake_module, "redis.asyncio": fake_aioredis}):
            result = _create_redis_client("redis://localhost:6379/0", socket_timeout=30.0)
        assert result == "FAKE_CLIENT"
        fake_aioredis.from_url.assert_called_once()

    def test_the_client_gets_a_socket_timeout(self) -> None:
        """The client must carry an explicit socket timeout.

        redis-py 8's default is 5 seconds, and that default is a bound
        on the SERVER's work -- not on how long the client is willing
        to wait for an idle BLOCK. The reply to this subscriber's read
        is delayed by whatever the event loop is doing, and the event
        loop is doing per-entry embedding and DuckDB work.

        The live proof, 2026-11-21: with the default in place, a
        backlog replay logged 31 ``xreadgroup error: Timeout reading
        from localhost:6379`` failures. Every one of them aborted
        client-side on a server that was answering normally.
        """
        fake_module = MagicMock()
        fake_aioredis = MagicMock()
        fake_aioredis.from_url = MagicMock(return_value="FAKE_CLIENT")
        fake_module.asyncio = fake_aioredis
        with patch.dict("sys.modules", {"redis": fake_module, "redis.asyncio": fake_aioredis}):
            _create_redis_client("redis://localhost:6379/0", socket_timeout=30.0)

        kwargs = fake_aioredis.from_url.call_args.kwargs
        assert kwargs.get("socket_timeout") is not None
        assert kwargs["socket_timeout"] > 0

    def test_the_socket_timeout_exceeds_the_block_duration(self) -> None:
        """The timeout must clear the BLOCK duration by a wide margin.

        A BLOCK that outlives its own socket timeout is a timeout on a
        healthy server: the client gives up on a reply that was going
        to arrive.
        """
        fake_module = MagicMock()
        fake_aioredis = MagicMock()
        fake_aioredis.from_url = MagicMock(return_value="FAKE_CLIENT")
        fake_module.asyncio = fake_aioredis
        with patch.dict("sys.modules", {"redis": fake_module, "redis.asyncio": fake_aioredis}):
            _create_redis_client("redis://localhost:6379/0", socket_timeout=30.0)

        kwargs = fake_aioredis.from_url.call_args.kwargs
        assert kwargs["socket_timeout"] > 1.5

    def test_the_client_gets_a_connect_timeout(self) -> None:
        """A connect that never completes must fail fast, not hang.

        A read on this group may legitimately take minutes; a connect
        never does. The two bounds are different problems and get
        different values.
        """
        fake_module = MagicMock()
        fake_aioredis = MagicMock()
        fake_aioredis.from_url = MagicMock(return_value="FAKE_CLIENT")
        fake_module.asyncio = fake_aioredis
        with patch.dict("sys.modules", {"redis": fake_module, "redis.asyncio": fake_aioredis}):
            _create_redis_client("redis://localhost:6379/0", socket_timeout=30.0)

        kwargs = fake_aioredis.from_url.call_args.kwargs
        assert 0 < kwargs["socket_connect_timeout"] <= 30


class TestTheSocketTimeoutIsDerivedFromTheConfiguredWork:
    """The timeout must scale with the per-event processing bound.

    The event loop's longest occupancy is the per-entry processing
    timeout: a batch of entries is processed concurrently, so the batch
    is bounded by ONE entry's timeout, not by the batch size times it.
    The socket timeout has to clear that, because a reply that arrives
    while the loop is busy is still a valid reply -- the client just
    has to be willing to wait for it.
    """

    def test_the_timeout_clears_the_per_event_timeout(self) -> None:
        subscriber = _make_subscriber(per_event_timeout_seconds=30.0)

        assert subscriber.socket_timeout_seconds > 30.0

    def test_a_longer_per_event_timeout_raises_the_socket_timeout(self) -> None:
        fast = _make_subscriber(per_event_timeout_seconds=5.0)
        slow = _make_subscriber(per_event_timeout_seconds=120.0)

        assert slow.socket_timeout_seconds > fast.socket_timeout_seconds

    def test_the_timeout_clears_the_block_duration(self) -> None:
        subscriber = _make_subscriber(xreadgroup_block_ms=1500)

        assert subscriber.socket_timeout_seconds > 1.5

    def test_the_client_is_built_with_the_derived_timeout(self) -> None:
        subscriber = _make_subscriber(per_event_timeout_seconds=30.0)
        fake_module = MagicMock()
        fake_aioredis = MagicMock()
        fake_aioredis.from_url = MagicMock(return_value="FAKE_CLIENT")
        fake_module.asyncio = fake_aioredis

        with patch.dict("sys.modules", {"redis": fake_module, "redis.asyncio": fake_aioredis}):
            _create_redis_client(
                "redis://localhost:6379/0",
                socket_timeout=subscriber.socket_timeout_seconds,
            )

        kwargs = fake_aioredis.from_url.call_args.kwargs
        assert kwargs["socket_timeout"] == subscriber.socket_timeout_seconds


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
    async def test_a_watermark_read_failure_still_starts_the_loop(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A watermark read that blows up must not stop the subscriber.

        The read only decides where a NEWLY CREATED group starts; the
        group already exists in every restart, so a failure costs a
        replayed back-catalogue, not a dead pipeline.
        """
        client = _FakeRedisClient()
        client.xreadgroup.side_effect = [
            # First call returns empty so the loop exits immediately.
            [],
            asyncio.CancelledError(),
        ]
        hot_store = MagicMock()
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("duckdb down")
        hot_store.conn = conn
        hot_store._lock = asyncio.Lock()
        s = _make_subscriber(hot_store=hot_store)
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
            await asyncio.sleep(0)
            started = s._running
            s._running = False
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await s._task
        # The subscriber started and is reading for new messages.
        assert started is True
        assert any("read=>" in rec.message for rec in caplog.records)
        assert any("watermark read failed" in rec.message for rec in caplog.records)
        # The group was created at the start of the stream, not skipped.
        client.xgroup_create.assert_awaited_once_with(
            name=STREAM_NAME,
            groupname="akosha-tool-invocation-indexers",
            id="0",
            mkstream=True,
        )


class TestStartHappyPath:
    """``start`` connects, ensures the group, and spawns the read loop."""

    @pytest.mark.asyncio
    async def test_starts_loop_and_logs_the_read_mode(self, caplog: pytest.LogCaptureFixture) -> None:
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
        # The read asks for NEW messages -- the only form that delivers
        # a message nobody has seen.
        assert client.xreadgroup.await_args.kwargs["streams"] == {STREAM_NAME: ">"}


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
    async def test_stop_deletes_the_consumer_name(self) -> None:
        s = _make_subscriber()
        client = _FakeRedisClient()
        client.xpending_range = AsyncMock(return_value=[])
        s._redis_client = client
        s._consumer_name = "akosha-fixed123"
        await s.stop()
        client.xgroup_delconsumer.assert_awaited_once_with(
            STREAM_NAME, s._consumer_group, "akosha-fixed123"
        )

    @pytest.mark.asyncio
    async def test_stop_keeps_a_consumer_that_has_pending_messages(self) -> None:
        """XGROUP DELCONSUMER destroys pending messages. Never call it on a
        consumer that still owns one: the message would be neither acked
        nor redelivered, and XAUTOCLAIM could never recover it.
        """
        s = _make_subscriber()
        client = _FakeRedisClient()
        client.xpending_range = AsyncMock(
            return_value=[{"message_id": "1-0", "consumer": "akosha-fixed123"}]
        )
        s._redis_client = client
        s._consumer_name = "akosha-fixed123"
        await s.stop()
        client.xgroup_delconsumer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_preserves_a_caller_supplied_consumer_name(self) -> None:
        """A stable, operator-supplied name must survive stop/start."""
        s = _make_subscriber(consumer_name="my-stable-consumer")
        client = _FakeRedisClient()
        client.xpending_range = AsyncMock(return_value=[])
        s._redis_client = client
        s._consumer_name = "my-stable-consumer"
        await s.stop()
        assert s._consumer_name == "my-stable-consumer"

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
    async def test_the_group_starts_at_the_watermark(self) -> None:
        """A group created over an indexed stream skips the back-catalogue."""
        s = _make_subscriber()
        client = _FakeRedisClient()
        await s._ensure_consumer_group(client, start_id="1700000000000-7")
        client.xgroup_create.assert_awaited_once_with(
            name=STREAM_NAME,
            groupname="akosha-tool-invocation-indexers",
            id="1700000000000-7",
            mkstream=True,
        )

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


class TestReadWatermarkId:
    """``_read_watermark_id`` reads the watermark row by primary key.

    The row is NOT found by vector search: it carries a zero-vector
    embedding, and a zero-vector query against the HNSW index is
    degenerate -- the row is not reliably in the result set. A primary
    key lookup is exact and cheap.

    The read holds the STORE's lock for the whole execute-then-fetch
    pair. Reading the connection without it is not safe: the substrate
    holds its threading lock only for the statement, and a write
    landing in the gap replaces the result.
    """

    @staticmethod
    def _store(row: Any) -> MagicMock:
        """A HotStore whose primary-key lookup returns ``row``."""
        hot_store = MagicMock()
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = row
        hot_store.conn = conn
        # A real async lock, so the read exercises the locked path.
        # (``MagicMock()._lock`` is a threading RLock -- a mock
        # artefact -- and the substrate's HotStore holds an
        # ``asyncio.Lock``.)
        hot_store._lock = asyncio.Lock()
        return hot_store

    async def test_returns_none_when_hot_store_is_none(self) -> None:
        s = _make_subscriber(hot_store=None)
        assert await s._read_watermark_id() is None

    async def test_returns_none_when_conn_is_missing(self) -> None:
        hot_store = MagicMock(spec=[])  # no `.conn` attribute
        s = _make_subscriber(hot_store=hot_store)
        assert await s._read_watermark_id() is None

    async def test_returns_none_on_query_exception(self, caplog: pytest.LogCaptureFixture) -> None:
        hot_store = MagicMock()
        conn = MagicMock()
        conn.execute.side_effect = RuntimeError("duckdb fail")
        hot_store.conn = conn
        hot_store._lock = asyncio.Lock()
        s = _make_subscriber(hot_store=hot_store)
        with caplog.at_level(logging.WARNING, logger="akosha.ingestion.bodai_event_subscriber"):
            result = await s._read_watermark_id()
        assert result is None
        assert any("watermark read failed" in rec.message for rec in caplog.records)

    async def test_returns_none_when_the_row_is_absent(self) -> None:
        s = _make_subscriber(hot_store=self._store(None))
        assert await s._read_watermark_id() is None

    async def test_reads_the_row_by_primary_key(self) -> None:
        hot_store = self._store((json.dumps({"last_message_id": "1700000000000-2"}),))
        s = _make_subscriber(hot_store=hot_store)
        assert await s._read_watermark_id() == "1700000000000-2"
        hot_store.conn.execute.assert_called_once_with(
            "SELECT metadata FROM conversations WHERE conversation_id = ?",
            [WATERMARK_CONVERSATION_ID],
        )

    async def test_accepts_dict_metadata(self) -> None:
        s = _make_subscriber(
            hot_store=self._store(({"last_message_id": "1700000000000-0"},))
        )
        assert await s._read_watermark_id() == "1700000000000-0"

    async def test_accepts_bytes_metadata(self) -> None:
        s = _make_subscriber(
            hot_store=self._store((b'{"last_message_id": "1700000000000-1"}',))
        )
        assert await s._read_watermark_id() == "1700000000000-1"

    async def test_returns_none_when_the_metadata_is_not_json(self) -> None:
        s = _make_subscriber(hot_store=self._store(("not-json-at-all",)))
        assert await s._read_watermark_id() is None

    async def test_returns_none_when_there_is_no_last_message_id(self) -> None:
        s = _make_subscriber(
            hot_store=self._store((json.dumps({"other_field": "value"}),))
        )
        assert await s._read_watermark_id() is None

    async def test_ignores_a_malformed_watermark(self, caplog: pytest.LogCaptureFixture) -> None:
        """A watermark that is not a stream id is ignored, not used."""
        s = _make_subscriber(
            hot_store=self._store(
                (json.dumps({"last_message_id": "malformed-no-dash"}),)
            )
        )
        with caplog.at_level(
            logging.WARNING,
            logger="akosha.ingestion.bodai_event_subscriber",
        ):
            assert await s._read_watermark_id() is None
        assert any("malformed watermark" in rec.message for rec in caplog.records)


class TestStreamIdComparison:
    """``_stream_id_key`` and ``_is_newer`` decide whether the watermark advances."""

    def test_a_well_formed_id_sorts_by_milliseconds_then_sequence(self) -> None:
        assert _stream_id_key("1700000000000-7") == (1700000000000, 7)

    def test_a_malformed_id_has_no_key(self) -> None:
        assert _stream_id_key("no-dash") is None
        assert _stream_id_key("1700000000000-x") is None

    def test_a_newer_millisecond_wins(self) -> None:
        assert _is_newer("1700000000001-0", "1700000000000-9") is True

    def test_the_same_millisecond_breaks_the_tie_on_the_sequence(self) -> None:
        assert _is_newer("1700000000000-8", "1700000000000-7") is True
        assert _is_newer("1700000000000-7", "1700000000000-8") is False

    def test_an_equal_id_is_not_an_advance(self) -> None:
        assert _is_newer("1700000000000-7", "1700000000000-7") is False

    def test_an_unparseable_candidate_is_rejected(self) -> None:
        """Accepting it would overwrite a good watermark with garbage."""
        assert _is_newer("not-a-stream-id", "1700000000000-7") is False

    def test_an_unparseable_current_is_ignored(self) -> None:
        """A valid id beats garbage."""
        assert _is_newer("1700000000000-7", "not-a-stream-id") is True


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
        assert subscriber._decode_envelope("not-a-dict") is None
        assert subscriber._decode_envelope(None) is None

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

    def test_live_channel_payload_shape_decodes(
        self, subscriber: BodaiToolInvocationSubscriber
    ) -> None:
        payload = {
            "channel": "bodai.hooks.post-tool-use",
            "headers": "{}",
            "payload": json.dumps(
                {
                    "agent_id": "agent-1",
                    "event": "PostToolUse",
                    "tool_name": "Read",
                    "tool_input": {"file_path": "/tmp/example"},
                }
            ),
        }
        result = subscriber._decode_envelope(payload)
        assert result is not None
        assert result["topic"] == "bodai.hooks.post-tool-use"
        assert result["payload"]["tool_name"] == "Read"
        assert result["headers"] == {}

    def test_the_captured_live_fixture_decodes(
        self, subscriber: BodaiToolInvocationSubscriber, live_entry: dict[str, Any]
    ) -> None:
        """The captured wire shape decodes, end to end, from the fixture.

        The fixture is the producer/consumer contract. If the producer's
        field names change, this test fails against the OLD expectation
        rather than the decoder quietly accepting a new shape nobody
        noticed.
        """
        result = subscriber._decode_envelope(live_entry)
        assert result is not None
        assert result["topic"] == "bodai.hooks.post-tool-use"
        assert result["payload"]["tool_name"] == "Read"
        assert result["payload"]["event"] == "PostToolUse"
        assert result["payload"]["harness"] == "claude-code"
        assert result["headers"] == {}

    def test_the_live_fixture_names_the_fields_the_stream_carries(
        self, live_entry: dict[str, Any]
    ) -> None:
        """The fixture lists exactly the fields the entry has."""
        fixture = json.loads(LIVE_ENTRY_FIXTURE.read_text())
        assert set(fixture["stream_entry_fields"]) == set(live_entry)

    def test_flat_enqueue_shape_decodes(
        self, subscriber: BodaiToolInvocationSubscriber
    ) -> None:
        """session-buddy's publisher writes the body fields directly onto
        the entry, with no nested ``payload`` field.
        """
        payload = {
            "channel": "session_buddy.publisher.heartbeat",
            "task_id": "t-1",
            "outcome": "ok",
        }
        result = subscriber._decode_envelope(payload)
        assert result is not None
        assert result["topic"] == "session_buddy.publisher.heartbeat"
        assert result["payload"]["task_id"] == "t-1"
        assert result["payload"]["outcome"] == "ok"
        assert "channel" not in result["payload"]

    def test_non_string_headers_do_not_drop_the_envelope(
        self, subscriber: BodaiToolInvocationSubscriber
    ) -> None:
        payload = {
            "channel": "x",
            "headers": {"already": "a dict"},
            "payload": json.dumps({"k": "v"}),
        }
        result = subscriber._decode_envelope(payload)
        assert result is not None
        assert result["payload"] == {"k": "v"}


# ---------------------------------------------------------------------------
# _build_content / _parse_timestamp
# ---------------------------------------------------------------------------


class TestBuildContent:
    """``_build_content`` formats a row for the embedding service."""

    def test_formats_all_fields(self) -> None:
        payload = {
            "tool": "akosha_search_all_systems",
            "surface": "claude_code",
            "result": "ok",
            "duration_ms": 42,
            "error": "boom",
        }
        content = BodaiToolInvocationSubscriber._build_content(payload)
        assert content == (
            "websocket tool invocation: "
            "tool=akosha_search_all_systems "
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

    def test_hook_event_uses_its_own_template(self) -> None:
        """A Claude Code hook envelope is not a websocket tool invocation.

        ``event`` is the event NAME (``PostToolUse``); the surface the tool
        ran on is ``harness`` (``claude-code``). The websocket prefix and
        the ``surface=event`` mapping mislabel every hook row.
        """
        content = BodaiToolInvocationSubscriber._build_content(
            {
                "event": "PostToolUse",
                "harness": "claude-code",
                "tool_name": "Read",
                "session_id": "s-1",
            },
            topic="bodai.hooks.post-tool-use",
        )
        assert content.startswith("hook tool invocation:")
        assert "tool=Read" in content
        assert "surface=claude-code" in content
        assert "surface=PostToolUse" not in content


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
    """Build a complete redis-stream entry in the LIVE wire shape.

    ``oneiric``'s Redis Streams adapter writes ``channel`` (the bus
    topic), ``payload`` (the JSON body) and ``headers`` (the JSON
    headers). Nothing in the fleet writes the older
    ``{topic, payload_json, headers_json}`` triplet, so the tests do
    not use it.
    """
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
            "channel": topic,
            "payload": json.dumps(body),
            "headers": json.dumps({"event_id": event_id}),
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
    async def test_indexes_the_live_hook_channel(self) -> None:
        """The channel the hook bridge actually publishes must be indexed."""
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
        entry = [
            "1700000000000-0",
            {
                "channel": "bodai.hooks.post-tool-use",
                "headers": "{}",
                "payload": json.dumps({"event": "PostToolUse", "tool_name": "Read"}),
            },
        ]
        with patch(
            "akosha.ingestion.bodai_event_subscriber.get_embedding_service",
            return_value=mock_service,
        ):
            await s._process_entry(entry, client=client)
        # The indexed row and the watermark row are both inserts; the
        # indexed row is the one keyed by the message id.
        indexed = [
            call.args[0]
            for call in hot_store.insert.await_args_list
            if getattr(call.args[0], "conversation_id", None) == "1700000000000-0"
        ]
        assert len(indexed) == 1
        assert "Read" in indexed[0].content
        client.xack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_logs_a_warning_when_decode_fails(self, caplog: pytest.LogCaptureFixture) -> None:
        """A silent drop is how a retired producer survived unnoticed."""
        s = _make_subscriber(hot_store=MagicMock())
        client = _FakeRedisClient()
        bad_entry = ["1700000000000-0", {"unrecognized": "shape"}]
        with caplog.at_level(
            logging.WARNING,
            logger="akosha.ingestion.bodai_event_subscriber",
        ):
            await s._process_entry(bad_entry, client=client)
        assert any("decode" in rec.message.lower() for rec in caplog.records)

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


class TestAReplayedRowIsNotAFailure:
    """Re-indexing a row that is already stored must be a no-op.

    The substrate's ``INSERT`` is not idempotent: it has no
    ``ON CONFLICT`` clause, so re-inserting a ``conversation_id`` that
    is already present raises a duplicate-key error. The subscriber
    treated that as an indexing FAILURE, which parks the message in
    the PEL and retries it three times before dropping it.

    The live proof, 2026-11-21: the backlog replay logged 170
    duplicate-key failures for rows that were already in DuckDB. The
    rows were fine. The pending list was not: the group's pending
    count sat at 108 with entries that could never succeed.

    A replay is the message's work being DONE, not a failure: the row
    is present, so the message must be ACKed and the watermark advanced.
    But no NEW row landed, so the "rows that actually landed" counter
    must not claim one.
    """

    @staticmethod
    def _duplicate_error() -> Exception:
        return RuntimeError(
            'Constraint Error: Duplicate key "conversation_id: 1791599555476-0" '
            "violates primary key constraint."
        )

    @pytest.mark.asyncio
    async def test_a_duplicate_row_is_acked(self) -> None:
        s = _make_subscriber(hot_store=MagicMock())
        client = _FakeRedisClient()

        async def boom(*_a: Any, **_kw: Any) -> bool:
            raise self._duplicate_error()

        with patch.object(s, "_index_envelope", side_effect=boom):
            await s._process_entry(_decode_envelope_payload(), client=client)
        client.xack.assert_awaited_once_with(
            STREAM_NAME, s._consumer_group, "1700000000000-0"
        )

    @pytest.mark.asyncio
    async def test_a_duplicate_row_is_not_left_pending(self) -> None:
        """The message must not be parked for a retry that cannot win."""
        s = _make_subscriber(hot_store=MagicMock())
        client = _FakeRedisClient()

        async def boom(*_a: Any, **_kw: Any) -> bool:
            raise self._duplicate_error()

        with patch.object(s, "_index_envelope", side_effect=boom):
            await s._process_entry(_decode_envelope_payload(), client=client)
        assert not s._index_retry_counts

    @pytest.mark.asyncio
    async def test_a_duplicate_row_does_not_count_as_a_landed_row(self) -> None:
        """The row was already there, so no new row landed."""
        s = _make_subscriber(hot_store=MagicMock())
        client = _FakeRedisClient()

        async def boom(*_a: Any, **_kw: Any) -> bool:
            raise self._duplicate_error()

        with patch.object(s, "_index_envelope", side_effect=boom):
            await s._process_entry(_decode_envelope_payload(), client=client)
        assert s.entries_indexed == 0

    @pytest.mark.asyncio
    async def test_a_duplicate_row_is_logged_as_a_replay(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        s = _make_subscriber(hot_store=MagicMock())
        client = _FakeRedisClient()

        async def boom(*_a: Any, **_kw: Any) -> bool:
            raise self._duplicate_error()

        with (
            patch.object(s, "_index_envelope", side_effect=boom),
            caplog.at_level(
                logging.INFO,
                logger="akosha.ingestion.bodai_event_subscriber",
            ),
        ):
            await s._process_entry(_decode_envelope_payload(), client=client)
        assert any("already indexed" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_a_genuine_failure_is_still_a_failure(self) -> None:
        """Only a duplicate is a replay. Everything else keeps failing."""
        s = _make_subscriber(hot_store=MagicMock())
        client = _FakeRedisClient()

        async def boom(*_a: Any, **_kw: Any) -> bool:
            raise RuntimeError("hot_store down")

        with patch.object(s, "_index_envelope", side_effect=boom):
            await s._process_entry(_decode_envelope_payload(), client=client)
        client.xack.assert_not_awaited()


class TestTheDuplicateKeyDetector:
    """The detector must recognise the backends, not everything."""

    def test_it_recognises_the_duckdb_message(self) -> None:
        exc = RuntimeError(
            'Constraint Error: Duplicate key "conversation_id: 1-0" violates '
            "primary key constraint."
        )
        assert _is_duplicate_key_error(exc) is True

    def test_it_recognises_the_postgres_message(self) -> None:
        exc = RuntimeError(
            'duplicate key value violates unique constraint "conversations_pkey"'
        )
        assert _is_duplicate_key_error(exc) is True

    def test_it_rejects_an_unrelated_error(self) -> None:
        assert _is_duplicate_key_error(RuntimeError("hot_store down")) is False

    def test_it_rejects_a_not_null_violation(self) -> None:
        """A NOT NULL violation is a real failure, not a replay."""
        exc = RuntimeError(
            "Constraint Error: NOT NULL constraint failed: conversations.content"
        )
        assert _is_duplicate_key_error(exc) is False


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

    @staticmethod
    def _store(current: str | None, *, rows_updated: int = 0) -> MagicMock:
        """A HotStore holding ``current`` as the watermark."""
        hot_store = MagicMock()
        hot_store.insert = AsyncMock()
        conn = MagicMock()
        metadata = (
            (json.dumps({"last_message_id": current}),) if current else None
        )
        # The SELECT returns the current row; the UPDATE returns a count.
        conn.execute.return_value.fetchone.side_effect = [
            metadata,
            (rows_updated,),
        ]
        hot_store.conn = conn
        # A real async lock, so the write exercises the locked path.
        hot_store._lock = asyncio.Lock()
        return hot_store

    @pytest.mark.asyncio
    async def test_creates_the_row_when_it_does_not_exist(self) -> None:
        hot_store = self._store(None)
        s = _make_subscriber(hot_store=hot_store)
        await s._update_watermark("1700000000000-9")
        # The UPDATE ran (and matched no row)...
        assert hot_store.conn.execute.call_count == 2
        # ...so the row was created.
        hot_store.insert.assert_awaited_once()
        record = hot_store.insert.await_args.args[0]
        assert record.conversation_id == WATERMARK_CONVERSATION_ID
        assert record.content == WATERMARK_CONTENT
        assert record.metadata == {"last_message_id": "1700000000000-9"}

    @pytest.mark.asyncio
    async def test_updates_the_row_in_place_when_it_exists(self) -> None:
        """An UPDATE, not a delete-and-reinsert: it cannot lose the row."""
        hot_store = self._store("1700000000000-3", rows_updated=1)
        s = _make_subscriber(hot_store=hot_store)
        await s._update_watermark("1700000000000-9")
        hot_store.insert.assert_not_awaited()
        update_call = hot_store.conn.execute.call_args_list[1]
        assert update_call.args[0].startswith("UPDATE conversations SET metadata")
        assert update_call.args[1][0] == {"last_message_id": "1700000000000-9"}
        assert update_call.args[1][2] == WATERMARK_CONVERSATION_ID

    @pytest.mark.asyncio
    async def test_a_lower_id_does_not_overwrite_a_higher_one(self) -> None:
        """The watermark is monotonic: it holds the highest id indexed."""
        hot_store = self._store("1700000000000-9")
        s = _make_subscriber(hot_store=hot_store)
        await s._update_watermark("1700000000000-3")
        # Only the read happened; nothing was written.
        assert hot_store.conn.execute.call_count == 1
        hot_store.insert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_logs_warning_when_the_store_cannot_write(self, caplog: pytest.LogCaptureFixture) -> None:
        hot_store = self._store(None)
        hot_store.conn.execute.side_effect = RuntimeError("duckdb fail")
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
    async def test_uses_one_stable_consumer_name(self) -> None:
        s = _make_subscriber()
        client = _FakeRedisClient()
        seen: list[str] = []

        async def record_consumer(*, consumername: str, **_: Any) -> list[Any]:
            seen.append(consumername)
            if len(seen) >= 2:
                raise asyncio.CancelledError()
            return []

        client.xreadgroup.side_effect = record_consumer
        s._redis_client = client
        s._running = True
        with pytest.raises(asyncio.CancelledError):
            await s._run_loop()
        assert len(seen) == 2
        assert len(set(seen)) == 1

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


@pytest.fixture
def subscriber() -> BodaiToolInvocationSubscriber:
    """A subscriber with the test-suite defaults."""
    return _make_subscriber()


class TestEnvelopePayloadIsAlwaysADict:
    """A non-dict payload must be coerced, not passed through.

    ``_index_envelope`` does ``payload.get("version")`` and
    ``_build_content`` does ``payload.get("tool_name")``. A payload
    that is a string raises AttributeError inside the indexing path,
    the message is never ACKed, and it is re-delivered on every read
    -- a poison message that never drains.

    The canonical ``envelope=`` blob is the one shape that did not
    coerce, while the two newer shapes did. The asymmetry was
    preserved through a refactor on the argument that the canonical
    envelope is "an object by definition" -- true, but the failure
    mode is a stuck message, not a clean rejection.
    """

    def test_a_string_payload_becomes_an_empty_dict(
        self, subscriber: BodaiToolInvocationSubscriber
    ) -> None:
        envelope = subscriber._decode_envelope(
            {"envelope": json.dumps({"topic": "t", "payload": "not a dict"})}
        )
        assert envelope is not None
        assert envelope["payload"] == {}

    def test_a_list_payload_becomes_an_empty_dict(
        self, subscriber: BodaiToolInvocationSubscriber
    ) -> None:
        envelope = subscriber._decode_envelope(
            {"envelope": json.dumps({"topic": "t", "payload": [1, 2, 3]})}
        )
        assert envelope is not None
        assert envelope["payload"] == {}

    def test_a_dict_payload_is_kept(
        self, subscriber: BodaiToolInvocationSubscriber
    ) -> None:
        envelope = subscriber._decode_envelope(
            {"envelope": json.dumps({"topic": "t", "payload": {"k": "v"}})}
        )
        assert envelope is not None
        assert envelope["payload"] == {"k": "v"}


class TestAPoisonMessageCannotStarveThePipeline:
    """A message that keeps failing must not be retried forever.

    With a STABLE consumer name (the leak fix), a message that is
    never ACKed stays in that consumer's PEL, and every XREADGROUP
    re-delivers it -- so it is served again and again. Once ten such
    messages accumulate they fill the ``count=10`` batch, and because
    pending entries are ordered before new ones, every new message is
    starved behind them. The pipeline stops without any error.

    The fix bounds the retries: after MAX_INDEX_RETRIES the message is
    ACKed and dropped with a warning, so the PEL drains.
    """

    @staticmethod
    def _failing_subscriber() -> BodaiToolInvocationSubscriber:
        return _make_subscriber()

    async def test_a_failing_message_is_retried_before_being_dropped(self) -> None:
        subscriber = self._failing_subscriber()
        client = MagicMock()
        client.xack = AsyncMock()

        with patch.object(
            subscriber,
            "_index_envelope",
            AsyncMock(side_effect=RuntimeError("indexing is down")),
        ):
            for _ in range(MAX_INDEX_RETRIES - 1):
                await subscriber._process_entry(
                    [b"1-1", {"channel": "bodai.hooks.post-tool-use", "payload": "{}"}], client=client
                )

        # Not yet dropped: the message stays pending for a retry.
        client.xack.assert_not_awaited()

    async def test_a_message_that_keeps_failing_is_eventually_dropped(self) -> None:
        subscriber = self._failing_subscriber()
        client = MagicMock()
        client.xack = AsyncMock()

        with patch.object(
            subscriber,
            "_index_envelope",
            AsyncMock(side_effect=RuntimeError("indexing is down")),
        ):
            for _ in range(MAX_INDEX_RETRIES):
                await subscriber._process_entry(
                    [b"1-1", {"channel": "bodai.hooks.post-tool-use", "payload": "{}"}], client=client
                )

        # Dropped, so the PEL drains and new messages are not starved.
        client.xack.assert_awaited_once_with(
            "bodai:events", "akosha-tool-invocation-indexers", "1-1"
        )

    async def test_a_successful_message_clears_its_retry_count(self) -> None:
        """A transient failure must not consume the retry budget."""
        subscriber = self._failing_subscriber()
        client = MagicMock()
        client.xack = AsyncMock()

        with patch.object(
            subscriber,
            "_index_envelope",
            AsyncMock(side_effect=RuntimeError("indexing is down")),
        ):
            await subscriber._process_entry(
                [b"1-1", {"channel": "bodai.hooks.post-tool-use", "payload": "{}"}], client=client
            )

        with patch.object(subscriber, "_index_envelope", AsyncMock(return_value=True)):
            await subscriber._process_entry(
                [b"1-1", {"channel": "bodai.hooks.post-tool-use", "payload": "{}"}], client=client
            )

        assert subscriber._index_retry_counts.get("1-1", 0) == 0

    async def test_a_timeout_is_bounded_too(self) -> None:
        """A timeout leaves the message pending, but not forever."""
        subscriber = self._failing_subscriber()
        client = MagicMock()
        client.xack = AsyncMock()

        with patch.object(subscriber, "_index_envelope", AsyncMock(side_effect=TimeoutError)):
            for _ in range(MAX_INDEX_RETRIES):
                await subscriber._process_entry(
                    [b"1-1", {"channel": "bodai.hooks.post-tool-use", "payload": "{}"}], client=client
                )

        client.xack.assert_awaited_once()


class TestReleaseConsumerWhenThePendingCountCannotBeRead:
    """An unreadable pending count must leave the consumer registered.

    ``XGROUP DELCONSUMER`` destroys every message still pending for the
    consumer. If the pending count cannot be determined, the safe move
    is to leave the consumer alone: a stale consumer is recoverable, a
    destroyed message is not.
    """

    async def test_the_consumer_is_kept_when_xpending_raises(self) -> None:
        subscriber = _make_subscriber()
        client = MagicMock()
        client.xpending_range = AsyncMock(side_effect=RuntimeError("connection reset"))
        client.xgroup_delconsumer = AsyncMock(return_value=0)
        subscriber._redis_client = client
        subscriber._consumer_name = "akosha-stable"

        await subscriber._release_consumer(client)

        client.xgroup_delconsumer.assert_not_awaited()

    async def test_a_consumer_with_pending_messages_is_kept(self) -> None:
        subscriber = _make_subscriber()
        client = MagicMock()
        client.xpending_range = AsyncMock(
            return_value=[{"message_id": "1-1", "consumer": "akosha-stable"}]
        )
        client.xgroup_delconsumer = AsyncMock(return_value=0)
        subscriber._redis_client = client
        subscriber._consumer_name = "akosha-stable"

        await subscriber._release_consumer(client)

        client.xgroup_delconsumer.assert_not_awaited()

    async def test_an_empty_consumer_is_removed(self) -> None:
        subscriber = _make_subscriber()
        client = MagicMock()
        client.xpending_range = AsyncMock(return_value=[])
        client.xgroup_delconsumer = AsyncMock(return_value=0)
        subscriber._redis_client = client
        subscriber._consumer_name = "akosha-stable"

        await subscriber._release_consumer(client)

        client.xgroup_delconsumer.assert_awaited_once_with(
            "bodai:events", "akosha-tool-invocation-indexers", "akosha-stable"
        )


def _indexable_entry() -> dict[str, Any]:
    """A stream entry on a channel that IS indexed.

    ``_process_entry`` skips (and ACKs) any topic outside
    ``INDEXED_CHANNELS`` without attempting an index -- so a test that
    wants to reach the indexing path has to use one of them.
    """
    return {"channel": "bodai.hooks.post-tool-use", "payload": "{}"}


class TestTheIdleStreamStillReclaimsPendingEntries:
    """A failed message on a quiet stream must still be retried.

    ``_retry_pending`` used to run only after a NON-EMPTY ``xreadgroup``
    response. On a quiet stream the read blocks for ``block_ms`` and
    returns empty, so the sweep never ran -- and the pending entry sat
    in the PEL until the NEXT message arrived. On a low-traffic stream
    that is minutes or hours; on a stream that has gone quiet for good
    it is for ever: the entry is never retried, never dropped, and the
    retry cap is never reached.

    The sweep is the ONLY thing that can reclaim a pending entry --
    ``XREADGROUP`` with ``>`` never returns one -- so a sweep that is
    gated on traffic is a sweep that stops.
    """

    async def test_the_sweep_runs_even_when_no_new_message_arrived(self) -> None:
        s = _make_subscriber()
        client = _FakeRedisClient()
        client.xreadgroup.side_effect = [[], asyncio.CancelledError()]
        s._redis_client = client
        s._running = True

        with pytest.raises(asyncio.CancelledError):
            await s._run_loop()

        client.xautoclaim.assert_awaited()

    async def test_a_reclaimed_entry_on_an_idle_stream_is_processed(self) -> None:
        """Not merely claimed -- it has to reach the indexing path."""
        s = _make_subscriber()
        client = _FakeRedisClient()
        client.xautoclaim = AsyncMock(
            return_value=["0-0", [(b"1-1", _indexable_entry())], []]
        )
        client.xreadgroup.side_effect = [[], asyncio.CancelledError()]
        s._redis_client = client
        s._running = True

        with (
            patch.object(s, "_index_envelope", AsyncMock(return_value=True)) as index,
            pytest.raises(asyncio.CancelledError),
        ):
            await s._run_loop()

        index.assert_awaited()
        client.xack.assert_awaited()

    async def test_the_sweep_is_not_run_on_every_idle_iteration(self) -> None:
        """One round trip per idle poll would be 40 a minute.

        The sweep can only ever claim an entry that has been pending for
        at least ``RETRY_CLAIM_MIN_IDLE_MS``, so sweeping more often
        than that cannot reclaim anything new.
        """
        s = _make_subscriber()
        client = _FakeRedisClient()
        client.xreadgroup.side_effect = [[], [], [], asyncio.CancelledError()]
        s._redis_client = client
        s._running = True

        with pytest.raises(asyncio.CancelledError):
            await s._run_loop()

        assert client.xautoclaim.await_count == 1


class TestPendingEntriesAreActuallyRedelivered:
    """The retry cap is only reachable if the runtime delivers the retry.

    ``XREADGROUP`` with ``>`` NEVER returns a message that is already
    pending for this consumer. Verified against Redis 7.4: a message
    read and not ACKed comes back once from ``>`` and never again --
    only ``0`` or ``XAUTOCLAIM`` brings it back.

    So without a reclaim sweep, a message whose indexing failed sits in
    the PEL forever: never retried, never dropped, and the retry cap
    never reached. The counter would stall at 1 and the drop path
    would be dead code that the tests nevertheless exercised.
    """

    async def test_the_sweep_reclaims_this_consumers_own_pending_entries(self) -> None:
        subscriber = _make_subscriber()
        subscriber._consumer_name = "akosha-stable"
        client = MagicMock()
        client.xautoclaim = AsyncMock(
            return_value=["0-0", [(b"1-1", _indexable_entry())], []]
        )
        client.xack = AsyncMock()

        await subscriber._retry_pending(client)

        client.xautoclaim.assert_awaited_once()
        # redis-py: xautoclaim(name, groupname, consumername, ...) -- the
        # consumer name is the THIRD POSITIONAL argument.
        args = client.xautoclaim.await_args.args
        assert args[2] == "akosha-stable"

    async def test_a_reclaimed_entry_is_processed(self) -> None:
        subscriber = _make_subscriber()
        subscriber._consumer_name = "akosha-stable"
        client = MagicMock()
        client.xautoclaim = AsyncMock(
            return_value=["0-0", [(b"1-1", _indexable_entry())], []]
        )
        client.xack = AsyncMock()

        with patch.object(subscriber, "_index_envelope", AsyncMock(return_value=True)) as index:
            await subscriber._retry_pending(client)

        # The reclaimed entry went through the indexing path -- not just
        # the ack path a non-indexed channel would take.
        index.assert_awaited()
        client.xack.assert_awaited()

    async def test_a_failed_sweep_does_not_stop_the_loop(self) -> None:
        """Redis trouble in the sweep must not take the subscriber down."""
        subscriber = _make_subscriber()
        subscriber._consumer_name = "akosha-stable"
        client = MagicMock()
        client.xautoclaim = AsyncMock(side_effect=RuntimeError("connection reset"))

        await subscriber._retry_pending(client)

    async def test_an_empty_claim_does_nothing(self) -> None:
        subscriber = _make_subscriber()
        subscriber._consumer_name = "akosha-stable"
        client = MagicMock()
        client.xautoclaim = AsyncMock(return_value=["0-0", [], []])
        client.xack = AsyncMock()

        await subscriber._retry_pending(client)

        client.xack.assert_not_awaited()

    async def test_the_sweep_resumes_from_the_cursor_redis_handed_back(self) -> None:
        """Redis returns a cursor; discarding it re-scans the same prefix.

        ``XAUTOCLAIM`` examines at most ``count * 10`` entries of the
        group's PEL per call, and an entry skipped for insufficient
        idle time CONSUMES that budget -- it is examined, not claimed.
        Starting every sweep at ``"0-0"`` therefore re-examines the same
        low-id prefix for ever. When that prefix is entirely inside the
        idle window, the sweep claims nothing and the entries past it
        are unreachable.
        """
        subscriber = _make_subscriber()
        subscriber._consumer_name = "akosha-stable"
        client = MagicMock()
        client.xautoclaim = AsyncMock(
            return_value=["10-0", [(b"1-1", _indexable_entry())], []]
        )
        client.xack = AsyncMock()

        await subscriber._retry_pending(client)
        await subscriber._retry_pending(client)

        calls = client.xautoclaim.await_args_list
        assert calls[0].kwargs["start_id"] == "0-0"
        assert calls[1].kwargs["start_id"] == "10-0"

    async def test_the_cursor_wraps_when_redis_reports_the_scan_finished(self) -> None:
        """A cursor of ``0-0`` means the scan reached the end of the PEL.

        The next sweep must start over, or the entries at the low end
        would never be looked at again.
        """
        subscriber = _make_subscriber()
        subscriber._consumer_name = "akosha-stable"
        client = MagicMock()
        client.xautoclaim = AsyncMock(
            return_value=["0-0", [(b"1-1", _indexable_entry())], []]
        )
        client.xack = AsyncMock()

        await subscriber._retry_pending(client)
        await subscriber._retry_pending(client)

        calls = client.xautoclaim.await_args_list
        assert calls[0].kwargs["start_id"] == "0-0"
        assert calls[1].kwargs["start_id"] == "0-0"

    async def test_the_claim_idle_time_exceeds_the_processing_timeout(self) -> None:
        """A peer must not be able to claim an entry that is still in flight.

        ``XAUTOCLAIM`` reclaims across the WHOLE consumer group and
        transfers ownership. An entry whose idle time equals the
        per-event processing timeout is therefore eligible for a peer's
        claim while this process is still working on it -- concurrent
        double-processing, and a duplicate-key error for whoever loses
        the race.
        """
        s = _make_subscriber(per_event_timeout_seconds=30.0)
        s._consumer_name = "akosha-stable"
        client = MagicMock()
        client.xautoclaim = AsyncMock(return_value=["0-0", [], []])

        await s._retry_pending(client)

        min_idle = client.xautoclaim.await_args.kwargs["min_idle_time"]
        assert min_idle > s._per_event_timeout_seconds * 1000

    async def test_a_longer_configured_timeout_raises_the_claim_idle_time(self) -> None:
        """The floor is a floor: the configured timeout wins when it is longer."""
        s = _make_subscriber(per_event_timeout_seconds=120.0)
        s._consumer_name = "akosha-stable"
        client = MagicMock()
        client.xautoclaim = AsyncMock(return_value=["0-0", [], []])

        await s._retry_pending(client)

        min_idle = client.xautoclaim.await_args.kwargs["min_idle_time"]
        assert min_idle >= 120.0 * 1000

    async def test_a_retry_that_fails_again_increments_the_counter(self) -> None:
        """The cap counts DELIVERIES, and the sweep is what delivers them."""
        subscriber = _make_subscriber()
        subscriber._consumer_name = "akosha-stable"
        client = MagicMock()
        client.xautoclaim = AsyncMock(
            return_value=["0-0", [(b"1-1", _indexable_entry())], []]
        )
        client.xack = AsyncMock()

        with patch.object(
            subscriber, "_index_envelope", AsyncMock(side_effect=RuntimeError("down"))
        ):
            await subscriber._retry_pending(client)

        assert subscriber._index_retry_counts.get("1-1") == 1


class TestTheBatchIsBoundedByOneEntriesTimeout:
    """A batch must not cost ten times one entry's timeout.

    Each entry's indexing is capped at ``per_event_timeout_seconds``
    (30s). Processed one after another, a batch of ten could hold the
    read loop for 300 seconds -- and with a hung embedding service a
    backlog drains at a fraction of a message per second. The entries
    in a batch are processed concurrently, so the batch costs one
    entry's timeout, not ten.
    """

    async def test_the_entries_in_a_batch_are_processed_concurrently(self) -> None:
        subscriber = _make_subscriber()
        client = MagicMock()
        client.xack = AsyncMock()
        in_flight = 0
        peak = 0

        async def _slow_index(*_args: Any, **_kwargs: Any) -> bool:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.05)
            in_flight -= 1
            return True

        entries = [
            [str(i).encode(), {"channel": "bodai.hooks.post-tool-use", "payload": "{}"}]
            for i in range(4)
        ]
        with patch.object(subscriber, "_index_envelope", _slow_index):
            await subscriber._process_response([["bodai:events", entries]], client=client)

        assert peak > 1, "the entries must be indexed concurrently"

    async def test_a_failure_in_one_entry_does_not_stop_the_others(self) -> None:
        subscriber = _make_subscriber()
        client = MagicMock()
        client.xack = AsyncMock()
        entries = [
            [b"1-1", {"channel": "bodai.hooks.post-tool-use", "payload": "{}"}],
            [b"2-2", {"channel": "bodai.hooks.post-tool-use", "payload": "{}"}],
        ]
        with patch.object(
            subscriber,
            "_index_envelope",
            AsyncMock(side_effect=[RuntimeError("boom"), True]),
        ):
            await subscriber._process_response([["bodai:events", entries]], client=client)

        # The second entry still landed.
        assert client.xack.await_count >= 1

    async def test_the_consumed_count_is_published(self) -> None:
        """The health payload needs the producer's WORK."""
        subscriber = _make_subscriber()
        client = MagicMock()
        client.xack = AsyncMock()
        entries = [
            [b"1-1", {"channel": "bodai.hooks.post-tool-use", "payload": "{}"}],
            [b"2-2", {"channel": "bodai.hooks.post-tool-use", "payload": "{}"}],
        ]
        with patch.object(subscriber, "_index_envelope", AsyncMock(return_value=True)):
            await subscriber._process_response([["bodai:events", entries]], client=client)

        assert subscriber.entries_consumed == 2
