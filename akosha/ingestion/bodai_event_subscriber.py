r"""A kosha -> bodai:events Redis Streams subscriber (push mode).

Plan: docs/plans/2026-08-29-push-subscriber.md Phase 2.

Push subscriber for Mahavishnu's websocket tool invocations. The
historical poll fallback (5-second interval poll of
``websocket_tool_invocation/v1/*``) was removed in 2026-09-26; this
Redis Streams subscription is now the sole ingestion path.
Mahavishnu's producer (commit 7b2c498c) ``xadd``\s envelopes onto the
``bodai:events`` stream; this subscriber consumes them, filters to
``topic == "websocket_tool_invocation"``, embeds each payload via the
existing ``EmbeddingService``, and inserts a ``HotRecord`` into the
HotStore. Rows whose schema version doesn't match the active
``SUPPORTED_SCHEMA_VERSION`` are silently skipped (forward-compat).

A persistent watermark row in the HotStore carries the highest message id
indexed so far. It is NOT the resume point: the read asks for NEW
messages (``>``), and the consumer group's own ``last-delivered-id`` is
the position that survives a restart. An explicit read id would be a
PEL read -- it can only replay the calling consumer's own pending
entries and can never deliver a message nobody has seen -- so a restart
would go permanently deaf. The watermark's job is to say where a
NEWLY CREATED consumer group starts, so that a group created over an
already-indexed stream does not replay the back-catalogue, and to
record how far the pipeline has got. The watermark row uses
``conversation_id="__bodai_subscriber_watermark__"`` (reserved;
``akosha_search_all_systems`` and friends must filter it out at query time).

Fail-soft contract:

* Redis missing, ``redis.asyncio`` import failure, or stream unreachable
  -> subscriber stays non-running (``_running=False``); the orchestrator
  (``WebSocketInvocationsSubscriber``) logs the failure and stays
  not-running until the next call to ``start()``.
* Per-message decode / embed / insert failure -> log at WARNING, skip the
  message (no XACK on success-path insert only; we XACK after
  ``hot_store.insert`` returns, so a transient HotStore error surfaces
  as a ``WARNING`` and the same message stays PENDING). It is
  redelivered by the reclaim sweep -- ``XAUTOCLAIM``, which reclaims
  entries idle for at least ``RETRY_CLAIM_MIN_IDLE_MS`` -- and NOT by
  the next ``xreadgroup``: a ``>`` read never returns a pending entry.
  The sweep runs on the idle path too, so a stream that has gone quiet
  still retries what it failed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from akosha.models import RESERVED_WATERMARK_CONVERSATION_ID, HotRecord
from akosha.processing.embeddings import get_embedding_service

logger = logging.getLogger(__name__)


#: Redis stream name; matches the producer-side constant in
#: ``mahavishnu/core/events/redis_publisher.py``.
STREAM_NAME: str = "bodai:events"

#: The topic this subscriber filters for. Anything else on the stream
#: is XACK'd but not indexed into the HotStore.
TOOL_INVOCATION_TOPIC: str = "websocket_tool_invocation"

#: Channels whose payloads become HotStore rows. ``websocket_tool_invocation``
#: is the canonical name; ``bodai.hooks.post-tool-use`` is what
#: ``mahavishnu``'s hook bridge actually publishes to ``bodai:events``.
INDEXED_CHANNELS: frozenset[str] = frozenset(
    {"websocket_tool_invocation", "bodai.hooks.post-tool-use"}
)

#: The channel ``mahavishnu``'s hook bridge publishes tool events on.
HOOK_EVENT_CHANNEL: str = "bodai.hooks.post-tool-use"

#: How many times a message may fail to index before it is dropped.
#:
#: A message that is never ACKed stays in the consumer's PEL. It is
#: NOT re-delivered by the read -- ``>`` never returns a pending entry
#: -- so the reclaim sweep is what brings it back. The sweep runs at
#: most once per :data:`RETRY_CLAIM_MIN_IDLE_MS`, so a message that
#: keeps failing occupies a pending-list slot for as long as it takes
#: to reach this cap, and the pending list grows if failures outpace
#: the sweep.
#:
#: Three attempts is enough to ride out a transient outage (an
#: embedding service hiccup, a hot-store lock) and short enough that
#: the pending list drains. After the cap the message is ACKed and
#: dropped with a warning: a lost row is visible, a growing pending
#: list is not.
MAX_INDEX_RETRIES: int = 3

#: How long a pending entry must have been idle before it is reclaimed
#: for a retry. This is what SPACES the retries: an entry is not
#: redelivered the instant it fails, so a transient outage gets a real
#: second chance rather than three immediate ones.
#:
#: It is a FLOOR, not the value. The effective idle time is the larger
#: of this and twice the per-event processing timeout, because
#: ``XAUTOCLAIM`` reclaims across the WHOLE consumer group: an idle
#: time equal to the processing timeout would make an entry eligible
#: for a peer's claim while this process is still working on it.
RETRY_CLAIM_MIN_IDLE_MS: int = 30_000

#: The claim idle time must exceed the per-event processing timeout by
#: this factor, so an entry is never eligible for a peer's claim while
#: it is still being processed.
RETRY_CLAIM_IDLE_FACTOR: float = 2.0

#: How many pending entries one reclaim sweep may bring back.
RETRY_CLAIM_COUNT: int = 10

#: Forward-compat gate — only rows whose ``payload["version"]`` matches
#: this constant are indexed. Mirror of ``SUPPORTED_SCHEMA_VERSION`` in
#: ``websocket_invocations_subscriber``.
SUPPORTED_SCHEMA_VERSION: str = "1.0.0"

#: Reserved ``conversation_id`` for the watermark row. Distinct from
#: any real envelope key (``bodai:events`` stream IDs) so search
#: queries can filter it out trivially. The value lives in
#: :mod:`akosha.models` because the hot store's queries must exclude
#: the same row.
WATERMARK_CONVERSATION_ID: str = RESERVED_WATERMARK_CONVERSATION_ID

#: ``system_id`` used for both indexed rows and the watermark.
SYSTEM_ID_MAHAVISHNU: str = "mahavishnu"

#: Sentinel watermark row content (search-time filters exclude this).
WATERMARK_CONTENT: str = "__watermark__"

#: Connect timeout for the subscriber's Redis client, in seconds.
#:
#: A read on this group may legitimately take minutes. A connect never
#: does -- so a connect that never completes must fail fast instead of
#: hanging the read loop.
SUBSCRIBER_CONNECT_TIMEOUT_SECONDS: float = 10.0

#: Multiplier applied to the per-event processing timeout to get the
#: socket timeout.
#:
#: The socket timeout bounds the wall-clock wait for a reply, and the
#: reply is delayed by whatever the event loop is doing. The event
#: loop's longest occupancy is the per-entry processing timeout: a
#: batch of entries is processed CONCURRENTLY, so the batch is bounded
#: by ONE entry's timeout, not by the batch size times it. The factor
#: leaves room for the BLOCK duration and for scheduling jitter on
#: top of that bound.
SOCKET_TIMEOUT_PER_EVENT_FACTOR: float = 2.0

#: Floor on the socket timeout, in seconds. It has to clear the BLOCK
#: duration (``xreadgroup_block_ms``, 1500 ms by default) with room to
#: spare: a BLOCK that outlives its own socket timeout is a timeout on
#: a healthy server.
SOCKET_TIMEOUT_FLOOR_SECONDS: float = 10.0

#: Phrases that identify a duplicate primary key across the storage
#: backends Akosha can be configured with.
#:
#: DuckDB raises ``ConstraintException`` with "Duplicate key ... violates
#: primary key constraint"; PostgreSQL raises ``UniqueViolation`` with
#: "duplicate key value violates unique constraint". The two backends
#: raise different exception types, so the MESSAGE is the portable
#: signal -- and it is the precise one: a NOT NULL violation is a real
#: failure and must not be mistaken for a replay.
_DUPLICATE_KEY_MARKERS: tuple[str, ...] = (
    "duplicate key",
    "violates primary key constraint",
    "violates unique constraint",
)


def _is_duplicate_key_error(exc: BaseException) -> bool:
    """Whether ``exc`` says the row is ALREADY stored.

    The substrate's ``INSERT`` is not idempotent -- it has no
    ``ON CONFLICT`` clause -- so re-inserting a ``conversation_id``
    that is already present raises. For the subscriber that is not a
    failure: it is a REPLAY of a message whose work is already done,
    and the row is present, which is the desired end state.

    The live proof, 2026-11-21: a backlog replay logged 170 duplicate
    key failures for rows that were already in DuckDB. Each one parked
    its message in the PEL for three pointless retries.
    """
    text = str(exc).lower()
    return any(marker in text for marker in _DUPLICATE_KEY_MARKERS)


#: Exponential backoff bounds for reconnect-after-error.
DEFAULT_RECONNECT_BACKOFF_SECONDS: float = 1.0
DEFAULT_RECONNECT_BACKOFF_MAX_SECONDS: float = 30.0

#: The only ``xreadgroup`` id that delivers a message nobody has seen.
#:
#: Redis documents every other id as a PEL read: "Any other ID, that is,
#: 0 or any other valid ID or incomplete ID (just the millisecond time
#: part), will have the effect of returning entries that are pending for
#: the consumer sending the command with IDs greater than the one
#: provided. So basically if the ID is not '>', then the command will
#: just let the client access its pending entries."
#:
#: So an explicit id -- the watermark plus one, say -- can NEVER deliver
#: a brand-new message: it only replays the calling consumer's own
#: pending entries, and a restarted process owns none. The consumer
#: group's ``last-delivered-id`` is the durable position instead: it
#: outlives the process, and ``>`` delivers everything after it.
NEW_MESSAGES: str = ">"


def _stream_id_key(message_id: str) -> tuple[int, int] | None:
    """Sortable key for a Redis stream id (``<ms>-<seq>``).

    ``None`` when the id is malformed -- callers read that as "cannot
    compare", not as "older".
    """
    try:
        ms_str, seq_str = message_id.rsplit("-", 1)
        return (int(ms_str), int(seq_str))
    except AttributeError, ValueError:
        return None


def _is_newer(candidate: str, current: str) -> bool:
    """Whether ``candidate`` is strictly newer than ``current``.

    The rule is asymmetric on purpose, because the two sides fail
    differently:

    * A candidate that does not parse is REJECTED. Accepting it would
      overwrite a good watermark with a garbage id, and the next read
      would reject the row as malformed and report "no watermark" --
      destroying the progress record and, with it, the start position
      a newly created consumer group would use.
    * A current that does not parse is IGNORED. A valid id beats
      garbage, so the candidate wins.

    Equality is not an advance: the watermark already records that id.
    """
    candidate_key = _stream_id_key(candidate)
    if candidate_key is None:
        return False
    current_key = _stream_id_key(current)
    if current_key is None:
        return True
    return candidate_key > current_key


def _message_id_from_metadata(metadata: object) -> str | None:
    """Extract ``last_message_id`` from a watermark metadata value.

    DuckDB returns JSON columns as strings, so the value arrives as a
    JSON string, a ``dict``, or bytes depending on the path.
    """
    value: object = metadata
    if isinstance(value, (bytes, bytearray)):
        value = value.decode(errors="replace")
    if isinstance(value, str):
        try:
            decoded: object = json.loads(value)
        except TypeError, ValueError:
            return None
        value = decoded
    if not isinstance(value, dict):
        return None
    last_id = value.get("last_message_id")
    if isinstance(last_id, (bytes, bytearray)):
        return last_id.decode(errors="replace")
    if isinstance(last_id, str):
        return last_id
    return None


def _create_redis_client(redis_url: str, *, socket_timeout: float) -> Any | None:
    """Return an async redis client or ``None`` when redis.asyncio is unavailable.

    Mirrors the lazy-creation pattern in
    ``mahavishnu/core/events/bodai_subscriber.py:_create_redis_client``:
    when ``redis.asyncio`` is not installed, ``None`` signals the
    caller to idle until cancellation.

    The socket timeout is REQUIRED, not inherited. redis-py 8's
    default is 5 seconds, and that default is a bound on the SERVER's
    work -- not on how long the client should tolerate an idle BLOCK.
    The reply to this subscriber's read is delayed by whatever the
    event loop is doing, and the event loop is doing per-entry
    embedding and DuckDB work.

    The live proof, 2026-11-21: with the default in place, a backlog
    replay logged 31 ``xreadgroup error: Timeout reading from
    localhost:6379`` failures. Every one aborted client-side against a
    server that was answering normally, and each aborted read had
    already registered its consumer on the server.

    The connect timeout is a different bound: a read on this group may
    legitimately take minutes, but a connect never does, so a connect
    that never completes must fail fast rather than hang the loop.
    """
    try:
        import redis.asyncio as aioredis
    except ImportError:
        logger.warning(
            "akosha.bodai_subscriber: redis.asyncio is not installed; "
            "subscriber will idle and the orchestrator will fall back to polling",
        )
        return None
    return aioredis.from_url(
        redis_url,
        decode_responses=False,
        socket_timeout=socket_timeout,
        socket_connect_timeout=SUBSCRIBER_CONNECT_TIMEOUT_SECONDS,
    )


class BodaiToolInvocationSubscriber:
    r"""Consume ``bodai:events`` Redis stream into HotStore.

    Mirrors ``WebSocketInvocationsSubscriber``'s shape
    (``start``/``stop``/``_running``/``_task``) so the orchestrator can
    compose them. Lazy redis client creation lets the subscriber survive
    a missing ``redis.asyncio`` install or an unreachable broker.

    Attributes:
        _redis_url: Connection string for the Redis broker.
        _consumer_group: Redis-Streams consumer group name (idempotently
            created on ``start()``).
        _hot_store: HotStore handle (AsyncMock-friendly). ``None``
            short-circuits ``start()`` to a no-op.
        _xreadgroup_block_ms: Block timeout passed to ``xreadgroup``.
        _per_event_timeout_seconds: Timeout for the per-message
            decode+embed+insert pipeline.
        _consumer_name: Stable name within the consumer group. When
            ``None``, a uuid4 hex is generated at ``start()`` so
            multiple Akosha processes don't compete for the same slot.
        _running: ``True`` while the read loop is active.
        _task: The asyncio task running the loop, if any.
        _redis_client: Lazily constructed redis client (``aclose``\d
            in ``stop``).
    """

    def __init__(
        self,
        *,
        redis_url: str,
        consumer_group: str = "akosha-tool-invocation-indexers",
        hot_store: Any = None,
        xreadgroup_block_ms: int = 1500,
        per_event_timeout_seconds: float = 30.0,
        consumer_name: str | None = None,
    ) -> None:
        """Initialize the subscriber.

        Args:
            redis_url: Connection string (e.g. ``redis://localhost:6379/0``).
            consumer_group: Redis Streams consumer group.
            hot_store: HotStore instance. ``None`` makes ``start()`` a no-op.
            xreadgroup_block_ms: Block timeout for ``xreadgroup``.
            per_event_timeout_seconds: Timeout for the per-message pipeline.
            consumer_name: Stable consumer name. ``None`` auto-generates.
        """
        self._redis_url = redis_url
        self._consumer_group = consumer_group
        self._hot_store = hot_store
        self._xreadgroup_block_ms = xreadgroup_block_ms
        self._per_event_timeout_seconds = per_event_timeout_seconds
        # How long a pending entry must be idle before the sweep may
        # reclaim it. Longer than the per-event processing timeout, so
        # a peer process cannot claim an entry that this process is
        # still working on. See :data:`RETRY_CLAIM_IDLE_FACTOR`.
        self._retry_claim_min_idle_ms = max(
            RETRY_CLAIM_MIN_IDLE_MS,
            int(per_event_timeout_seconds * 1000 * RETRY_CLAIM_IDLE_FACTOR),
        )
        self._consumer_name = consumer_name
        self._consumer_name_supplied = consumer_name is not None
        # The socket timeout for the Redis client, derived from the
        # bounds this subscriber already carries. See
        # :attr:`socket_timeout_seconds`.
        self._socket_timeout_seconds = max(
            SOCKET_TIMEOUT_FLOOR_SECONDS,
            self._per_event_timeout_seconds * SOCKET_TIMEOUT_PER_EVENT_FACTOR,
            (self._xreadgroup_block_ms / 1000.0) * SOCKET_TIMEOUT_PER_EVENT_FACTOR,
        )
        # message_id -> how many times indexing has failed for it. See
        # MAX_INDEX_RETRIES for why this is bounded.
        self._index_retry_counts: dict[str, int] = {}
        # Serialises the watermark write. The write is a DELETE plus an
        # awaited INSERT on the same primary key, so two concurrent
        # updates could interleave into a duplicate-key error. See
        # ``_update_watermark``.
        self._watermark_lock = asyncio.Lock()
        # How many stream entries this subscriber has consumed. The
        # health payload publishes it as the producer's WORK, which is
        # what distinguishes "the feed is starved" from "the feed is
        # empty because there was nothing to ingest".
        self._entries_consumed = 0
        # How many consumed entries SHOULD have produced a HotStore
        # row: the indexed channels, plus the entries whose channel
        # could not be read at all. This is the counter the stall
        # signal must use -- "entries read" counts the traffic that
        # was never going to be indexed (the hook bridge publishes six
        # channels and only one of them is indexed), so gating on it
        # would report a healthy pipeline as stalled.
        self._entries_candidate = 0
        # How many HotStore rows this subscriber actually landed.
        self._entries_indexed = 0
        # How many times the read from Redis failed. Published in the
        # health payload so a transport failure is visible WITHOUT
        # grepping a log file: the rollback signal used to count
        # "xreadgroup error" lines in another component's log, which
        # reads zero forever once that file rotates or that component
        # is fixed.
        self._read_errors = 0
        # Monotonic timestamp of the last reclaim sweep. The sweep can
        # only ever claim an entry that has been pending for at least
        # ``RETRY_CLAIM_MIN_IDLE_MS``, so sweeping more often than that
        # cannot reclaim anything new -- it is one extra round trip per
        # idle poll, and an idle poll is every ``block_ms``. Zero means
        # "never swept", so the FIRST sweep runs immediately.
        self._last_sweep_at = 0.0
        # Where the reclaim sweep resumes. Redis hands back a cursor
        # with every ``XAUTOCLAIM`` reply; discarding it makes every
        # sweep re-examine the same low-id prefix of the pending list.
        self._claim_cursor: str = "0-0"
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._redis_client: Any = None

    @property
    def running(self) -> bool:
        """Whether the read loop is currently active.

        Orchestrators read this to decide whether to start the read
        poll loop. ``False`` means the subscriber idled (redis missing
        or unreachable) and the orchestrator should skip ingestion.
        """
        return self._running

    @property
    def entries_consumed(self) -> int:
        """How many stream entries this subscriber has consumed.

        Published as the producer's WORK in the health payload. It is
        what separates "the feed is starved" from "the feed is empty
        because there was nothing to ingest": a subscriber that has
        consumed entries and landed none is starving; a subscriber that
        has consumed none simply has nothing to do.

        This counts EVERY entry read, including the ones that were
        never going to be indexed. Use :attr:`entries_candidate` to
        decide whether anything should have landed.
        """
        return self._entries_consumed

    @property
    def entries_candidate(self) -> int:
        """How many consumed entries should have produced a HotStore row.

        This is the counter the stall signal gates on. It counts the
        entries on an indexed channel and the entries whose channel
        could not be read -- the two cases where a row was owed. The
        entries on a non-indexed channel are excluded: they are
        traffic the pipeline is meant to drop, and counting them would
        report a healthy pipeline as stalled.
        """
        return self._entries_candidate

    @property
    def entries_indexed(self) -> int:
        """How many HotStore rows this subscriber actually landed."""
        return self._entries_indexed

    @property
    def read_errors(self) -> int:
        """How many times the read from Redis has failed."""
        return self._read_errors

    @property
    def embedding_dim(self) -> int:
        """Embedding dim of the HotStore; used for the zero-vector watermark."""
        return getattr(self._hot_store, "_embedding_dim", 384)

    @property
    def socket_timeout_seconds(self) -> float:
        """The socket timeout the Redis client is built with, in seconds.

        Derived from the two bounds this subscriber already carries:
        the per-event processing timeout (the event loop's longest
        occupancy, because a batch is processed concurrently) and the
        BLOCK duration (how long an idle read waits for a reply).

        The socket timeout is a bound on the SERVER's work, so it has
        to clear both. redis-py 8's 5-second default clears neither,
        which is why a backlog replay logged 31 read timeouts against
        a server that was answering normally.
        """
        return self._socket_timeout_seconds

    async def start(self) -> None:
        """Connect, ensure consumer group, spawn the read loop.

        The group is created at the watermark, so a group that does not
        exist yet does not replay the back-catalogue. The read itself
        asks for NEW messages: the group's ``last-delivered-id`` is the
        position that survives a restart.

        No-op when already running or when ``hot_store is None``. When
        the redis client cannot be constructed (import failure or
        unreachable broker) the subscriber logs at WARNING and stays
        non-running; the orchestrator then surfaces a "no ingestion"
        signal to callers.
        """
        if self._running:
            return
        if self._hot_store is None:
            logger.debug("BodaiToolInvocationSubscriber: no hot_store, skipping start")
            return

        client = _create_redis_client(
            self._redis_url,
            socket_timeout=self._socket_timeout_seconds,
        )
        if client is None:
            # Import failure: _create_redis_client already logged.
            logger.warning(
                "akosha.bodai_subscriber.fallback_to_poll: redis client is None "
                "(import failure or unreachable broker)"
            )
            return

        # The watermark is where the group starts when the group does
        # not exist yet, so read it BEFORE creating the group. A failed
        # read is not fatal: the group then starts at "0" and the
        # back-catalogue is replayed. (``_read_watermark_id`` handles
        # its own failures -- it logs and reports "no watermark".)
        watermark = await self._read_watermark_id()

        # Persist the client; on any of the following failures, close
        # it and leave _running=False so the orchestrator falls back.
        try:
            await self._ensure_consumer_group(client, start_id=watermark or "0")
        except Exception as exc:
            logger.warning(
                "akosha.bodai_subscriber.fallback_to_poll: xgroup_create failed: %s",
                exc,
            )
            with contextlib.suppress(Exception):
                await client.aclose()
            return

        self._redis_client = client
        self._consumer_name = self._consumer_name or self._auto_consumer_name()
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "BodaiToolInvocationSubscriber started "
            "(stream=%s, group=%s, block_ms=%d, timeout=%.1fs, "
            "group_start_id=%s, read=%s)",
            STREAM_NAME,
            self._consumer_group,
            self._xreadgroup_block_ms,
            self._per_event_timeout_seconds,
            watermark or "0",
            NEW_MESSAGES,
        )

    async def stop(self) -> None:
        """Stop the read loop; close the redis client.

        Idempotent. Swallows ``CancelledError`` and other exceptions so
        the orchestrator's ``stop()`` always returns.
        """
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("BodaiToolInvocationSubscriber task raised: %s", exc)
        self._task = None
        client = self._redis_client
        if client is not None and self._consumer_name:
            await self._release_consumer(client)
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                with contextlib.suppress(Exception):
                    await client.close()
            self._redis_client = None
        if not self._consumer_name_supplied:
            self._consumer_name = None
        logger.info("BodaiToolInvocationSubscriber stopped")

    async def _release_consumer(self, client: Any) -> None:
        """Unregister this consumer, but only if it owns no pending messages.

        ``XGROUP DELCONSUMER`` destroys every message still pending for
        the consumer: the message is neither acked nor redelivered, and
        ``XAUTOCLAIM`` can no longer recover it. A message that failed to
        index would therefore be lost on every graceful restart. When the
        pending count cannot be determined, leave the consumer registered
        — a stale consumer is recoverable, a destroyed message is not.
        """
        name = self._consumer_name
        if not name:
            return
        pending: list[Any] | None = None
        try:
            pending = await client.xpending_range(
                STREAM_NAME,
                self._consumer_group,
                "-",
                "+",
                1,
                consumername=name,
            )
        except Exception as exc:
            logger.warning(
                "akosha.bodai_subscriber: could not read pending count for "
                "consumer %s (%s); leaving it registered",
                name,
                exc,
            )
        if pending is None:
            return
        if pending:
            logger.warning(
                "akosha.bodai_subscriber: consumer %s still owns pending "
                "messages; leaving it registered so they stay recoverable",
                name,
            )
            return
        with contextlib.suppress(Exception):
            await client.xgroup_delconsumer(
                STREAM_NAME,
                self._consumer_group,
                name,
            )

    async def _ensure_consumer_group(self, client: Any, *, start_id: str = "0") -> None:
        """Idempotently create the consumer group on ``bodai:events``.

        ``start_id`` is where the group begins WHEN IT IS CREATED -- the
        watermark, so a group created over an already-indexed stream
        does not replay the back-catalogue. It is ignored when the group
        already exists: the group's own ``last-delivered-id`` is the
        durable position, and it is always ahead of the watermark
        because it advances at read time, before indexing.

        Tolerates ``BUSYGROUP`` so re-running with an existing group is
        a no-op. ``mkstream=True`` means Redis creates the stream if it
        does not exist yet, so the group can be created before the first
        event is published and no sentinel ``xadd`` is needed.
        """
        try:
            await client.xgroup_create(
                name=STREAM_NAME,
                groupname=self._consumer_group,
                id=start_id,
                mkstream=True,
            )
            logger.info(
                "Created consumer group stream=%s group=%s start_id=%s",
                STREAM_NAME,
                self._consumer_group,
                start_id,
            )
        except Exception as exc:
            # BUSYGROUP is normal on restart; anything else logs at WARNING.
            msg = str(exc).lower()
            if "busygroup" not in msg:
                raise
            logger.debug(
                "Consumer group already exists stream=%s group=%s",
                STREAM_NAME,
                self._consumer_group,
            )

    async def _read_watermark_id(self) -> str | None:
        """The last message id this subscriber indexed, or ``None``.

        Read by primary key rather than by vector search. The watermark
        row carries a zero-vector embedding, and a zero-vector query
        against the HNSW index is degenerate -- the row is not reliably
        in the result set, so a search can miss a watermark that is
        right there. The row is addressed by a primary key, so read it
        directly.

        The read holds the store's lock for the whole execute-then-fetch
        pair. That is what makes it safe: the substrate's
        ``_SerializedConnection`` holds its threading lock only for the
        statement, and ``fetchone`` re-acquires it afterwards, so a
        write landing in that gap replaces the connection's current
        result. A concurrent insert would then be read as "no
        watermark", which silently drops the monotonicity guard.
        """
        try:
            row = await self._query_row(
                "SELECT metadata FROM conversations WHERE conversation_id = ?",
                [WATERMARK_CONVERSATION_ID],
            )
        except Exception as exc:
            logger.warning("BodaiToolInvocationSubscriber: watermark read failed: %s", exc)
            return None
        if not row:
            return None
        message_id = _message_id_from_metadata(row[0])
        if message_id is None:
            return None
        if _stream_id_key(message_id) is None:
            logger.warning(
                "BodaiToolInvocationSubscriber: malformed watermark %r; ignoring it",
                message_id,
            )
            return None
        return message_id

    async def _query_row(self, sql: str, params: list[Any]) -> Any:
        """Run one query and fetch its first row, holding the store's lock.

        The lock is the store's, not this subscriber's: every operation
        that goes through the HotStore's API takes it, so holding it
        makes the connection quiescent for the whole execute-then-fetch
        pair. This is the pattern
        :mod:`akosha.storage.metrics_query` uses for its health-probe
        queries.

        Returns ``None`` when the query matches no row.
        """
        conn = getattr(self._hot_store, "conn", None)
        if conn is None:
            return None
        lock = getattr(self._hot_store, "_lock", None)
        if lock is None or not hasattr(lock, "__aenter__"):
            # No async lock to take: read without one. The substrate's
            # HotStore always has one, so this is a degraded path for
            # an exotic store, not the normal one.
            return conn.execute(sql, params).fetchone()
        async with lock:
            return conn.execute(sql, params).fetchone()

    async def _run_loop(self) -> None:
        """xreadgroup loop with exponential-backoff reconnect.

        Cancelled cleanly via ``stop()``. Yields to the event loop on
        empty responses so cancellation propagates promptly when the
        stream is idle.

        The read always asks for NEW messages. An explicit id would
        only replay this consumer's own pending entries -- see
        :data:`NEW_MESSAGES` -- so a restart would deliver nothing.

        The reclaim sweep runs on BOTH paths -- with a message and on an
        idle read. It has to: a message whose indexing failed is pending,
        and ``>`` never returns a pending entry, so a sweep gated on
        traffic would stop the moment the stream went quiet.
        """
        backoff = DEFAULT_RECONNECT_BACKOFF_SECONDS
        client = self._redis_client
        consumer_name = self._consumer_name or self._auto_consumer_name()
        self._consumer_name = consumer_name

        while self._running:
            try:
                response = await client.xreadgroup(
                    groupname=self._consumer_group,
                    consumername=consumer_name,
                    streams={STREAM_NAME: NEW_MESSAGES},
                    count=10,
                    block=self._xreadgroup_block_ms,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._read_errors += 1
                logger.warning(
                    "akosha.bodai_subscriber: xreadgroup error: %s",
                    exc,
                    exc_info=True,
                )
                await self._sleep_with_cancel(backoff)
                backoff = min(backoff * 2.0, DEFAULT_RECONNECT_BACKOFF_MAX_SECONDS)
                continue

            backoff = DEFAULT_RECONNECT_BACKOFF_SECONDS
            if response:
                await self._process_response(response, client=client)
            else:
                # Yield so cancellation propagates when the stream is
                # idle and the BLOCK timeout returns ``[]`` quickly.
                await asyncio.sleep(0)

            # The sweep runs on the IDLE path too, and that is the whole
            # point. A message whose indexing failed sits in the PEL,
            # and ``XREADGROUP`` with ``>`` never returns a pending
            # entry -- so a sweep gated on traffic is a sweep that
            # stops, and the entry is never retried, never dropped, and
            # the retry cap is never reached. On a stream that has gone
            # quiet for good, "the next message arrives" is never.
            await self._maybe_retry_pending(client)

    async def _maybe_retry_pending(self, client: Any) -> None:
        """Run the reclaim sweep at most once per ``RETRY_CLAIM_MIN_IDLE_MS``.

        ``XAUTOCLAIM`` can only reclaim an entry that has been pending
        for at least the claim idle time, so a sweep more often than
        that cannot reclaim anything new. Running one per idle poll
        would be an extra round trip every ``block_ms`` -- forty a
        minute -- against a shared Redis, for nothing.

        The first sweep runs immediately: ``_last_sweep_at`` starts at
        zero and the monotonic clock does not.
        """
        now = time.monotonic()
        if now - self._last_sweep_at < self._retry_claim_min_idle_ms / 1000:
            return
        self._last_sweep_at = now
        await self._retry_pending(client)

    async def _retry_pending(self, client: Any) -> None:
        """Redeliver this consumer's own pending entries so retries happen.

        ``XREADGROUP`` with ``>`` NEVER returns a message that is already
        pending for this consumer. Verified against Redis 7.4: a
        message read and not ACKed is returned once by ``>`` and never
        again; only ``0`` (or ``XAUTOCLAIM``) brings it back.

        So without this sweep a message whose indexing failed would sit
        in the PEL forever: never retried, never dropped, and the
        retry cap never reached. The runtime has to deliver the retry,
        not the read.

        ``XAUTOCLAIM`` reclaims pending entries -- including this
        consumer's own -- that have been idle for at least the claim
        idle time. That idle time is what spaces the retries: a
        transient outage gets a real second chance rather than an
        instant one.

        Claims are made with THIS consumer's name, so the claimed
        entries end up owned by the consumer that is reading them. Note
        that ``XAUTOCLAIM`` operates over the WHOLE GROUP's pending
        list and transfers ownership: in a deployment with two Akosha
        processes in one group, one process's sweep can claim the
        other's in-flight entry. That is why
        :data:`RETRY_CLAIM_MIN_IDLE_MS` must be longer than the
        per-event processing timeout -- otherwise a peer can take an
        entry out from under a processor that is still working on it.
        """
        if not self._consumer_name:
            return
        try:
            claimed = await client.xautoclaim(
                STREAM_NAME,
                self._consumer_group,
                self._consumer_name,
                min_idle_time=self._retry_claim_min_idle_ms,
                start_id=self._claim_cursor,
                count=RETRY_CLAIM_COUNT,
            )
        except Exception as exc:
            logger.warning(
                "akosha.bodai_subscriber: xautoclaim failed (%s); pending "
                "entries will not be retried this round",
                exc,
            )
            return
        # Carry the cursor forward. Redis returns the id the NEXT sweep
        # should resume from, and the scan only ever examines
        # ``count * 10`` entries of the pending list per call -- an entry
        # skipped for insufficient idle time is EXAMINED, so it consumes
        # that budget even though it is not claimed. Restarting every
        # sweep at "0-0" re-examines the same low-id prefix for ever,
        # and when that prefix is entirely inside the idle window the
        # sweep claims nothing and the entries past it are unreachable.
        # A cursor of "0-0" means the scan reached the end of the list,
        # so the next sweep starts over.
        self._claim_cursor = self._next_cursor(claimed)
        if not claimed:
            return
        entries = claimed[1] if len(claimed) > 1 else []
        if not entries:
            return
        logger.info(
            "akosha.bodai_subscriber: reclaiming %d pending entr%s for retry",
            len(entries),
            "y" if len(entries) == 1 else "ies",
        )
        self._entries_consumed += len(entries)
        await asyncio.gather(
            *(self._process_entry(entry, client=client) for entry in entries),
            return_exceptions=True,
        )

    @staticmethod
    def _auto_consumer_name() -> str:
        """Generate a unique consumer name per process."""
        import uuid

        return f"akosha-{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _next_cursor(claimed: Any) -> str:
        """The id the NEXT ``XAUTOCLAIM`` should resume from.

        Redis replies ``[next_cursor, entries, deleted_ids]``. A cursor
        of ``"0-0"`` means the scan reached the end of the pending list,
        so the next sweep starts over from the beginning -- otherwise
        the low-id entries would never be examined again.
        """
        if not claimed:
            return "0-0"
        cursor = claimed[0]
        if not isinstance(cursor, (str, bytes)):
            return "0-0"
        if isinstance(cursor, bytes):
            cursor = cursor.decode(errors="replace")
        return cursor if cursor and cursor != "0-0" else "0-0"

    async def _sleep_with_cancel(self, seconds: float) -> None:
        """``asyncio.sleep`` that respects ``_running`` for prompt cancellation."""
        try:
            await asyncio.wait_for(self._wait_until_stopped(), timeout=seconds)
        except TimeoutError:
            return

    async def _wait_until_stopped(self) -> None:
        """Block until ``_running`` flips False (cancelled by ``stop()``)."""
        while self._running:
            await asyncio.sleep(0.05)

    async def _process_response(self, response: Any, *, client: Any) -> None:
        """Decode, index, XACK each entry in an ``xreadgroup`` response.

        ``response`` shape from redis-py: ``[[stream_name, [(id, fields), ...]]]``
        — see :func:`_normalize_stream_entry` for the field-decoding
        rules.

        The entries in a batch are processed CONCURRENTLY, not one
        after another. Each entry's indexing is bounded by
        ``per_event_timeout_seconds`` (30s by default), so a serial batch
        of ten entries could hold the read loop for 300 seconds -- and
        with a hung embedding service a backlog would drain at a
        fraction of a message per second. Concurrent processing bounds
        the whole batch by ONE entry's timeout instead of ten.

        The HotStore serialises its own writes behind its lock, so
        concurrent indexing is safe there.
        """
        for stream in response:
            if not isinstance(stream, (list, tuple)) or len(stream) < 2:
                continue
            entries = stream[1]
            if not isinstance(entries, (list, tuple)):
                continue
            if not entries:
                continue
            self._entries_consumed += len(entries)
            await asyncio.gather(
                *(self._process_entry(entry, client=client) for entry in entries),
                return_exceptions=True,
            )

    async def _process_entry(self, entry: Any, *, client: Any) -> None:
        """Decode -> filter -> index -> XACK one Redis-stream entry."""
        normalized = self._normalize_stream_entry(entry)
        if normalized is None:
            return
        message_id, payload = normalized

        envelope = self._decode_envelope(payload)
        if envelope is None:
            # Decode failure: ack so we don't loop forever on a poison
            # message. The WARNING below is what makes a retired producer
            # visible instead of silently dropping every event.
            #
            # This entry COUNTED as a candidate: we could not read the
            # channel, so we cannot rule out that it was one of the
            # indexed ones. Counting it is what lets the health feed
            # tell "the producer is dropping everything" apart from
            # "the producer was never switched on".
            self._entries_candidate += 1
            logger.warning(
                "akosha.bodai_subscriber: failed to decode envelope "
                "(message_id=%s); acking and dropping",
                message_id,
            )
            await self._safe_xack(client, message_id)
            return

        topic = envelope.get("topic", "")
        inner_payload = envelope.get("payload") or {}
        event_id = envelope.get("event_id") or message_id

        # Filter: only the indexed channels become HotStore rows. Other
        # topics (e.g. ``pattern.detected``) flow through and are XACKed
        # but never indexed.
        if topic not in INDEXED_CHANNELS:
            logger.debug(
                "BodaiToolInvocationSubscriber: skipping topic=%s message_id=%s",
                topic,
                message_id,
            )
            await self._safe_xack(client, message_id)
            return

        # This entry is one that SHOULD have landed a row.
        self._entries_candidate += 1

        try:
            indexed = await asyncio.wait_for(
                self._index_envelope(inner_payload, event_id=event_id, topic=topic),
                timeout=self._per_event_timeout_seconds,
            )
        except TimeoutError:
            logger.warning(
                "akosha.bodai_subscriber: indexing timed out after %.1fs "
                "(message_id=%s); not acking for retry",
                self._per_event_timeout_seconds,
                message_id,
            )
            await self._note_index_failure(client, message_id, reason="timeout")
            return
        except Exception as exc:
            if _is_duplicate_key_error(exc):
                # The row is ALREADY stored, so this message's work is
                # DONE. The substrate's INSERT is not idempotent, so a
                # replay raises instead of being a no-op -- and a
                # raised duplicate used to park the message in the PEL
                # for three pointless retries, growing the pending
                # list with rows that were never going to land.
                #
                # The row is present but no NEW row landed, so the
                # "rows that actually landed" counter is not bumped.
                logger.info(
                    "akosha.bodai_subscriber: message_id=%s is already indexed; acking as a replay",
                    message_id,
                )
                await self._update_watermark(message_id)
                await self._safe_xack(client, message_id)
                return
            logger.warning(
                "akosha.bodai_subscriber: indexing failed for message_id=%s: %s",
                message_id,
                exc,
            )
            # Don't ACK on failure so the next xreadgroup re-delivers
            # the same message after a transient outage -- but only for
            # a bounded number of attempts. See MAX_INDEX_RETRIES.
            await self._note_index_failure(client, message_id, reason="error")
            return

        self._index_retry_counts.pop(message_id, None)
        if indexed:
            self._entries_indexed += 1
            await self._update_watermark(message_id)
            await self._safe_xack(client, message_id)
        else:
            # Schema version mismatch or empty content — drop, no
            # watermark bump (we processed it on the wire but didn't
            # add a new HotStore row).
            await self._safe_xack(client, message_id)

    async def _note_index_failure(self, client: Any, message_id: str, *, reason: str) -> None:
        """Count one indexing failure and drop the message once the cap is hit.

        Leaving a message pending is right for a transient failure and
        wrong for a permanent one. A message that is never ACKed stays
        in the pending list, and only the reclaim sweep brings it back
        -- so a message that fails for ever holds its slot until this
        cap is reached, and the pending list grows with every such
        message.

        The count is per message id, and a success clears it, so a
        transient outage does not consume the retry budget of a message
        that later succeeds.
        """
        attempts = self._index_retry_counts.get(message_id, 0) + 1
        self._index_retry_counts[message_id] = attempts
        if attempts < MAX_INDEX_RETRIES:
            logger.warning(
                "akosha.bodai_subscriber: message_id=%s left pending for retry "
                "(attempt %d of %d, reason=%s)",
                message_id,
                attempts,
                MAX_INDEX_RETRIES,
                reason,
            )
            return
        self._index_retry_counts.pop(message_id, None)
        logger.warning(
            "akosha.bodai_subscriber: dropping message_id=%s after %d failed "
            "indexing attempts (reason=%s); it would otherwise stay pending "
            "forever and starve the new messages behind it",
            message_id,
            attempts,
            reason,
        )
        await self._safe_xack(client, message_id)

    @staticmethod
    def _normalize_stream_entry(
        entry: Any,
    ) -> tuple[str, dict[str, Any]] | None:
        """Return ``(message_id, payload_dict)`` for one entry; ``None`` on shape mismatch."""
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            return None
        raw_id, raw_fields = entry[0], entry[1]
        if not isinstance(raw_fields, dict):
            return None
        message_id = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
        payload: dict[str, Any] = {}
        for field_key, field_value in raw_fields.items():
            key_str = field_key.decode() if isinstance(field_key, bytes) else str(field_key)
            value = field_value
            if isinstance(value, (bytes, bytearray)):
                try:
                    value = value.decode("utf-8")
                except UnicodeError:
                    continue
            payload[key_str] = value
        return message_id, payload

    def _decode_envelope(self, message_payload: dict[str, Any]) -> dict[str, Any] | None:
        """Decode the redis-stream payload into an envelope.

        Tolerates two shapes:

        1. ``envelope=<JSON>`` — the canonical oneiric envelope.
        2. The live shape — oneiric's Redis Streams adapter writes the
           channel, the JSON payload and the headers as top-level
           stream fields. A producer using the flat ``enqueue`` path
           writes the body fields directly onto the entry with no
           nested ``payload`` field.

        Returns ``{"topic", "payload", "event_id", "headers"}`` or
        ``None`` on parse failure.

        The two shapes are decoded by two separate helpers. They were
        one function once, and the live shape's two extra cases pushed
        the cyclomatic complexity past the project's threshold
        (ADR-025's 15) — the shape dispatch is not where the
        complexity belongs.

        A third shape — the direct ``{topic, payload_json,
        headers_json}`` triplet — used to be accepted here. Nothing in
        the fleet writes it: the only writer to ``bodai:events`` is
        oneiric's Redis Streams adapter, which writes ``channel`` /
        ``payload`` / ``headers``. It was removed because a branch that
        accepts a shape nobody writes is a branch nobody tests, and the
        end-to-end suite was running on it instead of the real one.
        """
        if not isinstance(message_payload, dict):
            return None

        envelope_blob = message_payload.get("envelope")
        if envelope_blob not in (None, "", b""):
            return self._decode_envelope_blob(envelope_blob)

        channel = message_payload.get("channel")
        if isinstance(channel, str):
            return self._decode_live_shape(message_payload, channel)

        return None

    def _decode_envelope_blob(self, envelope_blob: Any) -> dict[str, Any] | None:
        """Decode shape 1: the canonical ``envelope=`` JSON blob.

        A blob that parses to a non-object is fatal here: the canonical
        envelope is an object by definition, so anything else is a
        malformed message rather than a payload with an empty body.

        The ``payload`` INSIDE a well-formed envelope is coerced to a
        dict, like the other two shapes. A payload that is a string
        would otherwise reach ``_index_envelope``, raise AttributeError
        on ``payload.get(...)``, and never be ACKed -- a poison
        message that is re-delivered on every read.
        """
        try:
            decoded = json.loads(envelope_blob)
        except (ValueError, TypeError) as exc:
            logger.warning(
                "akosha.bodai_subscriber: failed to parse envelope= field: %s",
                exc,
            )
            return None
        if not isinstance(decoded, dict):
            return None
        raw_payload = decoded.get("payload")
        raw_headers = decoded.get("headers")
        headers = raw_headers if isinstance(raw_headers, dict) else {}
        return {
            "topic": str(decoded.get("topic") or decoded.get("event_type") or ""),
            "payload": raw_payload if isinstance(raw_payload, dict) else {},
            "event_id": str(decoded.get("event_id") or headers.get("event_id") or ""),
            "headers": headers,
        }

    def _decode_live_shape(
        self, message_payload: dict[str, Any], channel: str
    ) -> dict[str, Any] | None:
        """Decode shape 2: the live wire shape, nested or flat."""
        live_payload = message_payload.get("payload")
        live_headers = message_payload.get("headers")

        if isinstance(live_payload, (str, bytes, bytearray)):
            payload = self._parse_live_payload(live_payload)
            if payload is None:
                return None
        else:
            # The flat ``enqueue`` path: the body fields sit directly
            # on the entry, with no nested ``payload``.
            payload = {
                key: value
                for key, value in message_payload.items()
                if key not in ("channel", "headers", "payload")
            }

        headers = self._parse_live_headers(live_headers)
        if headers is None:
            return None

        return {
            "topic": channel,
            "payload": payload,
            "event_id": str(headers.get("event_id") or ""),
            "headers": headers,
        }

    @staticmethod
    def _parse_live_payload(raw: Any) -> dict[str, Any] | None:
        """Parse the live ``payload`` field.

        Returns:
            The parsed object, or ``None`` when the field is not
            parseable JSON. A payload that parses to a non-object
            becomes an empty body rather than an error.
        """
        try:
            decoded = json.loads(raw)
        except (ValueError, TypeError) as exc:
            logger.warning("akosha.bodai_subscriber: failed to parse payload field: %s", exc)
            return None
        return decoded if isinstance(decoded, dict) else {}

    @staticmethod
    def _parse_live_headers(raw: Any) -> dict[str, Any] | None:
        """Parse the live ``headers`` field.

        Returns:
            The parsed object, or ``None`` when the field is present
            but not parseable. An absent or blank header field is an
            empty header set, not an error.
        """
        if not isinstance(raw, (str, bytes, bytearray)) or raw in (None, "", b""):
            return {}
        try:
            decoded = json.loads(raw)
        except (ValueError, TypeError) as exc:
            logger.warning("akosha.bodai_subscriber: failed to parse headers field: %s", exc)
            return None
        return decoded if isinstance(decoded, dict) else {}

    async def _index_envelope(
        self, payload: dict[str, Any], *, event_id: str, topic: str = ""
    ) -> bool:
        """Embed ``payload`` and insert a HotRecord; return True on success.

        Returns ``False`` (without raising) when the schema version
        doesn't match or the content is empty — the caller still
        XACKs because the message is structurally fine, just ignored.
        """
        version = payload.get("version")
        if version is not None and version != SUPPORTED_SCHEMA_VERSION:
            return False
        content = self._build_content(payload, topic=topic)
        if not content:
            return False
        embedding_service = get_embedding_service()
        vec = await embedding_service.generate_embedding(content)
        embedding = vec.tolist() if hasattr(vec, "tolist") else list(vec)
        record = HotRecord(
            system_id=SYSTEM_ID_MAHAVISHNU,
            conversation_id=event_id,
            content=content,
            embedding=embedding,
            timestamp=self._parse_timestamp(payload),
            metadata=payload,
        )
        await self._hot_store.insert(record)
        return True

    @staticmethod
    def _build_content(payload: dict[str, Any], topic: str = "") -> str:
        """Render the audit row as a flat string for embedding.

        The websocket path and the hook path are different shapes with
        different field names, so they get different templates. A Claude
        Code hook envelope carries ``event`` (the event NAME) and
        ``harness`` (the surface the tool ran on) — mapping ``event`` onto
        "surface" would label every hook row with the wrong thing, and the
        "websocket tool invocation" prefix would be false for every hook
        row. Both strings are embedded, so the label is part of the
        vector.
        """
        if topic == HOOK_EVENT_CHANNEL or "harness" in payload:
            return (
                f"hook tool invocation: "
                f"event={payload.get('event') or '?'} "
                f"tool={payload.get('tool_name') or payload.get('tool') or '?'} "
                f"surface={payload.get('harness') or payload.get('surface') or '?'} "
                f"session={payload.get('session_id') or '?'}"
            )
        return (
            f"websocket tool invocation: "
            f"tool={payload.get('tool_name') or payload.get('tool') or '?'} "
            f"surface={payload.get('surface') or '?'} "
            f"result={payload.get('result') or payload.get('outcome') or '?'} "
            f"duration_ms={payload.get('duration_ms') or '?'} "
            f"error={payload.get('error', '')!r}"
        )

    @staticmethod
    def _parse_timestamp(payload: dict[str, Any]) -> datetime:
        """Best-effort ISO-8601 parse; falls back to now()."""
        ts = payload.get("timestamp")
        if isinstance(ts, str):
            with contextlib.suppress(ValueError):
                return datetime.fromisoformat(ts)
        return datetime.now(UTC)

    async def _update_watermark(self, message_id: str) -> None:
        """Upsert the watermark row in the HotStore, never moving it back.

        Uses a zero-vector embedding because HNSW index lookup on a
        zero vector is degenerate; ``akosha_search_all_systems`` filters out
        the reserved ``WATERMARK_CONVERSATION_ID`` at query time so the
        row never appears in search results.

        Upsert strategy: DELETE the existing watermark row (if any),
        then INSERT a fresh one. Done at the SQL level rather than via
        a generic HotStore API because we own the schema — the
        watermark is private to this subscriber.

        Two properties the naive version did not have, both of which
        the concurrent batch processing exposed:

        * **Atomic.** The naive version was a DELETE plus an INSERT on
          the same primary key, with the INSERT awaited. Two updates
          could interleave -- DELETE, DELETE, INSERT, INSERT -- into a
          duplicate-key error that left the row holding an arbitrary
          id. The lock serialises the pair.
        * **Monotonic.** The watermark is "how far the pipeline has got",
          so it must hold the HIGHEST message id indexed. A reclaimed
          pending entry carries an OLD id, and a slow consumer can
          finish after a faster one; neither may drag the row back.

        The row is UPDATED rather than deleted-and-reinserted: an
        UPDATE is one statement, so a failure cannot leave the row
        deleted (which would read as "no watermark" and, on a newly
        created consumer group, would restart the stream at "0"). The
        INSERT only happens the first time, when the row does not
        exist yet.
        """
        async with self._watermark_lock:
            current = await self._read_watermark_id()
            if current is not None and not _is_newer(message_id, current):
                return
            try:
                updated = await self._query_row(
                    "UPDATE conversations SET metadata = ?, timestamp = ? "
                    "WHERE conversation_id = ?",
                    [
                        {"last_message_id": message_id},
                        # DuckDB TIMESTAMP holds naive wall-clock
                        # values; a tz-aware value is silently shifted
                        # to the session's local time.
                        datetime.now(UTC).replace(tzinfo=None),
                        WATERMARK_CONVERSATION_ID,
                    ],
                )
                if updated and int(updated[0]) > 0:
                    return
                # No row yet: create it. The zero-vector embedding is
                # deliberate -- HNSW lookup on a zero vector is
                # degenerate, and the row is addressed by primary key.
                record = HotRecord(
                    system_id=SYSTEM_ID_MAHAVISHNU,
                    conversation_id=WATERMARK_CONVERSATION_ID,
                    content=WATERMARK_CONTENT,
                    embedding=[0.0] * self.embedding_dim,
                    timestamp=datetime.now(UTC),
                    metadata={"last_message_id": message_id},
                )
                await self._hot_store.insert(record)
            except Exception as exc:
                logger.warning(
                    "akosha.bodai_subscriber: watermark update failed (message_id=%s): %s",
                    message_id,
                    exc,
                )

    async def _safe_xack(self, client: Any, message_id: str) -> None:
        """XACK the message; log + swallow transport errors."""
        try:
            await client.xack(STREAM_NAME, self._consumer_group, message_id)
        except Exception as exc:
            logger.warning(
                "akosha.bodai_subscriber: xack failed for message_id=%s: %s",
                message_id,
                exc,
            )
