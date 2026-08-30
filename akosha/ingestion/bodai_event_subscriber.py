r"""A kosha -> bodai:events Redis Streams subscriber (push mode).

Plan: docs/plans/2026-08-29-push-subscriber.md Phase 2.

Replaces the 5-second Dhara poll loop in
``WebSocketInvocationsSubscriber`` with a Redis Streams subscription.
Mahavishnu's producer (commit 7b2c498c) ``xadd``\s envelopes onto the
``bodai:events`` stream; this subscriber consumes them, filters to
``topic == "websocket_tool_invocation"``, embeds each payload via the
existing ``EmbeddingService``, and inserts a ``HotRecord`` into the
HotStore. Rows whose schema version doesn't match the active
``SUPPORTED_SCHEMA_VERSION`` are silently skipped (forward-compat).

A persistent watermark row in the HotStore carries the
``last_processed_message_id`` so restarts resume from where they left
off rather than re-processing the whole stream. The watermark row uses
``conversation_id="__bodai_subscriber_watermark__"`` (reserved;
``search_all_systems`` and friends must filter it out at query time).

Fail-soft contract:

* Redis missing, ``redis.asyncio`` import failure, or stream unreachable
  -> subscriber stays non-running (``_running=False``); the orchestrator
  (``WebSocketInvocationsSubscriber``) falls back to Dhara polling.
* Per-message decode / embed / insert failure -> log at WARNING, skip the
  message (no XACK on success-path insert only; we XACK after
  ``hot_store.insert`` returns, so a transient HotStore error surfaces
  as a ``WARNING`` and the same message is re-delivered on next
  ``xreadgroup``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from akosha.models import HotRecord
from akosha.processing.embeddings import get_embedding_service

logger = logging.getLogger(__name__)


#: Redis stream name; matches the producer-side constant in
#: ``mahavishnu/core/events/redis_publisher.py``.
STREAM_NAME: str = "bodai:events"

#: The topic this subscriber filters for. Anything else on the stream
#: is XACK'd but not indexed into the HotStore.
TOOL_INVOCATION_TOPIC: str = "websocket_tool_invocation"

#: Forward-compat gate — only rows whose ``payload["version"]`` matches
#: this constant are indexed. Mirror of ``SUPPORTED_SCHEMA_VERSION`` in
#: ``websocket_invocations_subscriber``.
SUPPORTED_SCHEMA_VERSION: str = "1.0.0"

#: Reserved ``conversation_id`` for the watermark row. Distinct from
#: any real Dhara key (``websocket_tool_invocation/v1/*``) so search
#: queries can filter it out trivially.
WATERMARK_CONVERSATION_ID: str = "__bodai_subscriber_watermark__"

#: ``system_id`` used for both indexed rows and the watermark. Mirrors
#: ``SYSTEM_ID_MAHAVISHNU`` in the polling subscriber.
SYSTEM_ID_MAHAVISHNU: str = "mahavishnu"

#: Sentinel watermark row content (search-time filters exclude this).
WATERMARK_CONTENT: str = "__watermark__"

#: Exponential backoff bounds for reconnect-after-error.
DEFAULT_RECONNECT_BACKOFF_SECONDS: float = 1.0
DEFAULT_RECONNECT_BACKOFF_MAX_SECONDS: float = 30.0


def _create_redis_client(redis_url: str) -> Any | None:
    """Return an async redis client or ``None`` when redis.asyncio is unavailable.

    Mirrors the lazy-creation pattern in
    ``mahavishnu/core/events/bodai_subscriber.py:_create_redis_client``:
    when ``redis.asyncio`` is not installed, ``None`` signals the
    caller to idle until cancellation so the orchestrator can fall back
    to Dhara polling.
    """
    try:
        import redis.asyncio as aioredis  # type: ignore[import-not-found]
    except ImportError:
        logger.warning(
            "akosha.bodai_subscriber: redis.asyncio is not installed; "
            "subscriber will idle and the orchestrator will fall back to polling",
        )
        return None
    return aioredis.from_url(redis_url, decode_responses=False)


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
        self._xreadgroup_block_ms = int(xreadgroup_block_ms)
        self._per_event_timeout_seconds = float(per_event_timeout_seconds)
        self._consumer_name = consumer_name
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._redis_client: Any = None

    @property
    def running(self) -> bool:
        """Whether the read loop is currently active.

        Orchestrators read this to decide whether to skip the Dhara
        poll loop. ``False`` means the subscriber idled (redis missing
        or unreachable) and the orchestrator should fall back.
        """
        return self._running

    @property
    def embedding_dim(self) -> int:
        """Embedding dim of the HotStore; used for the zero-vector watermark."""
        return getattr(self._hot_store, "_embedding_dim", 384)

    async def start(self) -> None:
        """Connect, ensure consumer group, resume from watermark, spawn loop.

        No-op when already running or when ``hot_store is None``. When
        the redis client cannot be constructed (import failure or
        unreachable broker) the subscriber logs at WARNING and stays
        non-running; the orchestrator's ``running`` check then falls
        back to Dhara polling.
        """
        if self._running:
            return
        if self._hot_store is None:
            logger.debug("BodaiToolInvocationSubscriber: no hot_store, skipping start")
            return

        client = _create_redis_client(self._redis_url)
        if client is None:
            # Import failure: _create_redis_client already logged.
            logger.warning(
                "akosha.bodai_subscriber.fallback_to_poll: redis client is None "
                "(import failure or unreachable broker)"
            )
            return

        # Persist the client; on any of the following failures, close
        # it and leave _running=False so the orchestrator falls back.
        try:
            await self._ensure_consumer_group(client)
        except Exception as exc:
            logger.warning(
                "akosha.bodai_subscriber.fallback_to_poll: xgroup_create failed: %s",
                exc,
            )
            with contextlib.suppress(Exception):
                await client.aclose()
            return

        # Resolve the resume id BEFORE the loop starts so we don't have
        # to deal with nested-asyncio gymnastics from inside the loop.
        try:
            resume_id = await self._resume_id_async()
        except Exception as exc:
            logger.warning("akosha.bodai_subscriber: resume id resolution failed: %s", exc)
            resume_id = ">"

        self._redis_client = client
        self._running = True
        self._task = asyncio.create_task(self._run_loop(resume_id))
        logger.info(
            "BodaiToolInvocationSubscriber started "
            "(stream=%s, group=%s, block_ms=%d, timeout=%.1fs, resume_id=%s)",
            STREAM_NAME,
            self._consumer_group,
            self._xreadgroup_block_ms,
            self._per_event_timeout_seconds,
            resume_id,
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
        if self._redis_client is not None:
            try:
                await self._redis_client.aclose()
            except Exception:
                with contextlib.suppress(Exception):
                    await self._redis_client.close()
            self._redis_client = None
        logger.info("BodaiToolInvocationSubscriber stopped")

    async def _ensure_consumer_group(self, client: Any) -> None:
        """Idempotently create the consumer group on ``bodai:events``.

        Tolerates ``BUSYGROUP`` so re-running with an existing group is
        a no-op. When the stream doesn't exist yet, Redis Streams
        auto-creates it on the first ``xadd`` — so we don't need to
        ``xadd`` a sentinel first.
        """
        try:
            await client.xgroup_create(
                name=STREAM_NAME,
                groupname=self._consumer_group,
                id="0",
                mkstream=True,
            )
            logger.info(
                "Created consumer group stream=%s group=%s",
                STREAM_NAME,
                self._consumer_group,
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

    def _resume_id(self) -> str:
        """Resolve the ``xreadgroup`` resume id from the watermark row.

        Returns ``watermark + 1`` when a watermark row is present (so
        we don't re-process the same message), else ``">"`` (only new
        messages, not the historical back-catalog).
        """
        watermark = self._read_watermark()
        if watermark is None:
            return ">"
        # Stream ids are ``<ms>-<seq>``; bumping the seq component by 1
        # is enough to skip the last-processed id (Redis Streams id
        # monotonicity is enforced within a single ms anyway).
        try:
            ms_str, seq_str = watermark.rsplit("-", 1)
            return f"{ms_str}-{int(seq_str) + 1}"
        except ValueError, AttributeError:
            # Malformed watermark (shouldn't happen — we wrote it).
            # Be safe and resume from ">" rather than corrupt history.
            logger.warning(
                "BodaiToolInvocationSubscriber: malformed watermark %r; resuming from '>'",
                watermark,
            )
            return ">"

    def _read_watermark(self) -> str | None:
        """Synchronous shim — kept for backward compatibility.

        Real code paths always go through ``_read_watermark_async`` from
        inside the running loop. This shim returns ``None`` so any
        accidental sync caller fails open (treats the case as "no
        watermark row").
        """
        return None

    async def _read_watermark_async(self) -> str | None:
        """Async watermark read used from inside the event loop."""
        if self._hot_store is None:
            return None
        try:
            results = await self._hot_store.search_similar(
                query_embedding=[0.0] * self.embedding_dim,
                system_id=SYSTEM_ID_MAHAVISHNU,
                limit=50,
            )
        except Exception as exc:
            logger.warning("BodaiToolInvocationSubscriber: watermark read failed: %s", exc)
            return None
        for row in results:
            if not isinstance(row, dict):
                continue
            if row.get("conversation_id") != WATERMARK_CONVERSATION_ID:
                continue
            meta = row.get("metadata")
            # DuckDB returns JSON columns as strings; ``search_similar``
            # passes the value through verbatim, so accept either form.
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except ValueError, TypeError:
                    meta = None
            if not isinstance(meta, dict):
                continue
            last_id = meta.get("last_message_id")
            if isinstance(last_id, (str, bytes, bytearray)):
                return last_id.decode() if isinstance(last_id, bytes) else last_id
        return None

    async def _run_loop(self, start_id: str = ">") -> None:
        """xreadgroup loop with exponential-backoff reconnect.

        Cancelled cleanly via ``stop()``. Yields to the event loop on
        empty responses so cancellation propagates promptly when the
        stream is idle.
        """
        backoff = DEFAULT_RECONNECT_BACKOFF_SECONDS
        client = self._redis_client

        while self._running:
            try:
                response = await client.xreadgroup(
                    groupname=self._consumer_group,
                    consumername=self._consumer_name or self._auto_consumer_name(),
                    streams={STREAM_NAME: start_id},
                    count=10,
                    block=self._xreadgroup_block_ms,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "akosha.bodai_subscriber: xreadgroup error: %s",
                    exc,
                    exc_info=True,
                )
                await self._sleep_with_cancel(backoff)
                backoff = min(backoff * 2.0, DEFAULT_RECONNECT_BACKOFF_MAX_SECONDS)
                continue

            backoff = DEFAULT_RECONNECT_BACKOFF_SECONDS
            if not response:
                # Yield so cancellation propagates when the stream is
                # idle and the BLOCK timeout returns ``[]`` quickly.
                await asyncio.sleep(0)
                continue

            await self._process_response(response, client=client)

    async def _resume_id_async(self) -> str:
        """Resolve resume id from inside the loop (uses async watermark read)."""
        watermark = await self._read_watermark_async()
        if watermark is None:
            return ">"
        try:
            ms_str, seq_str = watermark.rsplit("-", 1)
            return f"{ms_str}-{int(seq_str) + 1}"
        except ValueError, AttributeError:
            logger.warning(
                "BodaiToolInvocationSubscriber: malformed watermark %r; resuming from '>'",
                watermark,
            )
            return ">"

    @staticmethod
    def _auto_consumer_name() -> str:
        """Generate a unique consumer name per process."""
        import uuid

        return f"akosha-{uuid.uuid4().hex[:12]}"

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
        """
        for stream in response:
            if not isinstance(stream, (list, tuple)) or len(stream) < 2:
                continue
            entries = stream[1]
            if not isinstance(entries, (list, tuple)):
                continue
            for entry in entries:
                await self._process_entry(entry, client=client)

    async def _process_entry(self, entry: Any, *, client: Any) -> None:
        """Decode -> filter -> index -> XACK one Redis-stream entry."""
        normalized = self._normalize_stream_entry(entry)
        if normalized is None:
            return
        message_id, payload = normalized

        envelope = self._decode_envelope(payload)
        if envelope is None:
            # Decode failure: ack so we don't loop forever on a poison
            # message; the WARNING already surfaced the cause.
            await self._safe_xack(client, message_id)
            return

        topic = envelope.get("topic", "")
        inner_payload = envelope.get("payload") or {}
        event_id = envelope.get("event_id") or message_id

        # Filter: only ``websocket_tool_invocation`` is indexed. Other
        # topics (e.g. ``pattern.detected``) flow through and are XACKed
        # but never make it to HotStore.
        if topic != TOOL_INVOCATION_TOPIC:
            logger.debug(
                "BodaiToolInvocationSubscriber: skipping topic=%s message_id=%s",
                topic,
                message_id,
            )
            await self._safe_xack(client, message_id)
            return

        try:
            indexed = await asyncio.wait_for(
                self._index_envelope(inner_payload, event_id=event_id),
                timeout=self._per_event_timeout_seconds,
            )
        except TimeoutError:
            logger.warning(
                "akosha.bodai_subscriber: indexing timed out after %.1fs "
                "(message_id=%s); not acking for retry",
                self._per_event_timeout_seconds,
                message_id,
            )
            return
        except Exception as exc:
            logger.warning(
                "akosha.bodai_subscriber: indexing failed for message_id=%s: %s",
                message_id,
                exc,
            )
            # Don't ACK on failure so the next xreadgroup re-delivers
            # the same message after a transient outage.
            return

        if indexed:
            await self._update_watermark(message_id)
            await self._safe_xack(client, message_id)
        else:
            # Schema version mismatch or empty content — drop, no
            # watermark bump (we processed it on the wire but didn't
            # add a new HotStore row).
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

        Tolerates three shapes (Phase 1 actual shape is the second):

        1. ``envelope=<JSON>`` — canonical oneiric envelope.
        2. Direct ``{topic, payload_json, headers_json}`` triplet — the
           ``RedisEventStreamPublisher`` wire shape.
        3. Legacy fallback — out of scope for this plan.

        Returns ``{"topic", "payload", "event_id", "headers"}`` or
        ``None`` on parse failure.
        """
        if not isinstance(message_payload, dict):
            return None

        envelope_blob = message_payload.get("envelope")
        if envelope_blob not in (None, "", b""):
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
            return {
                "topic": str(decoded.get("topic") or decoded.get("event_type") or ""),
                "payload": decoded.get("payload") or {},
                "event_id": str(
                    decoded.get("event_id") or (decoded.get("headers") or {}).get("event_id") or ""
                ),
                "headers": decoded.get("headers") or {},
            }

        # Direct triplet (Phase 1 actual shape).
        topic = message_payload.get("topic")
        payload_json = message_payload.get("payload_json")
        headers_json = message_payload.get("headers_json")
        event_id = message_payload.get("event_id")
        source = message_payload.get("source")
        if not isinstance(topic, str) or not isinstance(payload_json, (str, bytes)):
            return None
        try:
            payload = json.loads(payload_json)
            headers = json.loads(headers_json) if headers_json not in (None, "", b"") else {}
        except (ValueError, TypeError) as exc:
            logger.warning("akosha.bodai_subscriber: failed to parse triplet: %s", exc)
            return None
        if not isinstance(payload, dict):
            payload = {}
        if not isinstance(headers, dict):
            headers = {}
        return {
            "topic": topic,
            "payload": payload,
            "event_id": str(event_id or headers.get("event_id") or ""),
            "headers": headers,
            "source": source,
        }

    async def _index_envelope(self, payload: dict[str, Any], *, event_id: str) -> bool:
        """Embed ``payload`` and insert a HotRecord; return True on success.

        Returns ``False`` (without raising) when the schema version
        doesn't match or the content is empty — the caller still
        XACKs because the message is structurally fine, just ignored.
        """
        if payload.get("version") != SUPPORTED_SCHEMA_VERSION:
            return False
        content = self._build_content(payload)
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
    def _build_content(payload: dict[str, Any]) -> str:
        """Render the audit row as a flat string for embedding.

        Mirror of ``WebSocketInvocationsSubscriber._build_content`` —
        keeping the two paths byte-identical ensures a single embedding
        service produces the same vector whether the row arrived via
        poll or push.
        """
        return (
            f"websocket tool invocation: "
            f"tool={payload.get('tool', '?')} "
            f"surface={payload.get('surface', '?')} "
            f"result={payload.get('result', '?')} "
            f"duration_ms={payload.get('duration_ms', '?')} "
            f"error={payload.get('error', '')!r}"
        )

    @staticmethod
    def _parse_timestamp(payload: dict[str, Any]) -> datetime:
        """Best-effort ISO-8601 parse; falls back to now()."""
        ts = payload.get("timestamp")
        if isinstance(ts, str):
            try:
                return datetime.fromisoformat(ts)
            except ValueError:
                pass
        return datetime.now(UTC)

    async def _update_watermark(self, message_id: str) -> None:
        """Insert/upsert the watermark row in the HotStore.

        Uses a zero-vector embedding because HNSW index lookup on a
        zero vector is degenerate; ``search_all_systems`` filters out
        the reserved ``WATERMARK_CONVERSATION_ID`` at query time so the
        row never appears in search results.

        Upsert strategy: DELETE the existing watermark row (if any),
        then INSERT a fresh one. Done at the SQL level rather than via
        a generic HotStore API because we own the schema — the
        watermark is private to this subscriber.
        """
        try:
            # Use the duckdb connection directly for the delete+insert;
            # the public ``insert()`` would fail on the unique-key
            # constraint. We don't want to widen ``HotStore``'s surface
            # with an upsert for one private row.
            conn = getattr(self._hot_store, "conn", None)
            if conn is None:
                raise RuntimeError("HotStore.conn unavailable for watermark update")
            conn.execute(
                "DELETE FROM conversations WHERE conversation_id = ?",
                [WATERMARK_CONVERSATION_ID],
            )
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
