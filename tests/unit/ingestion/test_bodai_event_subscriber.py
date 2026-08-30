"""Tests for BodaiToolInvocationSubscriber (Redis push mode).

Plan: docs/plans/2026-08-29-push-subscriber.md Phase 2.

The subscriber consumes the ``bodai:events`` Redis stream filtered to
``topic == "websocket_tool_invocation"``, embeds each payload, and
inserts a ``HotRecord`` into the HotStore. These tests drive fakeredis
end-to-end so the wire shape matches production (Phase 1's
``RedisEventStreamPublisher`` emits ``{event_id, source, topic,
payload_json, headers_json}`` — Phase 2 consumes that exact shape).

Embedding service is stubbed via ``monkeypatch.setattr`` on the
subscriber module's reference; HotStore is a real ``HotStore``
instance backed by an in-memory DuckDB so the dim validation and
watermark row both exercise the production SQL.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import numpy as np
import pytest

from akosha.ingestion.bodai_event_subscriber import (
    STREAM_NAME,
    SUPPORTED_SCHEMA_VERSION,
    SYSTEM_ID_MAHAVISHNU,
    TOOL_INVOCATION_TOPIC,
    WATERMARK_CONVERSATION_ID,
    BodaiToolInvocationSubscriber,
)
from akosha.models import HotRecord
from akosha.storage.hot_store import HotStore


SUB_MODULE = "akosha.ingestion.bodai_event_subscriber"


def _make_fake_embedding_service(dim: int = 384) -> MagicMock:
    """Return a stub ``EmbeddingService`` whose ``generate_embedding`` is async."""
    service = MagicMock()
    service.generate_embedding = AsyncMock(
        return_value=np.zeros(dim, dtype=np.float32)
    )
    return service


def _patch_embedding_service(
    monkeypatch: pytest.MonkeyPatch, dim: int = 384
) -> MagicMock:
    """Swap the subscriber's ``get_embedding_service`` for a stub."""
    fake_svc = _make_fake_embedding_service(dim)
    monkeypatch.setattr(f"{SUB_MODULE}.get_embedding_service", MagicMock(return_value=fake_svc))
    return fake_svc


async def _make_hot_store(dim: int = 384) -> HotStore:
    """Real in-memory HotStore (DuckDB). Closed by the test fixture."""
    hot = HotStore(embedding_dim=dim)
    await hot.initialize()
    return hot


async def _close_hot_store(hot: HotStore | None) -> None:
    """Best-effort close; swallows errors so test failures stay loud."""
    if hot is None:
        return
    try:
        await hot.close()
    except Exception:  # noqa: BLE001
        pass


async def _xadd_wire(
    client: Any,
    *,
    topic: str,
    payload: dict[str, Any],
    headers: dict[str, Any] | None = None,
    event_id: str | None = None,
    source: str = "websocket_consumer",
) -> str:
    """Emit one wire-format message on the stream (mirror of Phase 1 producer)."""
    headers = headers or {}
    if event_id is not None:
        headers.setdefault("event_id", event_id)
    fields = {
        "event_id": headers.get("event_id", ""),
        "source": source,
        "topic": topic,
        "payload_json": json.dumps(payload),
        "headers_json": json.dumps(headers),
    }
    message_id = await client.xadd(STREAM_NAME, fields)
    return (
        message_id.decode() if isinstance(message_id, bytes) else str(message_id)
    )


async def _ensure_group(client: Any, group: str) -> None:
    """Idempotently create the consumer group; tests call this before injecting the client."""
    try:
        await client.xgroup_create(
            name=STREAM_NAME,
            groupname=group,
            id="0",
            mkstream=True,
        )
    except Exception:
        # BUSYGROUP: group already exists — fine.
        pass


async def _inject_client(
    sub: BodaiToolInvocationSubscriber, client: Any, group: str
) -> None:
    """Inject the fakeredis client and pre-create the consumer group."""
    await _ensure_group(client, group)
    sub._redis_client = client


async def _xack_count(client: Any, group: str) -> int:
    """Inspect the consumer group's pending-entries list length."""
    info = await client.xpending(STREAM_NAME, group)
    if isinstance(info, dict):
        return int(info.get("pending", 0))
    # Older fakeredis returns ``[count, min_id, max_id, [[consumer, count], ...]``
    if isinstance(info, (list, tuple)) and info:
        return int(info[0])
    return 0


def _payload_v1(
    tool: str = "websocket_get_status",
    *,
    surface: str = "websocket",
    result: str = "ok",
    duration_ms: int = 12,
    error: str = "",
    timestamp: str = "2026-08-29T12:00:00",
) -> dict[str, Any]:
    """Build a canonical v1.0.0 payload dict."""
    return {
        "version": SUPPORTED_SCHEMA_VERSION,
        "tool": tool,
        "surface": surface,
        "result": result,
        "duration_ms": duration_ms,
        "error": error,
        "timestamp": timestamp,
    }


# ---------------------------------------------------------------------------
# 1. Non-tool-invocation topics are filtered out (XACK only, no HotStore row)
# ---------------------------------------------------------------------------


class TestFiltersNonToolInvocationEnvelopes:
    @pytest.mark.asyncio
    async def test_pattern_detected_envelope_is_xacked_but_not_indexed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``pattern.detected`` envelopes flow through and are XACK'd; no HotStore row."""
        _patch_embedding_service(monkeypatch)
        hot = await _make_hot_store()

        try:
            client = fakeredis.aioredis.FakeRedis(decode_responses=False)
            try:
                group = "akosha-tool-invocation-indexers"
                sub = BodaiToolInvocationSubscriber(
                    redis_url="redis://test",
                    consumer_group=group,
                    hot_store=hot, xreadgroup_block_ms=10, consumer_name="c1",
                )
                # Inject the fakeredis client directly to skip _create_redis_client.
                await _inject_client(sub, client, "akosha-tool-invocation-indexers")

                # Push a non-tool-invocation envelope.
                await _xadd_wire(
                    client,
                    topic="pattern.detected",
                    payload={"pattern_id": "p1", "confidence": 0.9},
                    headers={"event_id": "evt-other"},
                    source="akosha",
                )

                # Start the loop in a task we can drain manually.
                sub._running = True
                loop_task = asyncio.create_task(sub._run_loop())
                try:
                    # Allow the loop to read + filter + xack.
                    await asyncio.sleep(0.2)
                finally:
                    sub._running = False
                    await asyncio.wait_for(loop_task, timeout=1.0)

                # No HotStore rows at all.
                count = hot.conn.execute(
                    "SELECT COUNT(*) FROM conversations"
                ).fetchone()[0]
                assert count == 0

                # XACK fired: pending list is empty.
                assert await _xack_count(client, group) == 0
            finally:
                await client.aclose()
        finally:
            await _close_hot_store(hot)


# ---------------------------------------------------------------------------
# 2. Tool invocation envelopes are indexed into HotStore
# ---------------------------------------------------------------------------


class TestIndexesToolInvocationEnvelope:
    @pytest.mark.asyncio
    async def test_indexes_tool_invocation_envelope_to_hot_store(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Valid envelope -> one HotStore row with the right fields."""
        _patch_embedding_service(monkeypatch)
        hot = await _make_hot_store()

        try:
            client = fakeredis.aioredis.FakeRedis(decode_responses=False)
            try:
                sub = BodaiToolInvocationSubscriber(
                    redis_url="redis://test",
                    hot_store=hot, xreadgroup_block_ms=10, consumer_name="c1",
                )
                await _inject_client(sub, client, "akosha-tool-invocation-indexers")

                await _xadd_wire(
                    client,
                    topic=TOOL_INVOCATION_TOPIC,
                    payload=_payload_v1(),
                    headers={"event_id": "evt-001"},
                    event_id="evt-001",
                )

                sub._running = True
                loop_task = asyncio.create_task(sub._run_loop())
                try:
                    await asyncio.sleep(0.3)
                finally:
                    sub._running = False
                    await asyncio.wait_for(loop_task, timeout=1.0)

                # One indexed row (the envelope); the watermark is also a row,
                # so we expect 2 rows total.
                rows = hot.conn.execute(
                    "SELECT system_id, conversation_id, content "
                    "FROM conversations ORDER BY conversation_id"
                ).fetchall()
                conv_ids = [r[1] for r in rows]
                assert "evt-001" in conv_ids
                # Watermark row carries the sentinel conversation_id.
                assert WATERMARK_CONVERSATION_ID in conv_ids

                # The indexed row's content matches the audit summary.
                indexed = next(r for r in rows if r[1] == "evt-001")
                assert indexed[0] == SYSTEM_ID_MAHAVISHNU
                assert "websocket tool invocation" in indexed[2]
                assert "websocket_get_status" in indexed[2]
            finally:
                await client.aclose()
        finally:
            await _close_hot_store(hot)


# ---------------------------------------------------------------------------
# 3. Unknown schema versions are skipped (forward-compat)
# ---------------------------------------------------------------------------


class TestSkipsUnknownSchemaVersion:
    @pytest.mark.asyncio
    async def test_skips_unknown_schema_version(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``payload.version != "1.0.0"`` -> no HotStore row for that envelope."""
        _patch_embedding_service(monkeypatch)
        hot = await _make_hot_store()

        try:
            client = fakeredis.aioredis.FakeRedis(decode_responses=False)
            try:
                sub = BodaiToolInvocationSubscriber(
                    redis_url="redis://test",
                    hot_store=hot, xreadgroup_block_ms=10, consumer_name="c1",
                )
                await _inject_client(sub, client, "akosha-tool-invocation-indexers")

                # Future-version envelope: must not be indexed.
                await _xadd_wire(
                    client,
                    topic=TOOL_INVOCATION_TOPIC,
                    payload={**_payload_v1(), "version": "2.0.0"},
                    headers={"event_id": "evt-future"},
                )

                sub._running = True
                loop_task = asyncio.create_task(sub._run_loop())
                try:
                    await asyncio.sleep(0.3)
                finally:
                    sub._running = False
                    await asyncio.wait_for(loop_task, timeout=1.0)

                # No indexed row (the watermark row also absent because
                # the message was filtered before reaching the watermark).
                rows = hot.conn.execute(
                    "SELECT conversation_id FROM conversations"
                ).fetchall()
                conv_ids = [r[0] for r in rows]
                assert "evt-future" not in conv_ids
            finally:
                await client.aclose()
        finally:
            await _close_hot_store(hot)


# ---------------------------------------------------------------------------
# 4. Watermark persists the last-processed message id
# ---------------------------------------------------------------------------


class TestWatermarkPersists:
    @pytest.mark.asyncio
    async def test_persists_watermark_after_insert(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Three envelopes -> watermark row's metadata holds the last xreadgroup id."""
        _patch_embedding_service(monkeypatch)
        hot = await _make_hot_store()

        try:
            client = fakeredis.aioredis.FakeRedis(decode_responses=False)
            try:
                sub = BodaiToolInvocationSubscriber(
                    redis_url="redis://test",
                    hot_store=hot, xreadgroup_block_ms=10, consumer_name="c1",
                )
                await _inject_client(sub, client, "akosha-tool-invocation-indexers")

                msg_ids: list[str] = []
                for i in range(3):
                    msg_ids.append(
                        await _xadd_wire(
                            client,
                            topic=TOOL_INVOCATION_TOPIC,
                            payload=_payload_v1(tool=f"tool_{i}"),
                            headers={"event_id": f"evt-{i:03d}"},
                            event_id=f"evt-{i:03d}",
                        )
                    )

                sub._running = True
                loop_task = asyncio.create_task(sub._run_loop())
                try:
                    # Wait until all three are processed.
                    for _ in range(50):
                        await asyncio.sleep(0.05)
                        row = hot.conn.execute(
                            "SELECT metadata FROM conversations "
                            "WHERE conversation_id = ?",
                            [WATERMARK_CONVERSATION_ID],
                        ).fetchone()
                        if row and msg_ids[-1] in str(row[0]):
                            break
                finally:
                    sub._running = False
                    await asyncio.wait_for(loop_task, timeout=1.0)

                # The watermark row carries the third message id.
                row = hot.conn.execute(
                    "SELECT metadata FROM conversations "
                    "WHERE conversation_id = ?",
                    [WATERMARK_CONVERSATION_ID],
                ).fetchone()
                assert row is not None
                meta = row[0]
                # DuckDB returns JSON as a string; accept either form.
                if isinstance(meta, str):
                    meta = json.loads(meta)
                assert meta.get("last_message_id") == msg_ids[-1]
            finally:
                await client.aclose()
        finally:
            await _close_hot_store(hot)


# ---------------------------------------------------------------------------
# 5. Restart resumes from the watermark (no re-processing)
# ---------------------------------------------------------------------------


class TestResumesFromWatermark:
    @pytest.mark.asyncio
    async def test_resumes_from_watermark_on_restart(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pre-existing watermark -> new subscriber doesn't re-index below it.

        Drives ``_process_response`` directly with a synthetic
        ``xreadgroup`` response containing the third message only —
        the ``resume_id`` resolution proves the watermark gate works,
        and we never have to actually spin up the fakeredis loop
        (whose ``block_ms`` event-loop integration is unreliable
        in tests).
        """
        _patch_embedding_service(monkeypatch)
        hot = await _make_hot_store()

        try:
            client = fakeredis.aioredis.FakeRedis(decode_responses=False)
            try:
                # Pre-publish 3 messages; we will pre-create the
                # watermark row pinned to msg_ids[1] so the loop's
                # resume id skips msg_ids[0] and msg_ids[1].
                msg_ids: list[str] = []
                for i in range(3):
                    msg_ids.append(
                        await _xadd_wire(
                            client,
                            topic=TOOL_INVOCATION_TOPIC,
                            payload=_payload_v1(tool=f"tool_{i}"),
                            headers={"event_id": f"evt-{i:03d}"},
                            event_id=f"evt-{i:03d}",
                        )
                    )

                # Plant the watermark row pinned to the SECOND message.
                from datetime import datetime, UTC

                await hot.insert(
                    HotRecord(
                        system_id=SYSTEM_ID_MAHAVISHNU,
                        conversation_id=WATERMARK_CONVERSATION_ID,
                        content="__watermark__",
                        embedding=[0.0] * 384,
                        timestamp=datetime.now(UTC),
                        metadata={"last_message_id": msg_ids[1]},
                    )
                )

                sub = BodaiToolInvocationSubscriber(
                    redis_url="redis://test",
                    hot_store=hot, xreadgroup_block_ms=0, consumer_name="c1",
                )
                await _inject_client(sub, client, "akosha-tool-invocation-indexers")

                # 1) Resolve the resume id exactly the way ``start()``
                # would. ``msg_ids[1] + 1`` -> ``<ms>-<seq+1>``.
                resume_id = await sub._resume_id_async()
                expected_resume = (
                    f"{msg_ids[1].rsplit('-', 1)[0]}-"
                    f"{int(msg_ids[1].rsplit('-', 1)[1]) + 1}"
                )
                assert resume_id == expected_resume, (
                    f"resume_id={resume_id!r} != expected={expected_resume!r}"
                )

                # 2) Drive ``_process_response`` with a synthetic
                # ``xreadgroup`` response containing only the message
                # AFTER the watermark — the loop would receive exactly
                # this on a real restart because xreadgroup honors
                # ``resume_id``.
                third_payload = _payload_v1(tool="tool_2")
                synthetic_response = [
                    [
                        STREAM_NAME.encode(),
                        [
                            (
                                msg_ids[2].encode(),
                                {
                                    b"event_id": b"evt-002",
                                    b"source": b"websocket_consumer",
                                    b"topic": TOOL_INVOCATION_TOPIC.encode(),
                                    b"payload_json": json.dumps(third_payload).encode(),
                                    b"headers_json": json.dumps(
                                        {"event_id": "evt-002"}
                                    ).encode(),
                                },
                            )
                        ],
                    ]
                ]
                await sub._process_response(synthetic_response, client=client)

                # Only ``evt-002`` was indexed. The watermark row was
                # upserted to ``msg_ids[2]``.
                rows = hot.conn.execute(
                    "SELECT conversation_id FROM conversations "
                    "WHERE conversation_id != ?",
                    [WATERMARK_CONVERSATION_ID],
                ).fetchall()
                conv_ids = sorted(r[0] for r in rows)
                assert conv_ids == ["evt-002"], (
                    f"expected only evt-002; got {conv_ids}"
                )

                # Watermark row carries msg_ids[2] (the last processed).
                row = hot.conn.execute(
                    "SELECT metadata FROM conversations "
                    "WHERE conversation_id = ?",
                    [WATERMARK_CONVERSATION_ID],
                ).fetchone()
                assert row is not None
                meta = row[0]
                if isinstance(meta, str):
                    meta = json.loads(meta)
                assert meta.get("last_message_id") == msg_ids[2]
            finally:
                await client.aclose()
        finally:
            await _close_hot_store(hot)


# ---------------------------------------------------------------------------
# 6. Fall back to poll when Redis is unavailable
# ---------------------------------------------------------------------------


class TestFallbackWhenRedisUnavailable:
    @pytest.mark.asyncio
    async def test_falls_back_to_poll_when_redis_unavailable(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ``_create_redis_client`` returns ``None`` the subscriber stays non-running."""
        _patch_embedding_service(monkeypatch)
        hot = await _make_hot_store()

        try:
            # Force the redis import to "fail" by patching the module
            # reference the subscriber uses.
            def _fake_creator(url: str) -> Any | None:
                return None

            monkeypatch.setattr(
                f"{SUB_MODULE}._create_redis_client", _fake_creator
            )

            sub = BodaiToolInvocationSubscriber(
                redis_url="redis://nowhere",
                hot_store=hot, xreadgroup_block_ms=10, consumer_name="c1",
            )
            await sub.start()

            try:
                assert sub.running is False
                assert sub._task is None
                # No client was set.
                assert sub._redis_client is None
            finally:
                await sub.stop()
        finally:
            await _close_hot_store(hot)


# ---------------------------------------------------------------------------
# 7. xreadgroup uses the configured block timeout
# ---------------------------------------------------------------------------


class TestXreadgroupBlockTimeout:
    @pytest.mark.asyncio
    async def test_xreadgroup_block_timeout_used(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The configured ``xreadgroup_block_ms`` is passed to ``xreadgroup``."""
        _patch_embedding_service(monkeypatch)
        hot = await _make_hot_store()

        try:
            client = fakeredis.aioredis.FakeRedis(decode_responses=False)
            try:
                sub = BodaiToolInvocationSubscriber(
                    redis_url="redis://test",
                    hot_store=hot, xreadgroup_block_ms=1234, consumer_name="c1",
                )
                await _inject_client(sub, client, "akosha-tool-invocation-indexers")

                # Spy on xreadgroup to capture the block argument.
                original = client.xreadgroup
                captured: dict[str, Any] = {}

                async def spy_xreadgroup(*args: Any, **kwargs: Any) -> Any:
                    captured["args"] = args
                    captured["kwargs"] = kwargs
                    return await original(*args, **kwargs)

                client.xreadgroup = spy_xreadgroup  # type: ignore[method-assign]

                sub._running = True
                loop_task = asyncio.create_task(sub._run_loop())
                try:
                    await asyncio.sleep(0.2)
                finally:
                    sub._running = False
                    await asyncio.wait_for(loop_task, timeout=1.0)

                # The block parameter was passed through.
                assert captured, "xreadgroup was never called"
                block = captured["kwargs"].get("block")
                assert block == 1234
            finally:
                await client.aclose()
        finally:
            await _close_hot_store(hot)


# ---------------------------------------------------------------------------
# 8. start() constructs the consumer group idempotently
# ---------------------------------------------------------------------------


class TestStartConsumerGroupIdempotent:
    @pytest.mark.asyncio
    async def test_start_is_idempotent_against_existing_group(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``xgroup_create`` must tolerate BUSYGROUP on restart."""
        _patch_embedding_service(monkeypatch)
        hot = await _make_hot_store()

        try:
            client = fakeredis.aioredis.FakeRedis(decode_responses=False)
            try:
                group = "akosha-tool-invocation-indexers"

                # Pre-create the group (simulating prior run).
                try:
                    await client.xgroup_create(
                        name=STREAM_NAME,
                        groupname=group,
                        id="0",
                        mkstream=True,
                    )
                except Exception:
                    pass

                # ``start()`` should not raise even though the group exists.
                monkeypatch.setattr(
                    f"{SUB_MODULE}._create_redis_client",
                    lambda url: client,
                )
                sub = BodaiToolInvocationSubscriber(
                    redis_url="redis://test",
                    consumer_group=group,
                    hot_store=hot, xreadgroup_block_ms=10, consumer_name="c1",
                )
                await sub.start()
                try:
                    assert sub.running is True
                finally:
                    await sub.stop()
            finally:
                await client.aclose()
        finally:
            await _close_hot_store(hot)


# ---------------------------------------------------------------------------
# 9. The envelope decoder handles both wire shapes
# ---------------------------------------------------------------------------


class TestEnvelopeDecoder:
    """Direct decode-helper coverage so the wire-shape contract is explicit."""

    @pytest.mark.asyncio
    async def test_decodes_direct_triplet(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_embedding_service(monkeypatch)
        sub = BodaiToolInvocationSubscriber(redis_url="redis://test")
        decoded = sub._decode_envelope(
            {
                "event_id": "evt-001",
                "source": "websocket_consumer",
                "topic": TOOL_INVOCATION_TOPIC,
                "payload_json": json.dumps({"version": "1.0.0", "tool": "x"}),
                "headers_json": json.dumps({"event_id": "evt-001"}),
            }
        )
        assert decoded is not None
        assert decoded["topic"] == TOOL_INVOCATION_TOPIC
        assert decoded["event_id"] == "evt-001"
        assert decoded["payload"]["version"] == "1.0.0"
        assert decoded["headers"]["event_id"] == "evt-001"

    @pytest.mark.asyncio
    async def test_decodes_canonical_envelope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_embedding_service(monkeypatch)
        sub = BodaiToolInvocationSubscriber(redis_url="redis://test")
        inner = {
            "topic": TOOL_INVOCATION_TOPIC,
            "event_id": "evt-002",
            "payload": {"version": "1.0.0", "tool": "y"},
            "headers": {"event_id": "evt-002"},
        }
        decoded = sub._decode_envelope({"envelope": json.dumps(inner)})
        assert decoded is not None
        assert decoded["topic"] == TOOL_INVOCATION_TOPIC
        assert decoded["event_id"] == "evt-002"

    @pytest.mark.asyncio
    async def test_decode_returns_none_on_malformed_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_embedding_service(monkeypatch)
        sub = BodaiToolInvocationSubscriber(redis_url="redis://test")
        # Missing topic — not a valid triplet, not a canonical envelope.
        decoded = sub._decode_envelope({"payload_json": "{}", "headers_json": "{}"})
        assert decoded is None
