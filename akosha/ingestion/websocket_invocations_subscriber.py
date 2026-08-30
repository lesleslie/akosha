"""Dhara -> HotStore subscriber for Mahavishnu's websocket tool invocations.

Plan: docs/plans/2026-08-29-akosha-websocket-search.md (Sub-plan B).
Polls the Dhara prefix ``websocket_tool_invocation/v1/*`` (keyed by
``WebsocketToolInvocationV1`` schema, registered in Mahavishnu) on a
configurable interval, embeds each row's content, and inserts a
``HotRecord`` into the HotStore. ``search_all_systems`` queries those
records (Sub-plan C wires the search path).

The subscriber is fail-soft: missing HotStore, missing Dhara handle,
schema version mismatch, embedding service unavailable, or any per-row
exception logs at WARNING and continues. Polling stops cleanly on
``stop()`` via the ``_running`` flag (no thread or task cancellation
needed -- the loop checks ``_running`` at the top of each iteration).

The ``_seen_keys: set[str]`` field provides idempotency without
database-side ``INSERT OR IGNORE`` semantics -- we just skip keys
we've already processed within this subscriber's lifetime. Persistent
deduplication is left to the HotStore's own content-hash check
(``HotStore._compute_content_hash``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from akosha.models import HotRecord
from akosha.processing.embeddings import get_embedding_service

logger = logging.getLogger(__name__)


DHARA_KEY_PREFIX = "websocket_tool_invocation/v1/"
SUPPORTED_SCHEMA_VERSION = "1.0.0"
SYSTEM_ID_MAHAVISHNU = "mahavishnu"


class WebSocketInvocationsSubscriber:
    """Polls Dhara for Mahavishnu's websocket invocation audit rows.

    Each row is embedded and inserted into the HotStore so that
    ``mcp__akosha__search_all_systems`` can serve real results.

    Attributes:
        _hot_store: HotStore handle (AsyncMock-friendly). May be None
            to indicate "do not run".
        _dhara: Object exposing ``await list_prefix(prefix)``. May be
            None; the subscriber becomes a no-op when this is unset.
        _poll_interval_seconds: Seconds between polls.
        _running: Set True while the polling loop is active.
        _task: The asyncio task running the loop, if any.
        _seen_keys: In-process set of Dhara keys already processed.
            Cleared on restart; persistent dedup is HotStore-side.
    """

    def __init__(
        self,
        *,
        hot_store: Any = None,
        dhara_handle: Any = None,
        poll_interval_seconds: float = 5.0,
    ) -> None:
        """Initialize the subscriber.

        Args:
            hot_store: HotStore instance. ``None`` means "no-op mode"
                (subscriber runs but ticks are skipped).
            dhara_handle: Object providing ``async list_prefix(prefix)``.
                ``None`` is allowed; ``_tick`` short-circuits.
            poll_interval_seconds: Sleep between polling iterations.
        """
        self._hot_store = hot_store
        self._dhara = dhara_handle
        self._poll_interval = poll_interval_seconds
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._seen_keys: set[str] = set()

    async def start(self) -> None:
        """Start the polling loop.

        No-op if already running, or if ``hot_store is None`` (lite
        mode / disabled in settings).
        """
        if self._running:
            return
        if self._hot_store is None:
            logger.debug(
                "WebSocketInvocationsSubscriber: no hot_store, skipping start"
            )
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "WebSocketInvocationsSubscriber started (interval=%.1fs)",
            self._poll_interval,
        )

    async def stop(self) -> None:
        """Stop the polling loop; wait for the in-flight tick to finish."""
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("WebSocketInvocationsSubscriber task raised: %s", exc)
        self._task = None
        logger.info("WebSocketInvocationsSubscriber stopped")

    async def _poll_loop(self) -> None:
        """One poll iteration at a time. Exits when ``_running`` flips False."""
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("WebSocketInvocationsSubscriber tick failed: %s", exc)
            await asyncio.sleep(self._poll_interval)

    async def _tick(self) -> None:
        """One polling iteration: fetch rows from Dhara, index each into HotStore."""
        if self._dhara is None:
            return
        try:
            rows = await self._dhara.list_prefix(DHARA_KEY_PREFIX)
        except Exception as exc:
            logger.debug("WebSocketInvocationsSubscriber: list_prefix failed: %s", exc)
            return
        for key, payload in rows:
            if key in self._seen_keys:
                continue
            try:
                await self._index_row(key, payload)
                self._seen_keys.add(key)
            except Exception as exc:
                logger.warning(
                    "WebSocketInvocationsSubscriber: failed to index key=%s: %s",
                    key,
                    exc,
                )

    async def _index_row(self, key: str, payload: dict[str, Any]) -> None:
        """Embed payload content and insert a HotRecord into the HotStore.

        Skips rows whose ``version`` field does not match
        ``SUPPORTED_SCHEMA_VERSION`` (forward-compat). Rows with an
        empty content string are also skipped.

        The embedding service is invoked via its module-level factory;
        tests can monkeypatch the ``get_embedding_service`` symbol on
        this module.
        """
        if payload.get("version") != SUPPORTED_SCHEMA_VERSION:
            return
        content = self._build_content(payload)
        if not content:
            return
        embedding_service = get_embedding_service()
        vec = await embedding_service.generate_embedding(content)
        # ndarray -> list[float] for the FLOAT[384] HotStore schema.
        embedding = vec.tolist() if hasattr(vec, "tolist") else list(vec)
        record = HotRecord(
            system_id=SYSTEM_ID_MAHAVISHNU,
            conversation_id=key,
            content=content,
            embedding=embedding,
            timestamp=self._parse_timestamp(payload),
            metadata=payload,
        )
        await self._hot_store.insert(record)

    @staticmethod
    def _build_content(payload: dict[str, Any]) -> str:
        """Render the audit row as a flat string for embedding.

        Embedding a structured audit row gives better semantic recall
        than embedding each field individually, because the language
        model sees the surrounding context.
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
        """Best-effort ISO-8601 timestamp parse; falls back to now()."""
        ts = payload.get("timestamp")
        if isinstance(ts, str):
            try:
                return datetime.fromisoformat(ts)
            except ValueError:
                pass
        return datetime.utcnow()
