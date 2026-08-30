"""Orchestrator: Dhara poll OR Redis push for Mahavishnu's websocket tool invocations.

Plan: docs/plans/2026-08-29-akosha-websocket-search.md (Sub-plan B) +
docs/plans/2026-08-29-push-subscriber.md Phase 3.

The class is now an orchestrator: when ``bodai_subscriber`` is
provided and successfully started (``running=True``), the orchestrator
skips the Dhara poll loop. Otherwise it falls back to the historical
5-second poll loop (Sub-plan B behaviour). The push path delivers
rows in milliseconds; the poll path remains the source of truth when
Redis is unavailable so no envelopes are lost.

The orchestrator's fail-soft contract mirrors
``BodaiToolInvocationSubscriber``: missing HotStore, missing Dhara
handle, schema version mismatch, embedding service unavailable, or
any per-row exception logs at WARNING and continues. Polling stops
cleanly on ``stop()`` via the ``_running`` flag (no thread or task
cancellation needed -- the loop checks ``_running`` at the top of each
iteration). ``stop()`` also tears down the optional push subscriber.

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
    """Orchestrates push vs poll for Mahavishnu's websocket invocation rows.

    Two source modes:

    * Push: ``bodai_subscriber`` is set AND ``running=True`` ->
      orchestrator skips the poll loop; rows arrive via Redis
      (``bodai:events`` -> ``BodaiToolInvocationSubscriber`` ->
      ``_index_row``).
    * Poll: ``bodai_subscriber`` is None OR failed to start ->
      orchestrator runs the historical 5-second Dhara poll loop
      (legacy path, retained as the fail-soft fallback).

    Each row is embedded and inserted into the HotStore so that
    ``mcp__akosha__search_all_systems`` can serve real results.

    Attributes:
        _hot_store: HotStore handle (AsyncMock-friendly). May be None
            to indicate "do not run".
        _dhara: Object exposing ``await list_prefix(prefix)``. May be
            None; the subscriber becomes a no-op when this is unset.
        _poll_interval_seconds: Seconds between polls.
        _bodai_subscriber: Optional ``BodaiToolInvocationSubscriber``
            providing push-mode indexing. When provided AND running, the
            poll loop is not started.
        _running: Set True while a source loop is active.
        _task: The asyncio task running the loop, if any.
        _seen_keys: In-process set of Dhara keys already processed.
            Cleared on restart; persistent dedup is HotStore-side.
        _source: Which source is active (``"push"`` or ``"poll"``).
            Exposed via ``source`` for tests / observability.
    """

    def __init__(
        self,
        *,
        hot_store: Any = None,
        dhara_handle: Any = None,
        poll_interval_seconds: float = 5.0,
        bodai_subscriber: Any | None = None,
    ) -> None:
        """Initialize the orchestrator.

        Args:
            hot_store: HotStore instance. ``None`` means "no-op mode"
                (orchestrator runs but ticks are skipped).
            dhara_handle: Object providing ``async list_prefix(prefix)``.
                ``None`` is allowed; ``_tick`` short-circuits.
            poll_interval_seconds: Sleep between polling iterations.
            bodai_subscriber: Optional push-mode subscriber. When
                provided, the orchestrator lets the push subscriber
                own ingestion and only falls back to polling when the
                push subscriber fails to start.
        """
        self._hot_store = hot_store
        self._dhara = dhara_handle
        self._poll_interval = poll_interval_seconds
        self._bodai_subscriber = bodai_subscriber
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._seen_keys: set[str] = set()
        self._source: str | None = None

    @property
    def source(self) -> str | None:
        """Which source the orchestrator currently drives (``"push"`` or ``"poll"``)."""
        return self._source

    async def start(self) -> None:
        """Start the push subscriber if provided, else the poll loop.

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

        # Push-first: when a push subscriber is provided AND its
        # ``running`` flag flips True after ``start()``, the poll
        # loop is skipped. ``start()`` on the push subscriber is
        # fail-soft — when Redis is down it stays non-running and we
        # fall back to the poll loop transparently.
        if self._bodai_subscriber is not None:
            try:
                await self._bodai_subscriber.start()
            except Exception as exc:  # noqa: BLE001 - log + fall back
                logger.warning(
                    "akosha.subscriber.fallback_to_poll: bodai subscriber start failed: %s",
                    exc,
                )
                self._bodai_subscriber = None
            if (
                self._bodai_subscriber is not None
                and getattr(self._bodai_subscriber, "running", False)
            ):
                self._running = True
                self._source = "push"
                logger.info(
                    "WebSocketInvocationsSubscriber: source=push "
                    "(bodai subscriber owns ingestion)",
                )
                return

        # Fallback path: legacy 5-second Dhara poll loop.
        self._running = True
        self._source = "poll"
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "WebSocketInvocationsSubscriber: source=poll (interval=%.1fs)",
            self._poll_interval,
        )

    async def stop(self) -> None:
        """Stop whichever source is active; tear down the push subscriber first.

        Order matters: the push subscriber's loop is independent, so
        it must be cancelled BEFORE we drop the orchestrator's
        ``_running`` flag (the poll loop reads it). Then we cancel the
        poll task and clear.
        """
        self._running = False
        if self._bodai_subscriber is not None:
            try:
                await self._bodai_subscriber.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "WebSocketInvocationsSubscriber: bodai subscriber stop failed: %s",
                    exc,
                )
            self._bodai_subscriber = None
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("WebSocketInvocationsSubscriber task raised: %s", exc)
        self._task = None
        self._source = None
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
        # ndarray -> list[float] for the FLOAT[N] HotStore schema (N is
        # the active embedding backend's dim; see Phase 2 of
        # docs/plans/2026-08-29-embedding-dim-fix.md). Dim validation is
        # enforced inside ``HotStore.insert()`` (fail-loud ValueError);
        # the fail-soft contract here is preserved by the
        # ``_tick`` exception handler — dim mismatches log at WARNING
        # and the subscriber continues with the next row.
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
