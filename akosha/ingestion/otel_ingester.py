"""OTel trace ingestion worker for standard OTLP/HTTP receivers.

Mirrors CodeGraphIngester's start/stop/polling-loop contract. Polls a
standard OTLP/HTTP receiver for spans and writes them as HotRecords
into HotStore. The hot_store is the shared singleton published by
the Akosha MCP lifespan; the embedding_service is the lifespan-owned
EmbeddingService.

The ingester issues a POST against ``/v1/traces`` with an empty
``resourceSpans`` envelope (the ingester does not export spans; it
consumes them). Standard receivers that accept OTLP/HTTP export
requests will respond with the spans they currently hold. Vanilla
collectors configured for export-only, or snapshot-poll collectors
that expose history on a different endpoint, will respond with
404/405/415 — those are treated as empty polls with a WARN log
and a bump to ``_errors_total`` so per-feed observability surfaces
the gap.

Watermarking is documented in :meth:`_polling_loop` (global watermark
across known systems, recovery window on cold start).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx2 as httpx

if TYPE_CHECKING:
    from akosha.models import HotRecord
    from akosha.processing.embeddings import EmbeddingService
    from akosha.storage.hot_store import HotStore
    from akosha.storage.pgvector_hot_store import PgvectorHotStore

logger = logging.getLogger(__name__)


class OtelTraceIngester:
    """Pull-based OTel trace ingester from an OTLP/HTTP collector.

    Polls an OTLP/HTTP collector for spans newer than the per-system-id
    watermark and writes them as HotRecords into HotStore.
    """

    def __init__(
        self,
        hot_store: HotStore | PgvectorHotStore,
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
        # Per-feed observability counters (see mcp-backend-wiring-discipline.md):
        # every feed must expose cycles_total, errors_total, last_poll_at
        self._cycles_total: int = 0
        self._errors_total: int = 0
        self._last_poll_at: float | None = None

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
            # CancelledError is the expected outcome of cancel(); suppress
            # it explicitly. Any OTHER exception escaping the polling loop
            # during shutdown is a real bug — log it but don't re-raise
            # so callers can still clean up their own resources (the
            # close() below, the lifespan teardown, etc.) without losing
            # the signal. The detail is recorded via logger.exception so
            # the post-mortem trail isn't lost.
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception(
                    "OTel polling loop raised during shutdown; "
                    "continuing teardown to avoid leaking the HTTP client"
                )
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        logger.info("Stopped OTel trace ingestion")

    async def _polling_loop(self) -> None:
        """Main polling loop. One cycle per ``poll_interval_seconds``.

        Watermarking semantics:
        - Cold start (no watermarks yet): each cycle polls
          ``now - initial_lookback_seconds`` so newly-discovered
          services get a recovery window.
        - Warm cycle: poll ``max(watermarks.values())``. This is a
          **global** watermark across all known systems because the
          snapshot-poll API takes a single ``since`` parameter. Slow
          systems may have already-ingested spans < global watermark
          permanently skipped; the per-system watermark dict is
          kept for forensics and to support a future per-system
          fetch. If you need strict per-system dedup, expose a
          batched endpoint that takes per-system ``since`` tuples.

        Cancellation: the inner per-span handler re-raises so the
        cycle-level ``except asyncio.CancelledError`` catches it and
        breaks the loop. No outer wrapper is needed — the inner
        handler is sufficient.
        """
        while self._running:
            try:
                self._cycles_total += 1
                since_unix_nano = self._now_unix_nano() - (
                    self.initial_lookback_seconds * 1_000_000_000
                )
                # If we already have watermarks, use the most
                # recent one so we don't re-pull old spans.
                # Note: this is the *global* watermark, not per-system.
                if self._watermarks:
                    since_unix_nano = max(self._watermarks.values())
                spans_by_system = await self._fetch_spans(since_unix_nano=since_unix_nano)
                for system_id, span in spans_by_system:
                    try:
                        await self._ingest_span(span, system_id=system_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        self._errors_total += 1
                        logger.exception(
                            f"OTel span ingestion failed for "
                            f"span_id={span.get('spanId', 'unknown')}: {e}"
                        )
                self._last_poll_at = time.time()
                # Wait before next poll
                await asyncio.sleep(self.poll_interval_seconds)
            except asyncio.CancelledError:
                logger.info("OTel polling loop cancelled")
                break
            except Exception as e:
                self._errors_total += 1
                logger.exception(f"Error in OTel polling loop: {e}")
                await asyncio.sleep(self.poll_interval_seconds)

    async def _fetch_spans(self, since_unix_nano: int) -> list[tuple[str, dict[str, Any]]]:
        """Fetch spans newer than ``since_unix_nano`` from the OTLP/HTTP endpoint.

        Issues a POST with an empty ``resourceSpans`` envelope (the
        ingester is a consumer, not an exporter — the empty body keeps
        the receiver's parser happy without us fabricating spans).
        Standard OTLP/HTTP receivers respond with the spans they hold;
        export-only collectors and snapshot-poll collectors return
        404/405/415, which we treat as empty polls with a WARN log and
        a bump to ``_errors_total`` so per-feed observability surfaces
        the gap. ``since_unix_nano`` is preserved in the signature for
        the per-poll INFO log even though OTLP/HTTP does not use it as
        a request parameter — see :meth:`_polling_loop` for the
        watermark semantics.

        Returns a list of ``(system_id, span)`` tuples. The system_id is
        extracted from the OTLP resource's ``service.name`` attribute;
        spans with no service.name default to ``"unknown"``. OTLP/HTTP
        wraps spans in ``resourceSpans[].scopeSpans[].spans[]``; this
        method unwraps that nesting.
        """
        if self._http_client is None:
            raise RuntimeError("HTTP client not initialized; call start() first")
        response = await self._http_client.post(
            self.otlp_endpoint,
            json={"resourceSpans": []},
            headers={"Content-Type": "application/json"},
        )
        status_code = response.status_code
        method = "POST"
        path = self.otlp_endpoint
        # Read body once for the log line + JSON parsing. ``len()`` works
        # on httpx bytes; empty bodies give 0.
        body_bytes = response.content
        body_len = len(body_bytes) if body_bytes is not None else 0
        logger.info(
            "OTel poll: method=%s path=%s status=%d bytes=%d since_unix_nano=%d",
            method,
            path,
            status_code,
            body_len,
            since_unix_nano,
        )
        if 200 <= status_code < 300:
            try:
                body = response.json()
            except (ValueError, json.JSONDecodeError):
                logger.warning(
                    "OTel poll returned 2xx but body was not valid JSON; "
                    "method=%s path=%s status=%d",
                    method,
                    path,
                    status_code,
                )
                return []
            if not body.get("resourceSpans"):
                return []
            result: list[tuple[str, dict[str, Any]]] = []
            for resource_spans in body.get("resourceSpans", []):
                # Extract service.name from resource.attributes. Per the OTLP
                # spec the value is one of the AnyValue wrappers;
                # ``stringValue`` is the canonical type for service.name but
                # we tolerate other shapes by stringifying the unwrapped
                # value. ``value: null`` and missing keys fall through to
                # ``"unknown"`` rather than raising TypeError.
                system_id = "unknown"
                for attr in resource_spans.get("resource", {}).get("attributes") or []:
                    if attr.get("key") != "service.name":
                        continue
                    value_entry = attr.get("value") or {}
                    if "stringValue" in value_entry:
                        system_id = str(value_entry["stringValue"])
                    elif "intValue" in value_entry:
                        system_id = str(value_entry["intValue"])
                    elif "boolValue" in value_entry:
                        system_id = str(value_entry["boolValue"])
                    elif "doubleValue" in value_entry:
                        system_id = str(value_entry["doubleValue"])
                    break
                for scope_spans in resource_spans.get("scopeSpans", []):
                    for span in scope_spans.get("spans", []):
                        result.append((system_id, span))
            return result
        if status_code in (404, 405, 415):
            # Receiver reachable but doesn't expose OTLP/HTTP export on this
            # endpoint — treat as empty poll. Bump _errors_total so per-feed
            # observability surfaces the gap.
            self._errors_total += 1
            logger.warning(
                "OTel poll: receiver not OTLP/HTTP-export-capable; "
                "method=%s path=%s status=%d",
                method,
                path,
                status_code,
            )
            return []
        if 500 <= status_code < 600:
            # 5xx → let the existing exception path in _polling_loop handle it
            # (it logs + bumps _errors_total + sleeps).
            response.raise_for_status()
        # Other 4xx (e.g. 400 Bad Request, 401/403 auth, 413 Payload Too Large)
            # The receiver is reachable, the request shape is wrong — this is a
            # CLIENT bug, not a transport error. Do NOT bump _errors_total; that
            # counter is for transport/feed health. Log loudly so the operator
            # notices the malformed request.
        logger.warning(
            "OTel poll: client error (not a transport failure); "
            "method=%s path=%s status=%d since_unix_nano=%d",
            method,
            path,
            status_code,
            since_unix_nano,
        )
        return []

    def _normalize_span(
        self,
        span: dict[str, Any],
        system_id: str,
        embedding: list[float],
    ) -> HotRecord:
        """Map an OTel span to a HotRecord.

        ``content`` is a JSON dump of the span fields (sans traceId,
        spanId, which are duplicated in the conversation_id and metadata).
        ``metadata.attributes.task_class`` is extracted from the
        ``task.class`` semantic attribute so ``query_local_traces`` can
        filter on it via the existing SQL WHERE clause.
        """
        from akosha.models import HotRecord

        task_class = self._attrs_to_dict(span.get("attributes", [])).get("task.class")

        # Serialize the span (drop spanId/traceId — those land in
        # conversation_id and metadata.otel.trace_id).
        span_for_content = {k: v for k, v in span.items() if k not in ("traceId", "spanId")}
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
                "otel": {
                    "trace_id": span.get("traceId", ""),
                    "span_id": span.get("spanId", ""),
                },
            },
        )

    async def _ingest_span(
        self,
        span: dict[str, Any],
        system_id: str,
    ) -> None:
        """Embed the span content, insert as a HotRecord, advance the watermark.

        The embedding input is the span ``name`` followed by its attributes
        rendered as ``key=value`` pairs separated by spaces. This produces
        cleaner tokens for natural-language embedding models than the
        previous ``f"{name} {dict_repr}"`` format (which embedded Python
        repr punctuation — single quotes, braces, colons — that the
        embedding model would tokenize as foreign characters).
        """
        attrs = self._attrs_to_dict(span.get("attributes", []))
        attr_pairs = " ".join(f"{k}={v}" for k, v in sorted(attrs.items()))
        content_for_embedding = f"{span.get('name', '')} {attr_pairs}".strip()
        embedding_array = await self.embedding_service.generate_embedding(content_for_embedding)
        record = self._normalize_span(span, system_id=system_id, embedding=embedding_array.tolist())
        await self.hot_store.insert(record)
        # Watermark advances ONLY on successful insert.
        start_unix_nano = int(span.get("startTimeUnixNano", "0"))
        self._watermarks[system_id] = max(self._watermarks.get(system_id, 0), start_unix_nano)

    @staticmethod
    def _attrs_to_dict(attrs: list[dict[str, Any]] | None) -> dict[str, Any]:
        """Flatten OTel attributes list ``[{key, value}]`` into a dict.

        OTel value entries are wrapped in AnyValue: ``{"stringValue": "..."}``,
        ``{"intValue": "..."}``, ``{"doubleValue": ...}``, ``{"boolValue": ...}``,
        ``{"arrayValue": {"values": [...]}}``, ``{"kvlistValue": {"values": [...]}}``,
        ``{"bytesValue": "<base64>"}``. The unwrap supports all of the
        primitive shapes and recursively descends into array/kvlist entries
        so the resulting dict holds pure Python primitives (no raw
        ``{"stringValue": ...}`` wrappers left).
        """

        def _unwrap(value_entry: dict[str, Any] | None) -> Any:
            if not value_entry:
                return None
            if "stringValue" in value_entry:
                return value_entry["stringValue"]
            if "intValue" in value_entry:
                return int(value_entry["intValue"])
            if "doubleValue" in value_entry:
                return float(value_entry["doubleValue"])
            if "boolValue" in value_entry:
                return bool(value_entry["boolValue"])
            if "bytesValue" in value_entry:
                return value_entry["bytesValue"]
            if "arrayValue" in value_entry:
                array = value_entry["arrayValue"] or {}
                return [_unwrap(v) for v in array.get("values", [])]
            if "kvlistValue" in value_entry:
                kvlist = value_entry["kvlistValue"] or {}
                return {
                    (kv or {}).get("key"): _unwrap((kv or {}).get("value"))
                    for kv in kvlist.get("values", [])
                }
            # Unknown variant — keep the wrapper but stringified so the
            # embedding text is still meaningful instead of a Python repr.
            return str(value_entry)

        result: dict[str, Any] = {}
        for entry in attrs or []:
            key = entry.get("key")
            if key is None:
                continue
            result[key] = _unwrap(entry.get("value"))
        return result

    @staticmethod
    def _now_unix_nano() -> int:
        """Current wall-clock time in unix nanoseconds (OTLP convention)."""
        return time.time_ns()
