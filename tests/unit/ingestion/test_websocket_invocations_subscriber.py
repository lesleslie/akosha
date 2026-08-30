"""Tests for WebSocketInvocationsSubscriber (Dhara -> HotStore).

Sub-plan B of docs/plans/2026-08-29-akosha-websocket-search.md. The
subscriber polls Dhara's ``websocket_tool_invocation/v1/*`` prefix on
an interval, embeds each payload, and inserts a ``HotRecord`` into the
HotStore. These tests focus on the lifecycle, idempotency, and fail-soft
contract — not on the embedding service internals (those are mocked).

The embedding service is stubbed to return a 384-dim ndarray so the
HotStore's FLOAT[384] schema is honored. In production the singleton at
``akosha.processing.embeddings`` is used directly; here we swap it out
via ``monkeypatch.setattr`` on the subscriber module's reference.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from akosha.models import HotRecord

# Import lazily inside fixtures to avoid bleeding the module-level
# ``akosha.processing.embeddings`` singleton state across tests.
SUB_MODULE = "akosha.ingestion.websocket_invocations_subscriber"


def _make_fake_embedding_service() -> MagicMock:
    """Return an AsyncMock-ready EmbeddingService double.

    ``generate_embedding`` is async and returns a 384-dim ndarray so it
    matches the HotStore's ``FLOAT[384]`` schema. The fallback in the
    real ``EmbeddingService._generate_fallback_embedding`` happens to
    also be 384-dim by default in oneiric, so this shape mirrors that.
    """
    service = MagicMock()
    service.generate_embedding = AsyncMock(
        return_value=np.zeros(384, dtype=np.float32)
    )
    return service


def _make_fake_hot_store() -> MagicMock:
    """Return a mock ``HotStore`` whose ``insert`` is an AsyncMock.

    We only need to assert that ``insert`` was called with the right
    record; we do not exercise the real DuckDB path in unit tests.
    """
    hs = MagicMock()
    hs.insert = AsyncMock(return_value=None)
    return hs


def _make_fake_dhara(rows: list[tuple[str, dict[str, Any]]] | None = None) -> MagicMock:
    """Return a mock Dhara handle whose ``list_prefix`` is an AsyncMock.

    Defaults to returning no rows so a subscriber with this handle does
    a no-op tick. Tests override ``rows=`` when they want the
    subscriber to process something.
    """
    handle = MagicMock()
    handle.list_prefix = AsyncMock(return_value=list(rows or []))
    return handle


def _patch_embedding_service(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Swap the subscriber's ``get_embedding_service`` for one that returns a stub."""
    fake_svc = _make_fake_embedding_service()
    fake_factory = MagicMock(return_value=fake_svc)
    monkeypatch.setattr(f"{SUB_MODULE}.get_embedding_service", fake_factory)
    return fake_svc


# ---------------------------------------------------------------------------
# 1. Indexes a Dhara row into HotStore
# ---------------------------------------------------------------------------


class TestIndexesRow:
    @pytest.mark.asyncio
    async def test_subscriber_indexes_dhara_row_into_hot_store(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One Dhara row -> one HotStore.insert call with the right fields."""
        fake_svc = _patch_embedding_service(monkeypatch)

        hot_store = _make_fake_hot_store()
        dhara = _make_fake_dhara(
            rows=[
                (
                    "websocket_tool_invocation/v1/1700000000000",
                    {
                        "version": "1.0.0",
                        "tool": "websocket_get_status",
                        "surface": "websocket",
                        "result": "ok",
                        "duration_ms": 12,
                        "error": "",
                        "timestamp": "2026-08-29T12:00:00",
                    },
                ),
            ]
        )
        from akosha.ingestion.websocket_invocations_subscriber import (
            WebSocketInvocationsSubscriber,
        )

        sub = WebSocketInvocationsSubscriber(
            hot_store=hot_store,
            dhara_handle=dhara,
            poll_interval_seconds=0.01,
        )
        await sub._tick()

        dhara.list_prefix.assert_awaited_once()
        # Embedding service was consulted.
        fake_svc.generate_embedding.assert_awaited_once()
        # HotStore.insert was called once.
        hot_store.insert.assert_awaited_once()
        record: HotRecord = hot_store.insert.await_args.args[0]
        assert isinstance(record, HotRecord)
        assert record.system_id == "mahavishnu"
        assert record.conversation_id == "websocket_tool_invocation/v1/1700000000000"
        assert record.metadata["tool"] == "websocket_get_status"
        assert len(record.embedding) == 384


# ---------------------------------------------------------------------------
# 2. Skips unknown schema versions
# ---------------------------------------------------------------------------


class TestSchemaVersion:
    @pytest.mark.asyncio
    async def test_subscriber_skips_unknown_schema_version(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``version != "1.0.0"`` rows are not indexed (forward-compat)."""
        _patch_embedding_service(monkeypatch)
        hot_store = _make_fake_hot_store()
        dhara = _make_fake_dhara(
            rows=[
                (
                    "websocket_tool_invocation/v1/1",
                    {
                        "version": "2.0.0",
                        "tool": "websocket_x",
                        "surface": "websocket",
                        "result": "ok",
                        "timestamp": "2026-08-29T12:00:00",
                    },
                ),
                (
                    "websocket_tool_invocation/v1/2",
                    {
                        # missing version field
                        "tool": "websocket_y",
                        "surface": "websocket",
                        "result": "ok",
                        "timestamp": "2026-08-29T12:00:00",
                    },
                ),
            ]
        )
        from akosha.ingestion.websocket_invocations_subscriber import (
            WebSocketInvocationsSubscriber,
        )
        sub = WebSocketInvocationsSubscriber(
            hot_store=hot_store,
            dhara_handle=dhara,
            poll_interval_seconds=0.01,
        )
        await sub._tick()

        hot_store.insert.assert_not_awaited()


# ---------------------------------------------------------------------------
# 3. Graceful no-op when hot_store is missing
# ---------------------------------------------------------------------------


class TestGracefulNoop:
    @pytest.mark.asyncio
    async def test_subscriber_graceful_noop_when_hot_store_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``start()`` must not raise or spawn a task when ``hot_store is None``."""
        _patch_embedding_service(monkeypatch)
        from akosha.ingestion.websocket_invocations_subscriber import (
            WebSocketInvocationsSubscriber,
        )

        sub = WebSocketInvocationsSubscriber(
            hot_store=None,
            dhara_handle=_make_fake_dhara(),
            poll_interval_seconds=0.01,
        )

        await sub.start()
        try:
            assert sub._task is None
            assert sub._running is False
        finally:
            await sub.stop()


# ---------------------------------------------------------------------------
# 4. Polls Dhara on its interval
# ---------------------------------------------------------------------------


class TestPolling:
    @pytest.mark.asyncio
    async def test_subscriber_polls_dhara_on_interval(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The loop must call ``dhara.list_prefix`` repeatedly while running."""
        _patch_embedding_service(monkeypatch)
        hot_store = _make_fake_hot_store()
        dhara = _make_fake_dhara()  # returns no rows

        from akosha.ingestion.websocket_invocations_subscriber import (
            WebSocketInvocationsSubscriber,
        )

        # Replace ``asyncio.sleep`` with a coroutine that just yields so the
        # polling loop rotates without waiting real wall-clock time.
        sleeps: list[float] = []
        real_sleep = asyncio.sleep

        async def fast_sleep(delay: float) -> None:
            sleeps.append(delay)
            # Yield to the loop once so the next iteration runs.
            await real_sleep(0)

        monkeypatch.setattr("asyncio.sleep", fast_sleep)

        sub = WebSocketInvocationsSubscriber(
            hot_store=hot_store,
            dhara_handle=dhara,
            poll_interval_seconds=0.01,
        )
        await sub.start()
        # Let the loop spin a handful of times.
        await real_sleep(0.05)
        await sub.stop()

        # Multiple ticks must have happened.
        assert dhara.list_prefix.await_count >= 2, (
            f"expected at least 2 list_prefix calls, got {dhara.list_prefix.await_count}"
        )
        # The poll interval was respected by every sleep().
        assert all(s == 0.01 for s in sleeps)
        assert len(sleeps) >= 2


# ---------------------------------------------------------------------------
# 5. Idempotent on duplicate keys
# ---------------------------------------------------------------------------


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_subscriber_idempotent_on_duplicate_keys(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Same Dhara key returned twice -> HotStore.insert called once."""
        _patch_embedding_service(monkeypatch)
        hot_store = _make_fake_hot_store()
        # First poll returns the row; second poll returns the same row again.
        poll_results: list[list[tuple[str, dict[str, Any]]]] = [
            [
                (
                    "websocket_tool_invocation/v1/dup",
                    {
                        "version": "1.0.0",
                        "tool": "websocket_dup",
                        "surface": "websocket",
                        "result": "ok",
                        "duration_ms": 5,
                        "error": "",
                        "timestamp": "2026-08-29T12:00:00",
                    },
                )
            ],
            [
                (
                    "websocket_tool_invocation/v1/dup",
                    {
                        "version": "1.0.0",
                        "tool": "websocket_dup",
                        "surface": "websocket",
                        "result": "ok",
                        "duration_ms": 5,
                        "error": "",
                        "timestamp": "2026-08-29T12:00:01",
                    },
                )
            ],
        ]

        handle = MagicMock()
        handle.list_prefix = AsyncMock(side_effect=poll_results)

        from akosha.ingestion.websocket_invocations_subscriber import (
            WebSocketInvocationsSubscriber,
        )
        sub = WebSocketInvocationsSubscriber(
            hot_store=hot_store,
            dhara_handle=handle,
            poll_interval_seconds=0.01,
        )

        await sub._tick()
        await sub._tick()

        # First tick indexed it, second tick saw the same key in _seen_keys and
        # skipped past it.
        assert hot_store.insert.await_count == 1
        # Both ticks still polled Dhara (the seen-key check is in _tick).
        assert handle.list_prefix.await_count == 2


# ---------------------------------------------------------------------------
# 6. End-to-end integration: DharaHttpClient -> subscriber -> HotStore
# ---------------------------------------------------------------------------


class TestDharaHttpClientIntegration:
    @pytest.mark.asyncio
    async def test_subscriber_indexes_rows_via_dhara_http_client(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Replace the mock Dhara handle with a real ``DharaHttpClient`` whose
        underlying httpx call is mocked to return Dhara-format responses.

        Followup 4 of the websocket-search plan: after this ships, the
        subscriber's ``list_prefix`` actually reaches Dhara via HTTP
        rather than short-circuiting on ``dhara_handle=None``. This test
        proves the wiring end-to-end without standing up a real Dhara.
        """
        import httpx

        from akosha.storage.dhara_http_client import DharaHttpClient

        _patch_embedding_service(monkeypatch)
        hot_store = _make_fake_hot_store()

        # Build the Dhara-format response envelope the real Dhara MCP
        # server emits: ``{"content": [{"type": "text", "text": "<json>"}]}``
        # where ``<json>`` is the JSON-encoded list of {key, value} pairs.
        dhara_payload = [
            {
                "key": "websocket_tool_invocation/v1/1700000000000",
                "value": {
                    "version": "1.0.0",
                    "tool": "websocket_get_status",
                    "surface": "websocket",
                    "result": "ok",
                    "duration_ms": 12,
                    "error": "",
                    "timestamp": "2026-08-29T12:00:00",
                },
            }
        ]
        import json as _json

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.json = MagicMock(
            return_value={
                "content": [
                    {"type": "text", "text": _json.dumps(dhara_payload)}
                ]
            }
        )
        mock_response.raise_for_status = MagicMock(return_value=None)

        # Construct the real client, then inject a stub httpx.AsyncClient
        # so the post() call returns our envelope without a network hop.
        client = DharaHttpClient(base_url="http://dhara.test.invalid")
        stub_httpx = AsyncMock(spec=httpx.AsyncClient)
        stub_httpx.post = AsyncMock(return_value=mock_response)
        stub_httpx.aclose = AsyncMock(return_value=None)
        client._client = stub_httpx

        from akosha.ingestion.websocket_invocations_subscriber import (
            WebSocketInvocationsSubscriber,
        )

        sub = WebSocketInvocationsSubscriber(
            hot_store=hot_store,
            dhara_handle=client,
            poll_interval_seconds=0.01,
        )
        await sub._tick()

        # The HTTP POST should have hit the Dhara MCP ``list_prefix`` tool.
        stub_httpx.post.assert_awaited_once()
        post_kwargs = stub_httpx.post.await_args.kwargs
        assert post_kwargs["json"]["name"] == "list_prefix"
        assert post_kwargs["json"]["arguments"]["prefix"] == (
            "websocket_tool_invocation/v1/"
        )
        assert stub_httpx.post.await_args.args[0] == (
            "http://dhara.test.invalid/tools/call"
        )

        # And the parsed row should have made it into the HotStore.
        hot_store.insert.assert_awaited_once()
        record: HotRecord = hot_store.insert.await_args.args[0]
        assert isinstance(record, HotRecord)
        assert record.system_id == "mahavishnu"
        assert record.conversation_id == (
            "websocket_tool_invocation/v1/1700000000000"
        )


def _fake_embedding_service() -> MagicMock:
    """Standalone helper used by direct call sites if needed."""
    return _make_fake_embedding_service()


def _now_utc() -> datetime:
    """Today's UTC timestamp, used for default fallback comparisons."""
    return datetime.now(UTC)
