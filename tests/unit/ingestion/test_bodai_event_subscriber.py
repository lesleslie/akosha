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
import inspect
import json
import logging
from pathlib import Path
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
    """Return a stub ``EmbeddingService`` whose ``akosha_generate_embedding`` is async."""
    service = MagicMock()
    service.generate_embedding = AsyncMock(return_value=np.zeros(dim, dtype=np.float32))
    return service


def _patch_embedding_service(monkeypatch: pytest.MonkeyPatch, dim: int = 384) -> MagicMock:
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
) -> str:
    """Emit one message on the stream in the LIVE producer's shape.

    ``oneiric/adapters/queue/redis_streams.py:publish`` writes exactly
    three fields: ``channel`` (the bus topic), ``payload`` (the
    JSON-encoded body) and ``headers`` (the JSON-encoded headers).
    Nothing in the fleet writes the older ``{topic, payload_json,
    headers_json}`` triplet, so the end-to-end tests exercise the shape
    that is actually on the wire.
    """
    headers = headers or {}
    if event_id is not None:
        headers.setdefault("event_id", event_id)
    fields = {
        "channel": topic,
        "payload": json.dumps(payload),
        "headers": json.dumps(headers),
    }
    message_id = await client.xadd(STREAM_NAME, fields)
    return message_id.decode() if isinstance(message_id, bytes) else str(message_id)


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


async def _inject_client(sub: BodaiToolInvocationSubscriber, client: Any, group: str) -> None:
    """Inject the fakeredis client and pre-create the consumer group."""
    await _ensure_group(client, group)
    sub._redis_client = client


async def _wait_for(condition: Any, *, timeout: float = 5.0) -> bool:
    """Poll a condition until it is truthy; ``False`` on timeout.

    The condition may be a plain callable or an async one. Polling the
    observable condition is what makes these tests independent of how
    loaded the machine is: a fixed sleep asserts that the read loop
    finished inside N milliseconds, which is a statement about the
    runner, not about the code.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        result = condition()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return True
        await asyncio.sleep(0.02)
    return False


async def _wait_until_processed(
    sub: Any, client: Any, group: str, *, timeout: float = 5.0
) -> bool:
    """Wait until the subscriber has read the entry and it has been acked.

    Both halves matter: the pending count is ZERO before the entry is
    read, so waiting on it alone would return before the loop had done
    anything.
    """

    async def _done() -> bool:
        return sub.entries_consumed >= 1 and await _xack_count(client, group) == 0

    return await _wait_for(_done, timeout=timeout)


async def _wait_for_event(hot: HotStore, conversation_id: str, *, timeout: float = 5.0) -> bool:
    """Wait until ``conversation_id`` is indexed; ``False`` on timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        row = hot.conn.execute(
            "SELECT 1 FROM conversations WHERE conversation_id = ?",
            [conversation_id],
        ).fetchone()
        if row:
            return True
        await asyncio.sleep(0.02)
    return False


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


class TestTheLiveShapeIsTheOneThatLands:
    @pytest.mark.asyncio
    async def test_the_captured_live_entry_lands_a_row_and_a_watermark(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The captured live entry must land a row AND a watermark.

        This is the branch's headline claim, end to end: the shape
        ``oneiric``'s Redis Streams adapter writes -- ``channel``,
        ``payload``, ``headers`` -- read through the loop, decoded,
        indexed, and recorded as the watermark.

        The fixture is the captured live entry, not a hand-written
        approximation, so the test fails when the producer's shape
        drifts rather than when the test drifts.
        """
        _patch_embedding_service(monkeypatch)
        hot = await _make_hot_store()

        try:
            client = fakeredis.aioredis.FakeRedis(decode_responses=False)
            try:
                fixture = json.loads(
                    (Path(__file__).resolve().parents[2] / "fixtures"
                     / "bodai_events_stream_entry.json").read_text()
                )
                fields = {
                    key: (
                        json.dumps(value)
                        if key in ("payload", "headers")
                        else value
                    )
                    for key, value in fixture.items()
                    if key != "stream_entry_fields"
                }
                message_id = await client.xadd(STREAM_NAME, fields)
                message_id = (
                    message_id.decode() if isinstance(message_id, bytes) else message_id
                )

                group = "akosha-tool-invocation-indexers"
                sub = BodaiToolInvocationSubscriber(
                    redis_url="redis://test",
                    consumer_group=group,
                    hot_store=hot,
                    xreadgroup_block_ms=10,
                    consumer_name="c1",
                )
                await _inject_client(sub, client, group)

                sub._running = True
                loop_task = asyncio.create_task(sub._run_loop())
                try:
                    # The captured entry carries no event_id in its
                    # headers, so the row is keyed by the stream id.
                    assert await _wait_for_event(hot, message_id), (
                        "the live-shaped entry was never indexed"
                    )
                finally:
                    sub._running = False
                    await asyncio.wait_for(loop_task, timeout=1.0)

                # The watermark records the message that landed.
                row = hot.conn.execute(
                    "SELECT metadata FROM conversations WHERE conversation_id = ?",
                    [WATERMARK_CONVERSATION_ID],
                ).fetchone()
                assert row is not None, "no watermark row was written"
                meta = row[0]
                if isinstance(meta, str):
                    meta = json.loads(meta)
                assert meta.get("last_message_id") == message_id
            finally:
                await client.aclose()
        finally:
            await _close_hot_store(hot)

    @pytest.mark.asyncio
    async def test_a_new_group_starts_at_the_watermark(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A group created over an indexed stream must not replay it.

        The watermark's job: when the consumer group does not exist yet,
        it is created AT the watermark, so the messages that were
        already indexed are not delivered again. Passing the watermark
        to the READ instead of to the group creation would break this --
        and an explicit read id cannot deliver a new message at all.
        """
        _patch_embedding_service(monkeypatch)
        hot = await _make_hot_store()

        try:
            client = fakeredis.aioredis.FakeRedis(decode_responses=False)
            try:
                group = "akosha-watermark-start-group"
                older_id = await _xadd_wire(
                    client,
                    topic=TOOL_INVOCATION_TOPIC,
                    payload=_payload_v1(tool="tool_old"),
                    headers={"event_id": "evt-old"},
                    event_id="evt-old",
                )
                newer_id = await _xadd_wire(
                    client,
                    topic=TOOL_INVOCATION_TOPIC,
                    payload=_payload_v1(tool="tool_new"),
                    headers={"event_id": "evt-new"},
                    event_id="evt-new",
                )

                # The watermark says the first message was already
                # indexed.
                from datetime import UTC, datetime

                await hot.insert(
                    HotRecord(
                        system_id=SYSTEM_ID_MAHAVISHNU,
                        conversation_id=WATERMARK_CONVERSATION_ID,
                        content="__watermark__",
                        embedding=[0.0] * 384,
                        timestamp=datetime.now(UTC),
                        metadata={"last_message_id": older_id},
                    )
                )

                sub = BodaiToolInvocationSubscriber(
                    redis_url="redis://test",
                    consumer_group=group,
                    hot_store=hot,
                    xreadgroup_block_ms=10,
                    consumer_name="c1",
                )
                monkeypatch.setattr(
                    f"{SUB_MODULE}._create_redis_client", lambda _url: client
                )
                await sub.start()
                try:
                    assert await _wait_for_event(hot, "evt-new"), (
                        "the message after the watermark was not delivered"
                    )
                finally:
                    await sub.stop()

                # The message BELOW the watermark was not re-indexed.
                rows = hot.conn.execute(
                    "SELECT conversation_id FROM conversations WHERE conversation_id != ?",
                    [WATERMARK_CONVERSATION_ID],
                ).fetchall()
                conv_ids = sorted(r[0] for r in rows)
                assert conv_ids == ["evt-new"], (
                    f"expected only evt-new; got {conv_ids}"
                )
                assert newer_id > older_id
            finally:
                await client.aclose()
        finally:
            await _close_hot_store(hot)

    @pytest.mark.asyncio
    async def test_the_unwritten_triplet_shape_is_not_indexed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The ``{topic, payload_json, headers_json}`` triplet is not a shape.

        Nothing in the fleet writes it: the only writer to
        ``bodai:events`` is ``oneiric``'s Redis Streams adapter, which
        writes ``channel``/``payload``/``headers``. The triplet decoder
        was removed because a branch that accepts a shape nobody writes
        is a branch nobody tests -- and the e2e suite was running on it
        instead of the real one.
        """
        _patch_embedding_service(monkeypatch)
        hot = await _make_hot_store()

        try:
            client = fakeredis.aioredis.FakeRedis(decode_responses=False)
            try:
                await client.xadd(
                    STREAM_NAME,
                    {
                        "topic": TOOL_INVOCATION_TOPIC,
                        "payload_json": json.dumps(_payload_v1()),
                        "headers_json": json.dumps({"event_id": "evt-triplet"}),
                    },
                )
                group = "akosha-tool-invocation-indexers"
                sub = BodaiToolInvocationSubscriber(
                    redis_url="redis://test",
                    consumer_group=group,
                    hot_store=hot,
                    xreadgroup_block_ms=10,
                    consumer_name="c1",
                )
                await _inject_client(sub, client, group)

                sub._running = True
                loop_task = asyncio.create_task(sub._run_loop())
                try:
                    assert await _wait_until_processed(sub, client, group), (
                        "the entry was never read and acked"
                    )
                finally:
                    sub._running = False
                    await asyncio.wait_for(loop_task, timeout=1.0)

                count = hot.conn.execute(
                    "SELECT COUNT(*) FROM conversations"
                ).fetchone()[0]
                assert count == 0, (
                    "the triplet shape was indexed, but no producer writes it"
                )
            finally:
                await client.aclose()
        finally:
            await _close_hot_store(hot)


class TestTheCountersSayWhatOwedARow:
    """The stall signal gates on the CANDIDATE count, not the read count.

    The hook bridge publishes six channels and only one of them is
    indexed. A pipeline that reads nothing but traffic it is meant to
    drop has done no work that could have produced a row, and gating
    on the raw read count would report it as stalled.
    """

    @pytest.mark.asyncio
    async def test_traffic_that_is_not_indexable_owes_no_row(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_embedding_service(monkeypatch)
        hot = await _make_hot_store()

        try:
            client = fakeredis.aioredis.FakeRedis(decode_responses=False)
            try:
                group = "akosha-tool-invocation-indexers"
                sub = BodaiToolInvocationSubscriber(
                    redis_url="redis://test",
                    consumer_group=group,
                    hot_store=hot,
                    xreadgroup_block_ms=10,
                    consumer_name="c1",
                )
                await _inject_client(sub, client, group)
                await _xadd_wire(
                    client,
                    topic="pattern.detected",
                    payload={"pattern_id": "p1"},
                    headers={"event_id": "evt-other"},
                )

                sub._running = True
                loop_task = asyncio.create_task(sub._run_loop())
                try:
                    assert await _wait_for(
                        lambda: sub.entries_consumed == 1
                    ), "the entry was not read"
                finally:
                    sub._running = False
                    await asyncio.wait_for(loop_task, timeout=1.0)

                assert sub.entries_consumed == 1, "the entry was not read"
                assert sub.entries_candidate == 0, (
                    "traffic that is not indexable owes no row, and must "
                    "not arm the stall signal"
                )
                assert sub.entries_indexed == 0
            finally:
                await client.aclose()
        finally:
            await _close_hot_store(hot)

    @pytest.mark.asyncio
    async def test_an_indexed_entry_owes_a_row_and_lands_one(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_embedding_service(monkeypatch)
        hot = await _make_hot_store()

        try:
            client = fakeredis.aioredis.FakeRedis(decode_responses=False)
            try:
                group = "akosha-tool-invocation-indexers"
                sub = BodaiToolInvocationSubscriber(
                    redis_url="redis://test",
                    consumer_group=group,
                    hot_store=hot,
                    xreadgroup_block_ms=10,
                    consumer_name="c1",
                )
                await _inject_client(sub, client, group)
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
                    assert await _wait_for_event(hot, "evt-001")
                finally:
                    sub._running = False
                    await asyncio.wait_for(loop_task, timeout=1.0)

                assert sub.entries_candidate == 1
                assert sub.entries_indexed == 1
            finally:
                await client.aclose()
        finally:
            await _close_hot_store(hot)


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
                    hot_store=hot,
                    xreadgroup_block_ms=10,
                    consumer_name="c1",
                )
                # Inject the fakeredis client directly to skip _create_redis_client.
                group = "akosha-tool-invocation-indexers"
                await _inject_client(sub, client, group)

                # Push a non-tool-invocation envelope.
                await _xadd_wire(
                    client,
                    topic="pattern.detected",
                    payload={"pattern_id": "p1", "confidence": 0.9},
                    headers={"event_id": "evt-other"},
                )

                # Start the loop in a task we can drain manually.
                sub._running = True
                loop_task = asyncio.create_task(sub._run_loop())
                try:
                    # Wait for the loop to read + filter + xack.
                    assert await _wait_until_processed(sub, client, group), (
                        "the entry was never read and acked"
                    )
                finally:
                    sub._running = False
                    await asyncio.wait_for(loop_task, timeout=1.0)

                # No HotStore rows at all.
                count = hot.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
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
                    hot_store=hot,
                    xreadgroup_block_ms=10,
                    consumer_name="c1",
                )
                group = "akosha-tool-invocation-indexers"
                await _inject_client(sub, client, group)

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
                    assert await _wait_until_processed(sub, client, group), (
                        "the entry was never read and acked"
                    )
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
                    hot_store=hot,
                    xreadgroup_block_ms=10,
                    consumer_name="c1",
                )
                group = "akosha-tool-invocation-indexers"
                await _inject_client(sub, client, group)

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
                    assert await _wait_until_processed(sub, client, group), (
                        "the entry was never read and acked"
                    )
                finally:
                    sub._running = False
                    await asyncio.wait_for(loop_task, timeout=1.0)

                # No indexed row (the watermark row also absent because
                # the message was filtered before reaching the watermark).
                rows = hot.conn.execute("SELECT conversation_id FROM conversations").fetchall()
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
                    hot_store=hot,
                    xreadgroup_block_ms=10,
                    consumer_name="c1",
                )
                group = "akosha-tool-invocation-indexers"
                await _inject_client(sub, client, group)

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
                            "SELECT metadata FROM conversations WHERE conversation_id = ?",
                            [WATERMARK_CONVERSATION_ID],
                        ).fetchone()
                        if row and msg_ids[-1] in str(row[0]):
                            break
                finally:
                    sub._running = False
                    await asyncio.wait_for(loop_task, timeout=1.0)

                # The watermark row carries the third message id.
                row = hot.conn.execute(
                    "SELECT metadata FROM conversations WHERE conversation_id = ?",
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
# 5. A restart must still deliver the messages published after it
# ---------------------------------------------------------------------------


class TestARestartStillDeliversNewMessages:
    @pytest.mark.asyncio
    async def test_a_restart_delivers_a_message_published_after_the_restart(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A restarted subscriber must see the messages published after it.

        The resume path used to read with an EXPLICIT id -- the
        watermark + 1. Redis documents the opposite of what that
        assumed::

            Any other ID ... will have the effect of returning entries
            that are pending for the consumer sending the command with
            IDs greater than the one provided. So basically if the ID
            is not '>', then the command will just let the client
            access its pending entries.

        An explicit id can therefore never deliver a brand-new
        message: it only replays the calling consumer's own pending
        entries. Once the first successful index wrote a watermark,
        every restart read with an explicit id and went permanently
        deaf -- the pipeline looked alive and delivered nothing.

        The consumer group's own ``last-delivered-id`` is the durable
        position, so the read asks for NEW messages and the group
        resumes where it left off.
        """
        _patch_embedding_service(monkeypatch)
        hot = await _make_hot_store()

        try:
            server = fakeredis.FakeServer()
            group = "akosha-tool-invocation-indexers"
            first_client = fakeredis.aioredis.FakeRedis(
                server=server, decode_responses=False
            )
            second_client = fakeredis.aioredis.FakeRedis(
                server=server, decode_responses=False
            )
            try:
                # --- first run: index one message, write the watermark
                monkeypatch.setattr(
                    f"{SUB_MODULE}._create_redis_client", lambda _url: first_client
                )
                first = BodaiToolInvocationSubscriber(
                    redis_url="redis://test",
                    consumer_group=group,
                    hot_store=hot,
                    xreadgroup_block_ms=10,
                    consumer_name="c1",
                )
                await _xadd_wire(
                    first_client,
                    topic=TOOL_INVOCATION_TOPIC,
                    payload=_payload_v1(tool="tool_before"),
                    headers={"event_id": "evt-before"},
                    event_id="evt-before",
                )
                await first.start()
                assert await _wait_for_event(hot, "evt-before"), (
                    "the first run never indexed its message"
                )
                await first.stop()

                # The watermark row exists now. This is what used to
                # make the next start read with an explicit id.
                row = hot.conn.execute(
                    "SELECT metadata FROM conversations WHERE conversation_id = ?",
                    [WATERMARK_CONVERSATION_ID],
                ).fetchone()
                assert row is not None, "expected a watermark row after the first run"

                # --- restart: a NEW consumer, a message published after it
                monkeypatch.setattr(
                    f"{SUB_MODULE}._create_redis_client", lambda _url: second_client
                )
                second = BodaiToolInvocationSubscriber(
                    redis_url="redis://test",
                    consumer_group=group,
                    hot_store=hot,
                    xreadgroup_block_ms=10,
                    consumer_name="c2",
                )
                await _xadd_wire(
                    second_client,
                    topic=TOOL_INVOCATION_TOPIC,
                    payload=_payload_v1(tool="tool_after"),
                    headers={"event_id": "evt-after"},
                    event_id="evt-after",
                )
                await second.start()
                try:
                    delivered = await _wait_for_event(hot, "evt-after")
                finally:
                    await second.stop()

                assert delivered, (
                    "the restarted subscriber never delivered the message "
                    "published after the restart"
                )
            finally:
                await first_client.aclose()
                await second_client.aclose()
        finally:
            await _close_hot_store(hot)

    @pytest.mark.asyncio
    async def test_the_read_asks_for_new_messages(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The read must use '>' -- the only form that delivers new messages."""
        _patch_embedding_service(monkeypatch)
        hot = await _make_hot_store()

        try:
            server = fakeredis.FakeServer()
            client = fakeredis.aioredis.FakeRedis(server=server, decode_responses=False)
            try:
                reads: list[Any] = []
                original_xreadgroup = client.xreadgroup

                async def _recording_xreadgroup(*args: Any, **kwargs: Any) -> Any:
                    reads.append(kwargs.get("streams"))
                    return await original_xreadgroup(*args, **kwargs)

                monkeypatch.setattr(client, "xreadgroup", _recording_xreadgroup)

                monkeypatch.setattr(
                    f"{SUB_MODULE}._create_redis_client", lambda _url: client
                )
                sub = BodaiToolInvocationSubscriber(
                    redis_url="redis://test",
                    hot_store=hot,
                    xreadgroup_block_ms=10,
                    consumer_name="c1",
                )
                await sub.start()
                try:
                    await asyncio.sleep(0.1)
                finally:
                    await sub.stop()

                assert reads, "expected the subscriber to read from the stream"
                for streams in reads:
                    assert streams == {STREAM_NAME: ">"}, (
                        f"the read must ask for new messages, got {streams!r}"
                    )
            finally:
                await client.aclose()
        finally:
            await _close_hot_store(hot)


# ---------------------------------------------------------------------------
# 6. The watermark is atomic and monotonic
# ---------------------------------------------------------------------------


class TestTheWatermarkIsAtomicAndMonotonic:
    @pytest.mark.asyncio
    async def test_the_watermark_never_moves_backwards(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A lower message id must not overwrite a higher one."""
        _patch_embedding_service(monkeypatch)
        hot = await _make_hot_store()

        try:
            sub = BodaiToolInvocationSubscriber(
                redis_url="redis://test",
                hot_store=hot,
                consumer_name="c1",
            )
            await sub._update_watermark("1700000000000-9")
            await sub._update_watermark("1700000000000-3")

            row = hot.conn.execute(
                "SELECT metadata FROM conversations WHERE conversation_id = ?",
                [WATERMARK_CONVERSATION_ID],
            ).fetchone()
            assert row is not None
            meta = row[0]
            if isinstance(meta, str):
                meta = json.loads(meta)
            assert meta.get("last_message_id") == "1700000000000-9", (
                "the watermark moved backwards"
            )
        finally:
            await _close_hot_store(hot)

    @pytest.mark.asyncio
    async def test_concurrent_updates_leave_the_highest_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Concurrent updates must not collide or lose the highest id.

        The write is a DELETE plus an INSERT on the same primary key,
        and the INSERT is awaited -- so two updates could interleave
        into a duplicate-key error and leave the row holding an
        arbitrary id.
        """
        _patch_embedding_service(monkeypatch)
        hot = await _make_hot_store()

        try:
            sub = BodaiToolInvocationSubscriber(
                redis_url="redis://test",
                hot_store=hot,
                consumer_name="c1",
            )
            with caplog.at_level(
                logging.WARNING, logger="akosha.ingestion.bodai_event_subscriber"
            ):
                await asyncio.gather(
                    sub._update_watermark("1700000000000-1"),
                    sub._update_watermark("1700000000000-7"),
                    sub._update_watermark("1700000000000-4"),
                )

            assert not [
                rec
                for rec in caplog.records
                if "watermark update failed" in rec.message
            ], "a concurrent watermark update failed"

            row = hot.conn.execute(
                "SELECT metadata FROM conversations WHERE conversation_id = ?",
                [WATERMARK_CONVERSATION_ID],
            ).fetchone()
            assert row is not None
            meta = row[0]
            if isinstance(meta, str):
                meta = json.loads(meta)
            assert meta.get("last_message_id") == "1700000000000-7", (
                "the watermark does not hold the highest message id"
            )
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

            monkeypatch.setattr(f"{SUB_MODULE}._create_redis_client", _fake_creator)

            sub = BodaiToolInvocationSubscriber(
                redis_url="redis://nowhere",
                hot_store=hot,
                xreadgroup_block_ms=10,
                consumer_name="c1",
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
                    hot_store=hot,
                    xreadgroup_block_ms=1234,
                    consumer_name="c1",
                )
                group = "akosha-tool-invocation-indexers"
                await _inject_client(sub, client, group)

                # Spy on xreadgroup to capture the block argument.
                original = client.xreadgroup
                captured: dict[str, Any] = {}

                async def spy_xreadgroup(*args: Any, **kwargs: Any) -> Any:
                    captured["args"] = args
                    captured["kwargs"] = kwargs
                    return await original(*args, **kwargs)

                client.xreadgroup = spy_xreadgroup

                sub._running = True
                loop_task = asyncio.create_task(sub._run_loop())
                try:
                    assert await _wait_for(lambda: bool(captured)), (
                        "xreadgroup was never called"
                    )
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
                    hot_store=hot,
                    xreadgroup_block_ms=10,
                    consumer_name="c1",
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
    async def test_decodes_canonical_envelope(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
