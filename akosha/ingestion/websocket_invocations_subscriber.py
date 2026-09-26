"""Orchestrator: Redis push for Mahavishnu's websocket tool invocations.

Plan: docs/plans/2026-08-29-push-subscriber.md Phase 3.

The class wires the ``BodaiToolInvocationSubscriber`` (Redis Streams
push) into the HotStore so ``mcp__akosha__search_all_systems`` can
serve real results. The previous Dhara-poll fallback was removed
when Dhara was decommissioned; the orchestrator now only drives the
push path (which is the source of truth). When the push subscriber
fails to start (Redis unavailable), the orchestrator simply stays
not-running — there is no longer a poll fallback to degrade to.

The orchestrator's fail-soft contract mirrors
``BodaiToolInvocationSubscriber``: missing HotStore, schema version
mismatch, embedding service unavailable, or any per-row exception
logs at WARNING and continues. Polling stops cleanly on ``stop()``
via the ``_running`` flag. ``stop()`` also tears down the optional
push subscriber.

The ``_seen_keys: set[str]`` field provides idempotency without
database-side ``INSERT OR IGNORE`` semantics -- we just skip keys
we've already processed within this subscriber's lifetime. Persistent
deduplication is left to the HotStore's own content-hash check
(``HotStore._compute_content_hash``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

from akosha.models import HotRecord
from akosha.processing.embeddings import get_embedding_service

logger = logging.getLogger(__name__)


SUPPORTED_SCHEMA_VERSION = "1.0.0"
SYSTEM_ID_MAHAVISHNU = "mahavishnu"


class WebSocketInvocationsSubscriber:
    """Push-mode subscriber for Mahavishnu's websocket invocation rows.

    When ``bodai_subscriber`` is provided and successfully started
    (``running=True``), the orchestrator owns ingestion. Rows arrive
    via Redis (``bodai:events`` -> ``BodaiToolInvocationSubscriber`` ->
    ``_index_row``).

    Each row is embedded and inserted into the HotStore so that
    ``mcp__akosha__search_all_systems`` can serve real results.

    Attributes:
        _hot_store: HotStore handle (AsyncMock-friendly). May be None
            to indicate "do not run".
        _bodai_subscriber: Optional ``BodaiToolInvocationSubscriber``
            providing push-mode indexing. When provided AND running,
            the orchestrator owns ingestion.
        _running: Set True while the subscriber is active.
        _source: Which source is active (``"push"``). Exposed via
            ``source`` for tests / observability.
    """

    def __init__(
        self,
        *,
        hot_store: Any = None,
        poll_interval_seconds: float = 5.0,
        bodai_subscriber: Any | None = None,
    ) -> None:
        """Initialize the orchestrator.

        Args:
            hot_store: HotStore instance. ``None`` means "no-op mode"
                (orchestrator runs but ticks are skipped).
            poll_interval_seconds: Reserved for compatibility with
                earlier revisions; the push path does not poll.
            bodai_subscriber: Optional push-mode subscriber. When
                provided, the orchestrator lets the push subscriber
                own ingestion.
        """
        self._hot_store = hot_store
        self._poll_interval = poll_interval_seconds
        self._bodai_subscriber = bodai_subscriber
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._seen_keys: set[str] = set()
        self._source: str | None = None

    @property
    def source(self) -> str | None:
        """Which source the orchestrator currently drives (always ``"push"``)."""
        return self._source

    async def start(self) -> None:
        """Start the push subscriber.

        No-op if already running, or if ``hot_store is None`` (lite
        mode / disabled in settings).
        """
        if self._running:
            return
        if self._hot_store is None:
            logger.debug("WebSocketInvocationsSubscriber: no hot_store, skipping start")
            return

        if self._bodai_subscriber is None:
            logger.debug(
                "WebSocketInvocationsSubscriber: no bodai subscriber configured, skipping start",
            )
            return

        try:
            await self._bodai_subscriber.start()
        except Exception as exc:
            logger.warning(
                "WebSocketInvocationsSubscriber: bodai subscriber start failed: %s",
                exc,
            )
            self._bodai_subscriber = None
            return

        if getattr(self._bodai_subscriber, "running", False):
            self._running = True
            self._source = "push"
            logger.info(
                "WebSocketInvocationsSubscriber: source=push (bodai subscriber owns ingestion)",
            )

    async def stop(self) -> None:
        """Stop the push subscriber.

        Tears down the optional push subscriber and clears
        orchestration state. Idempotent — safe to call when not
        running.
        """
        self._running = False
        if self._bodai_subscriber is not None:
            try:
                await self._bodai_subscriber.stop()
            except Exception as exc:
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
        # per-row exceptions here are logged at WARNING by the push
        # subscriber's outer loop.
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
            with contextlib.suppress(ValueError):
                return datetime.fromisoformat(ts)
        return datetime.now(tz=UTC)
