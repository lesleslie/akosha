"""OTel trace ingestion worker from an OTLP/HTTP collector.

Mirrors CodeGraphIngester's start/stop/polling-loop contract. Polls an
OTLP/HTTP collector for spans newer than the per-system-id watermark
and writes them as HotRecords into HotStore. The hot_store is the
shared singleton published by the Akosha MCP lifespan; the
embedding_service is the lifespan-owned EmbeddingService.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import httpx2 as httpx

if TYPE_CHECKING:
    from akosha.processing.embeddings import EmbeddingService
    from akosha.storage.hot_store import HotStore

logger = logging.getLogger(__name__)


class OtelTraceIngester:
    """Pull-based OTel trace ingester from an OTLP/HTTP collector.

    Polls an OTLP/HTTP collector for spans newer than the per-system-id
    watermark and writes them as HotRecords into HotStore.
    """

    def __init__(
        self,
        hot_store: HotStore,
        embedding_service: EmbeddingService,
        otlp_endpoint: str = "http://localhost:4318/v1/traces",
        poll_interval_seconds: int = 60,
        max_spans_per_poll: int = 500,
        initial_lookback_seconds: int = 3600,
    ) -> None:
        self.hot_store = hot_store
        self.embedding_service = embedding_service
        self.otlp_endpoint = otlp_endpoint
        self.poll_interval_seconds = poll_interval_seconds
        self.max_spans_per_poll = max_spans_per_poll
        self.initial_lookback_seconds = initial_lookback_seconds
        self._running = False
        self._poll_task: asyncio.Task[None] | None = None
        self._http_client: httpx.AsyncClient | None = None
        # Per-system-id watermark; system_id -> max start_time_unix_nano seen
        self._watermarks: dict[str, int] = {}

    async def start(self) -> None:
        """Start the OTel trace ingestion worker."""
        if self._running:
            logger.warning("OTel trace ingester already running")
            return

        self._http_client = httpx.AsyncClient(timeout=30.0)
        self._running = True
        self._poll_task = asyncio.create_task(self._polling_loop())
        logger.info(
            f"Started OTel trace ingestion from {self.otlp_endpoint} "
            f"(interval={self.poll_interval_seconds}s)"
        )

    async def stop(self) -> None:
        """Stop the OTel trace ingestion worker."""
        if not self._running:
            return

        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._poll_task
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        logger.info("Stopped OTel trace ingestion")

    async def _polling_loop(self) -> None:
        """Main polling loop. Implemented in Task 2."""
        # Placeholder; Task 2 fills this in.
        while self._running:
            await asyncio.sleep(self.poll_interval_seconds)
