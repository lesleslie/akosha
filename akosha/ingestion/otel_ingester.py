"""OTel trace ingestion worker from an OTLP/HTTP collector.

Mirrors CodeGraphIngester's start/stop/polling-loop contract. Polls an
OTLP/HTTP collector for spans newer than the per-system-id watermark
and writes them as HotRecords into HotStore. The hot_store is the
shared singleton published by the Akosha MCP lifespan; the
embedding_service is the lifespan-owned EmbeddingService.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from datetime import UTC, datetime
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
        """Main polling loop. One cycle per ``poll_interval_seconds``."""
        try:
            while self._running:
                try:
                    # Each cycle polls once with a watermark = max over
                    # all known system_ids; new system_ids discovered
                    # mid-cycle get the recovery window. Each span's
                    # system_id is extracted from its OTLP resource.
                    since_unix_nano = self._now_unix_nano() - (
                        self.initial_lookback_seconds * 1_000_000_000
                    )
                    # If we already have watermarks, use the most
                    # recent one so we don't re-pull old spans.
                    if self._watermarks:
                        since_unix_nano = max(self._watermarks.values())
                    spans_by_system = await self._fetch_spans(
                        since_unix_nano=since_unix_nano
                    )
                    for system_id, span in spans_by_system:
                        try:
                            await self._ingest_span(span, system_id=system_id)
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            logger.exception(
                                f"OTel span ingestion failed for "
                                f"span_id={span.get('spanId', 'unknown')}: {e}"
                            )
                    # Wait before next poll
                    await asyncio.sleep(self.poll_interval_seconds)
                except asyncio.CancelledError:
                    logger.info("OTel polling loop cancelled")
                    break
                except Exception as e:
                    logger.exception(f"Error in OTel polling loop: {e}")
                    await asyncio.sleep(self.poll_interval_seconds)
        except asyncio.CancelledError:
            pass

    async def _fetch_spans(
        self, since_unix_nano: int
    ) -> list[tuple[str, dict[str, Any]]]:
        """Fetch spans newer than ``since_unix_nano`` from the OTLP/HTTP endpoint.

        Returns a list of ``(system_id, span)`` tuples. The system_id is
        extracted from the OTLP resource's ``service.name`` attribute;
        spans with no service.name default to ``"unknown"``. OTLP/HTTP
        wraps spans in ``resourceSpans[].scopeSpans[].spans[]``; this
        method unwraps that nesting.
        """
        if self._http_client is None:
            raise RuntimeError("HTTP client not initialized; call start() first")
        response = await self._http_client.get(
            self.otlp_endpoint,
            params={"since": str(since_unix_nano)},
        )
        response.raise_for_status()
        body = response.json()
        result: list[tuple[str, dict[str, Any]]] = []
        for resource_spans in body.get("resourceSpans", []):
            # Extract service.name from resource.attributes
            system_id = "unknown"
            for attr in resource_spans.get("resource", {}).get("attributes", []):
                if attr.get("key") == "service.name":
                    value_entry = attr.get("value", {})
                    if "stringValue" in value_entry:
                        system_id = value_entry["stringValue"]
                    break
            for scope_spans in resource_spans.get("scopeSpans", []):
                for span in scope_spans.get("spans", []):
                    result.append((system_id, span))
        return result

    def _normalize_span(
        self,
        span: dict[str, Any],
        system_id: str,
        embedding: list[float],
    ) -> Any:  # returns HotRecord; Any to avoid runtime import in TYPE_CHECKING
        """Map an OTel span to a HotRecord.

        ``content`` is a JSON dump of the span fields (sans traceId,
        spanId, which are duplicated in the conversation_id and metadata).
        ``metadata.attributes.task_class`` is extracted from the
        ``task.class`` semantic attribute so ``query_local_traces`` can
        filter on it via the existing SQL WHERE clause.
        """
        from akosha.storage.models import HotRecord

        attrs = self._attrs_to_dict(span.get("attributes", []))
        task_class = attrs.get("task.class")

        # Serialize the span (drop spanId/traceId — those land in
        # conversation_id and metadata.otel.trace_id).
        span_for_content = {
            k: v for k, v in span.items() if k not in ("traceId", "spanId")
        }
        content = json.dumps(span_for_content, sort_keys=True, default=str)

        start_unix_nano = int(span.get("startTimeUnixNano", "0"))
        ts = datetime.fromtimestamp(start_unix_nano / 1_000_000_000, tz=UTC)

        return HotRecord(
            system_id=system_id,
            conversation_id=f"{system_id}:{span.get('spanId', '')}",
            content=content,
            embedding=embedding,
            timestamp=ts,
            metadata={
                "attributes": {"task_class": task_class} if task_class else {},
                "otel": {"trace_id": span.get("traceId", "")},
            },
        )

    async def _ingest_span(
        self,
        span: dict[str, Any],
        system_id: str,
    ) -> None:
        """Embed the span content, insert as a HotRecord, advance the watermark."""
        content_for_embedding = (
            f"{span.get('name', '')} "
            f"{self._attrs_to_dict(span.get('attributes', []))}"
        )
        embedding_array = await self.embedding_service.generate_embedding(
            content_for_embedding
        )
        record = self._normalize_span(
            span, system_id=system_id, embedding=embedding_array.tolist()
        )
        await self.hot_store.insert(record)
        # Watermark advances ONLY on successful insert.
        start_unix_nano = int(span.get("startTimeUnixNano", "0"))
        self._watermarks[system_id] = max(
            self._watermarks.get(system_id, 0), start_unix_nano
        )

    @staticmethod
    def _attrs_to_dict(attrs: list[dict[str, Any]] | None) -> dict[str, Any]:
        """Flatten OTel attributes list ``[{key, value}]`` into a dict.

        OTel value entries are wrapped: ``{"stringValue": "..."}`` or
        ``{"intValue": "..."}``. We unwrap the stringValue/intValue
        variant for the common cases.
        """
        result: dict[str, Any] = {}
        for entry in attrs or []:
            key = entry.get("key")
            value_entry = entry.get("value", {})
            if "stringValue" in value_entry:
                result[key] = value_entry["stringValue"]
            elif "intValue" in value_entry:
                result[key] = int(value_entry["intValue"])
            elif "boolValue" in value_entry:
                result[key] = bool(value_entry["boolValue"])
            else:
                result[key] = value_entry
        return result

    @staticmethod
    def _now_unix_nano() -> int:
        """Current wall-clock time in unix nanoseconds (OTLP convention)."""
        return int(time.time_ns())
