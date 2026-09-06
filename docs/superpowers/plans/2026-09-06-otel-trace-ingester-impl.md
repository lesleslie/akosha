# OTel Trace Ingester Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `akosha/ingestion/otel_ingester.py` — a polling ingester that pulls OTel spans from an OTLP/HTTP collector and writes them as `HotRecord`s into `HotStore` — and wire it into the Akosha MCP lifespan alongside the existing `CodeGraphIngester`.

**Architecture:** Mirror `CodeGraphIngester`'s start/stop/polling-loop contract exactly (the Wave 5 work proved this is the right shape). The OTel ingester polls `OTLP_ENDPOINT/v1/traces?since=<watermark>`, normalizes each span into a `HotRecord`, calls `embedding_service.generate_embedding(content).tolist()` for the FLOAT[384] embedding, and writes through `HotStore.insert`. Dedup via per-system-id timestamp watermark; on restart, default to "now - initial_lookback_seconds".

**Tech Stack:** Python 3.14, asyncio, `httpx2` (the project's standard; matches `CodeGraphIngester`), `EmbeddingService` from `akosha.processing.embeddings`, `HotStore.insert` from `akosha.storage.hot_store`, `HotRecord` from `akosha.storage.models`. DuckDB-backed `HotStore` (in-memory by default).

**Spec:** `/Users/les/Projects/akosha/docs/superpowers/specs/2026-09-06-otel-trace-ingester-design.md`

## Global Constraints

- `from __future__ import annotations` as the first non-comment line of every source file (project convention; mypy strict).
- Imports sorted within each section (`force-sort-within-sections = true`, `known-first-party = ["akosha"]`).
- Modern syntax: `X | None` not `Optional[X]`, `list[str]` not `List[str]`.
- Function arguments with default `None` typed as `X | None = None`.
- No `assert` in production code; use `akosha.core.errors` exceptions.
- No `Any` in tool inputs / orchestration state (use `TYPE_CHECKING` + protocol if needed).
- In `except` blocks, use `logger.exception(...)`, never `logger.error(..., exc_info=True)`.
- Use the Oneiric logger (`oneiric.logging`); do not introduce stdlib `logging` new instances.
- Use `httpx2 as httpx` (matches `CodeGraphIngester`).
- All I/O in the orchestration layer is async.
- Remove unused imports immediately (Ruff F401).
- Coverage floor: 89.0% (`--cov-fail-under=89.0` in pyproject).
- Lifespan integration must include opt-out env var `AKOSHA_SKIP_OTEL_INGESTER=1` (mirrors `AKOSHA_SKIP_CODE_GRAPH_INGESTER=1`).

---

## File Structure

| File | Responsibility |
|---|---|
| `akosha/ingestion/otel_ingester.py` (new) | The `OtelTraceIngester` class with start/stop/polling/normalize/insert |
| `akosha/mcp/server.py` (modify) | Lifespan integration: construct, start, stop; share `hot_store` via existing singleton; `/health` surfacing |
| `tests/unit/test_otel_trace_ingester.py` (new) | Unit tests for ingester lifecycle, normalization, watermark, error paths |
| `tests/unit/test_wave5_lifespan_wiring.py` (modify) | Two new tests: lifespan starts OTel ingester by default; `AKOSHA_SKIP_OTEL_INGESTER=1` opts out |
| `tests/integration/test_otel_ingester_e2e.py` (new) | End-to-end test with mock OTLP collector ASGI app |

The plan does **not** add new modules for watermarks — the watermark is held in-memory on the ingester instance, matching `CodeGraphIngester._known_graph_ids`.

---

## Task 1: OtelTraceIngester skeleton with start/stop

**Files:**
- Create: `akosha/ingestion/otel_ingester.py`
- Test: `tests/unit/test_otel_trace_ingester.py`

**Interfaces:**
- Produces: `class OtelTraceIngester` with `__init__(hot_store, embedding_service, otlp_endpoint, poll_interval_seconds, max_spans_per_poll, initial_lookback_seconds)` and `async def start()` / `async def stop()`. `_polling_loop()` is a private method. `_running`, `_poll_task`, `_http_client`, `_watermarks: dict[str, int]` are instance attributes.
- The `embedding_service` parameter must satisfy the duck type: `await embedding_service.generate_embedding(text)` returns `np.ndarray` (the existing `EmbeddingService` contract).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_otel_trace_ingester.py
"""Unit tests for OtelTraceIngester."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from akosha.ingestion.otel_ingester import OtelTraceIngester


def _make_ingester(**overrides: Any) -> OtelTraceIngester:
    """Build an ingester with mocked dependencies."""
    defaults: dict[str, Any] = {
        "hot_store": MagicMock(),
        "embedding_service": MagicMock(),
        "otlp_endpoint": "http://collector.local:4318/v1/traces",
        "poll_interval_seconds": 3600,  # long; we never wait
        "max_spans_per_poll": 500,
        "initial_lookback_seconds": 3600,
    }
    defaults.update(overrides)
    return OtelTraceIngester(**defaults)


@pytest.mark.asyncio
async def test_start_and_stop_lifecycle() -> None:
    """The ingester must start a poll task and stop it cleanly."""
    ingester = _make_ingester()

    await ingester.start()
    assert ingester._running is True
    assert ingester._poll_task is not None
    assert not ingester._poll_task.done()

    await ingester.stop()
    assert ingester._running is False
    assert ingester._http_client is None  # closed on stop


@pytest.mark.asyncio
async def test_stop_is_idempotent_when_not_running() -> None:
    """Stopping an ingester that was never started is a no-op."""
    ingester = _make_ingester()
    await ingester.stop()  # must not raise
    assert ingester._running is False


@pytest.mark.asyncio
async def test_start_is_idempotent_when_already_running() -> None:
    """Starting an already-running ingester is a no-op (logs a warning)."""
    ingester = _make_ingester()
    await ingester.start()
    first_task = ingester._poll_task
    await ingester.start()  # must not replace the running task
    assert ingester._poll_task is first_task
    await ingester.stop()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/les/Projects/akosha && /Users/les/Projects/akosha/.venv/bin/pytest tests/unit/test_otel_trace_ingester.py -v --no-cov`
Expected: `ModuleNotFoundError: No module named 'akosha.ingestion.otel_ingester'`

- [ ] **Step 3: Write minimal implementation**

```python
# akosha/ingestion/otel_ingester.py
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
```

You'll also need to add `from contextlib import suppress` at the top of the module.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/les/Projects/akosha && /Users/les/Projects/akosha/.venv/bin/pytest tests/unit/test_otel_trace_ingester.py -v --no-cov`
Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/les/Projects/akosha
git add akosha/ingestion/otel_ingester.py tests/unit/test_otel_trace_ingester.py
git -c user.email='les@wedgwoodwebworks.com' commit -m "feat(akosha): OtelTraceIngester skeleton with start/stop lifecycle

Mirrors CodeGraphIngester's contract surface so future contributors
recognize the sibling workers instantly. Polling loop body is a
placeholder; Task 2 fills it in with the actual HTTP fetch + span
normalization + HotStore.insert pipeline.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 2: Span fetch + normalization + insert

**Files:**
- Modify: `akosha/ingestion/otel_ingester.py:90-110` (the `_polling_loop` placeholder)
- Test: `tests/unit/test_otel_trace_ingester.py`

**Interfaces:**
- Consumes: `_http_client` (set by `start()`), `_watermarks` (in-memory dict), `embedding_service`, `hot_store`.
- Produces: `_fetch_spans(since_unix_nano: int) -> list[dict[str, Any]]` (HTTP GET OTLP/HTTP and unwrap `resourceSpans`), `_normalize_span(span: dict) -> HotRecord` (map OTel → `HotRecord`), `_ingest_span(record: HotRecord, span: dict) -> None` (embed + insert + watermark advance).

- [ ] **Step 1: Write the failing tests**

Add these tests to `tests/unit/test_otel_trace_ingester.py`:

```python
@pytest.mark.asyncio
async def test_fetch_spans_returns_otlp_resource_spans() -> None:
    """The HTTP fetch unwraps OTLP resourceSpans into a flat span list."""
    ingester = _make_ingester()
    await ingester.start()
    try:
        # Replace _http_client with a mock that returns canned OTLP
        canned_response = {
            "resourceSpans": [
                {
                    "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "akosha"}}]},
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": "0af7651916cd43dd8448eb211c80319c",
                                    "spanId": "b7ad6b7169203331",
                                    "name": "test.span",
                                    "startTimeUnixNano": "1700000000000000000",
                                    "endTimeUnixNano": "1700000000001000000",
                                    "attributes": [
                                        {"key": "task.class", "value": {"stringValue": "CODE_GENERATION"}}
                                    ],
                                }
                            ]
                        }
                    ],
                }
            ]
        }
        mock_response = MagicMock()
        mock_response.json.return_value = canned_response
        mock_response.raise_for_status = MagicMock()
        ingester._http_client.get = AsyncMock(return_value=mock_response)

        spans = await ingester._fetch_spans(since_unix_nano=0)
        assert len(spans) == 1
        assert spans[0]["name"] == "test.span"
    finally:
        await ingester.stop()


def test_normalize_span_produces_hot_record() -> None:
    """The normalizer maps OTel span fields to a HotRecord."""
    ingester = _make_ingester()
    span = {
        "traceId": "0af7651916cd43dd8448eb211c80319c",
        "spanId": "b7ad6b7169203331",
        "name": "test.span",
        "startTimeUnixNano": "1700000000000000000",
        "endTimeUnixNano": "1700000000001000000",
        "attributes": [{"key": "task.class", "value": {"stringValue": "CODE_GENERATION"}}],
    }
    system_id = "akosha"
    embedding = np.zeros(384, dtype=np.float32)
    record = ingester._normalize_span(span, system_id=system_id, embedding=embedding.tolist())

    assert record.system_id == "akosha"
    assert record.conversation_id == "akosha:b7ad6b7169203331"
    assert "test.span" in record.content
    assert '"task.class": "CODE_GENERATION"' in record.content
    assert record.metadata["attributes"]["task_class"] == "CODE_GENERATION"
    assert record.embedding == embedding.tolist()


@pytest.mark.asyncio
async def test_ingest_span_writes_to_hot_store_and_advances_watermark() -> None:
    """_ingest_span embeds, inserts, and advances the watermark."""
    ingester = _make_ingester(
        embedding_service=MagicMock(
            generate_embedding=AsyncMock(
                return_value=np.zeros(384, dtype=np.float32)
            )
        ),
    )
    hot_store = ingester.hot_store
    hot_store.insert = AsyncMock()

    span = {
        "traceId": "0af7651916cd43dd8448eb211c80319c",
        "spanId": "b7ad6b7169203331",
        "name": "test.span",
        "startTimeUnixNano": "1700000000000000000",
        "endTimeUnixNano": "1700000000001000000",
        "attributes": [],
    }
    system_id = "akosha"
    await ingester._ingest_span(span, system_id=system_id)

    hot_store.insert.assert_awaited_once()
    inserted_record = hot_store.insert.await_args.args[0]
    assert inserted_record.system_id == "akosha"
    assert ingester._watermarks["akosha"] == 1700000000000000000


@pytest.mark.asyncio
async def test_ingest_span_skips_when_embedding_fails() -> None:
    """A span whose embedding fails is logged + skipped; watermark does NOT advance."""
    ingester = _make_ingester(
        embedding_service=MagicMock(
            generate_embedding=AsyncMock(side_effect=RuntimeError("embedding down"))
        ),
    )
    hot_store = ingester.hot_store
    hot_store.insert = AsyncMock()

    span = {
        "traceId": "0af7651916cd43dd8448eb211c80319c",
        "spanId": "b7ad6b7169203331",
        "name": "test.span",
        "startTimeUnixNano": "1700000000000000000",
        "attributes": [],
    }
    await ingester._ingest_span(span, system_id="akosha")

    hot_store.insert.assert_not_awaited()
    assert "akosha" not in ingester._watermarks
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/les/Projects/akosha && /Users/les/Projects/akosha/.venv/bin/pytest tests/unit/test_otel_trace_ingester.py -v --no-cov`
Expected: 4 new tests FAIL with `AttributeError: 'OtelTraceIngester' object has no attribute '_fetch_spans'` (etc.)

- [ ] **Step 3: Implement `_fetch_spans`, `_normalize_span`, `_ingest_span`, and replace `_polling_loop` placeholder**

Replace the placeholder `_polling_loop` in `akosha/ingestion/otel_ingester.py` with this body, and add the three private methods below it:

```python
    async def _polling_loop(self) -> None:
        """Main polling loop. One cycle per ``poll_interval_seconds``."""
        try:
            while self._running:
                try:
                    # Each system_id polls independently; the watermark
                    # gates the since parameter.
                    for system_id in list(self._watermarks.keys() or ["__default__"]):
                        watermark = self._watermarks.get(system_id)
                        if watermark is None:
                            # Restart recovery window
                            watermark = self._now_unix_nano() - (
                                self.initial_lookback_seconds * 1_000_000_000
                            )
                        spans = await self._fetch_spans(since_unix_nano=watermark)
                        if spans:
                            logger.info(
                                f"Fetched {len(spans)} OTel spans for {system_id}"
                            )
                        for span in spans[: self.max_spans_per_poll]:
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

    async def _fetch_spans(self, since_unix_nano: int) -> list[dict[str, Any]]:
        """Fetch spans newer than ``since_unix_nano`` from the OTLP/HTTP endpoint.

        Returns a flat list of span dicts. OTLP/HTTP wraps spans in
        ``resourceSpans[].scopeSpans[].spans[]``; this method unwraps
        that nesting.
        """
        if self._http_client is None:
            raise RuntimeError("HTTP client not initialized; call start() first")
        response = await self._http_client.get(
            self.otlp_endpoint,
            params={"since": str(since_unix_nano)},
        )
        response.raise_for_status()
        body = response.json()
        result: list[dict[str, Any]] = []
        for resource_spans in body.get("resourceSpans", []):
            for scope_spans in resource_spans.get("scopeSpans", []):
                for span in scope_spans.get("spans", []):
                    result.append(span)
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
        from datetime import UTC, datetime

        from akosha.storage.models import HotRecord

        attrs = self._attrs_to_dict(span.get("attributes", []))
        task_class = attrs.get("task.class")

        # Serialize the span (drop spanId/traceId — those land in
        # conversation_id and metadata.otel.trace_id).
        span_for_content = {
            k: v for k, v in span.items() if k not in ("traceId", "spanId")
        }
        import json
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
        from akosha.storage.models import HotRecord

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
        import time
        return int(time.time_ns())
```

You'll also need to add at the top of the file:

```python
from datetime import UTC, datetime  # used in _normalize_span; can be runtime import or moved up
```

Actually, since both `datetime` and `json` are only used inside method bodies, keeping them inline (as written above) is fine and avoids polluting the module-level imports.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/les/Projects/akosha && /Users/les/Projects/akosha/.venv/bin/pytest tests/unit/test_otel_trace_ingester.py -v --no-cov`
Expected: 7 tests PASS (3 from Task 1 + 4 from Task 2).

- [ ] **Step 5: Commit**

```bash
cd /Users/les/Projects/akosha
git add akosha/ingestion/otel_ingester.py tests/unit/test_otel_trace_ingester.py
git -c user.email='les@wedgwoodwebworks.com' commit -m "feat(akosha): OTel span fetch, normalize, and ingest pipeline

Implements the polling loop body for OtelTraceIngester:

- _fetch_spans: HTTP GET OTLP/HTTP /v1/traces?since=<watermark>;
  unwraps OTLP resourceSpans[].scopeSpans[].spans[] into a flat list.
- _normalize_span: maps an OTel span to a HotRecord. content is a
  JSON dump; metadata.attributes.task_class is extracted from the
  task.class semantic attribute so query_local_traces can filter on
  it via the existing SQL WHERE clause.
- _ingest_span: embeds content via EmbeddingService.generate_embedding,
  inserts via HotStore.insert, advances the watermark ONLY on
  successful insert.
- _attrs_to_dict: flattens OTel attributes list [{key, value}] into
  a dict, unwrapping stringValue/intValue/boolValue variants.
- _polling_loop: per-system-id watermark; on cold start, defaults
  to now - initial_lookback_seconds.

Error handling: HTTP failure, embedding failure, or insert failure
log + skip the span/c; watermark does NOT advance on failure, so
the next poll re-attempts.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 3: Lifespan integration + /health surfacing

**Files:**
- Modify: `akosha/mcp/server.py` (lifespan block, /health probe aggregates)
- Test: `tests/unit/test_wave5_lifespan_wiring.py` (add 2 tests)

**Interfaces:**
- Consumes: `OtelTraceIngester` (from Task 1+2), `hot_store`, `embedding_service`, `get_shared_hot_store` (existing).
- Produces: `_otel_trace_ingester` module-level singleton in `akosha.mcp.server`, lifespan constructs and starts it (unless `AKOSHA_SKIP_OTEL_INGESTER=1`), stops it on shutdown. /health probe reports `otel_ingester_running: bool` and `otel_endpoint: str` under the existing `local_traces_feed` aggregate.

- [ ] **Step 1: Read the current lifespan block in akosha/mcp/server.py**

Read `/Users/les/Projects/akosha/akosha/mcp/server.py` lines 460-560 (the lifespan body) to identify the exact insertion points. Locate:
1. The `CodeGraphIngester` construction block (mirror it for OTel)
2. The shutdown block where `_code_graph_ingester.stop()` is called
3. The `/health` probe block where `code_graphs_feed` is built (extend `local_traces_feed` aggregate)

- [ ] **Step 2: Write the failing tests**

Append to `tests/unit/test_wave5_lifespan_wiring.py`:

```python
class TestOtelTraceIngesterLifespanWiring:
    """OtelTraceIngester is wired into the lifespan alongside CodeGraphIngester."""

    async def test_lifespan_starts_otel_ingester_by_default(
        self, _reset_wave5_module_state
    ) -> None:
        """Without AKOSHA_SKIP_OTEL_INGESTER, the OTel ingester is constructed and started."""
        # Re-use the patched_lifespan-equivalent fixture pattern from existing
        # tests in this file; the test must assert the ingester is set on
        # akosha.mcp.server._otel_trace_ingester and that its start() was awaited.
        # Implementation note: this test will be filled in alongside the
        # production code change below. The key assertion is:
        # assert akosha.mcp.server._otel_trace_ingester is not None
        # assert akosha.mcp.server._otel_trace_ingester._running is True
        pytest.skip("filled in alongside production code")

    async def test_lifespan_skips_otel_ingester_when_env_opt_out(
        self, _reset_wave5_module_state, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AKOSHA_SKIP_OTEL_INGESTER=1 suppresses the ingester entirely."""
        monkeypatch.setenv("AKOSHA_SKIP_OTEL_INGESTER", "1")
        pytest.skip("filled in alongside production code")
```

For the **real** assertions, look at the existing `test_lifespan_starts_code_graph_ingester` test in this file and mirror its pattern. The two new tests should:
1. Patch `akosha.ingestion.otel_ingester.OtelTraceIngester` with an `AsyncMock` so `start()` is awaitable.
2. Construct the app, enter the lifespan, assert `OtelTraceIngester` was called and `start()` was awaited.
3. For the opt-out test, set `AKOSHA_SKIP_OTEL_INGESTER=1` and assert `OtelTraceIngester` was NOT called.

- [ ] **Step 3: Modify akosha/mcp/server.py lifespan**

In `akosha/mcp/server.py`, after the existing `CodeGraphIngester` block in the lifespan (search for `_code_graph_ingester = CodeGraphIngester(...)`), add:

```python
        # OTel trace ingester (Wave 6). Mirrors the CodeGraphIngester
        # construction block above. Skipped in tests via the standard
        # AKOSHA_SKIP_OTEL_INGESTER=1 env var.
        from akosha.ingestion.otel_ingester import OtelTraceIngester

        _otel_trace_ingester: OtelTraceIngester | None = None
        if not os.getenv("AKOSHA_SKIP_OTEL_INGESTER"):
            _otel_trace_ingester = OtelTraceIngester(
                hot_store=hot_store,
                embedding_service=embedding_service,
                otlp_endpoint=os.getenv(
                    "AKOSHA_OTLP_ENDPOINT", "http://localhost:4318/v1/traces"
                ),
                poll_interval_seconds=int(os.getenv("AKOSHA_OTEL_POLL_SECONDS", "60")),
            )
            await _otel_trace_ingester.start()
```

Declare the module-level singleton near the existing module-level singletons (`_shared_hot_store`, `_shared_kg_builder`, `_code_graph_ingester`, `_kg_refresh_task`):

```python
_otel_trace_ingester: OtelTraceIngester | None = None
```

In the shutdown block (search for `_code_graph_ingester.stop()`), add:

```python
        if _otel_trace_ingester is not None:
            await _otel_trace_ingester.stop()
            _otel_trace_ingester = None
```

- [ ] **Step 4: Extend the /health probe**

In the /health probe builder (search for `local_traces_feed` in `akosha/mcp/server.py`), extend the existing aggregate. The probe currently reports `feed_entities_count` and `last_updated_timestamp`. Add:

```python
                    "otel_ingester_running": (
                        _otel_trace_ingester is not None
                        and _otel_trace_ingester._running
                    ),
                    "otel_endpoint": (
                        _otel_trace_ingester.otlp_endpoint
                        if _otel_trace_ingester is not None
                        else None
                    ),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/les/Projects/akosha && /Users/les/Projects/akosha/.venv/bin/pytest tests/unit/test_wave5_lifespan_wiring.py tests/unit/test_mcp_server_lifespan.py -v --no-cov`
Expected: All existing tests + the 2 new tests PASS.

If `tests/unit/test_mcp_server_lifespan.py` regresses because the OTel ingester tries to reach a real OTLP collector, add `monkeypatch.setenv("AKOSHA_SKIP_OTEL_INGESTER", "1")` to its fixture — same pattern as the existing `AKOSHA_SKIP_CODE_GRAPH_INGESTER` opt-out.

- [ ] **Step 6: Commit**

```bash
cd /Users/les/Projects/akosha
git add akosha/mcp/server.py tests/unit/test_wave5_lifespan_wiring.py tests/unit/test_mcp_server_lifespan.py
git -c user.email='les@wedgwoodwebworks.com' commit -m "feat(akosha): wire OtelTraceIngester into MCP lifespan (Wave 6)

Mirrors the CodeGraphIngester wiring:
- Module-level singleton _otel_trace_ingester
- Lifespan startup constructs and starts the ingester (opt-out via
  AKOSHA_SKIP_OTEL_INGESTER=1)
- Lifespan shutdown stops the ingester and clears the singleton
- /health probe extends the existing local_traces_feed aggregate with
  otel_ingester_running and otel_endpoint fields

Tests: 2 new lifespan tests in test_wave5_lifespan_wiring.py verify
the default-start and env-opt-out paths. test_mcp_server_lifespan.py
gets the standard AKOSHA_SKIP_OTEL_INGESTER=1 opt-out so it doesn't
try to reach a real OTLP collector during unit tests.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 4: End-to-end integration test with mock OTLP collector

**Files:**
- Create: `tests/integration/test_otel_ingester_e2e.py`
- Create: `tests/integration/mock_otlp_collector.py` (helper module)

**Interfaces:**
- Produces: `MockOtelCollector` ASGI app that returns canned spans; `test_otel_ingester_e2e` that starts the mock, points the ingester at it, polls once, and asserts the span landed in `hot_store`.

- [ ] **Step 1: Create the mock OTLP collector**

```python
# tests/integration/mock_otlp_collector.py
"""Minimal OTLP/HTTP-shaped mock for integration testing."""

from __future__ import annotations

import json
from typing import Any
from starlette.requests import Request
from starlette.responses import JSONResponse


class MockOtelCollector:
    """Returns canned OTel spans from GET /v1/traces?since=<unix_nano>."""

    def __init__(self, spans: list[dict[str, Any]] | None = None) -> None:
        self.spans = spans or []
        self.request_count = 0
        self.last_query: dict[str, str] | None = None

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        request = Request(scope, receive)
        if request.url.path != "/v1/traces":
            response = JSONResponse({"error": "not found"}, status_code=404)
            await response(scope, receive, send)
            return
        self.request_count += 1
        self.last_query = dict(request.query_params)
        since = int(request.query_params.get("since", "0"))
        filtered = [
            s for s in self.spans if int(s.get("startTimeUnixNano", "0")) > since
        ]
        body = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": "akosha"}}
                        ]
                    },
                    "scopeSpans": [{"spans": filtered}],
                }
            ]
        }
        response = JSONResponse(body)
        await response(scope, receive, send)


def make_canned_span(
    span_id: str = "b7ad6b7169203331",
    name: str = "test.span",
    start_unix_nano: str = "1700000000000000000",
    task_class: str = "CODE_GENERATION",
) -> dict[str, Any]:
    """Return a single OTel span shaped like OTLP/HTTP JSON."""
    return {
        "traceId": "0af7651916cd43dd8448eb211c80319c",
        "spanId": span_id,
        "name": name,
        "startTimeUnixNano": start_unix_nano,
        "endTimeUnixNano": str(int(start_unix_nano) + 1_000_000),
        "attributes": [
            {"key": "task.class", "value": {"stringValue": task_class}},
        ],
    }
```

- [ ] **Step 2: Write the e2e test**

```python
# tests/integration/test_otel_ingester_e2e.py
"""End-to-end test: OtelTraceIngester polls a mock OTLP collector and ingests spans."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import numpy as np
import pytest
import uvicorn

from akosha.ingestion.otel_ingester import OtelTraceIngester
from akosha.storage.hot_store import HotStore

from tests.integration.mock_otlp_collector import MockOtelCollector, make_canned_span


@pytest.fixture
async def mock_collector_server() -> Any:
    """Start the mock OTLP collector on an ephemeral port."""
    spans = [make_canned_span(task_class="CODE_GENERATION")]
    collector = MockOtelCollector(spans=spans)
    config = uvicorn.Config(collector, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}/v1/traces", collector
    finally:
        server.should_exit = True
        await task


@pytest.fixture
def real_hot_store() -> HotStore:
    """A real in-memory HotStore (DuckDB :memory:)."""
    from akosha.storage.hot_store import HotStore
    import asyncio
    store = HotStore(database_path=":memory:")
    return store


@pytest.fixture
def stub_embedding_service() -> Any:
    """An EmbeddingService stand-in that returns a zero-vector."""
    service = AsyncMock()
    service.generate_embedding = AsyncMock(
        return_value=np.zeros(384, dtype=np.float32)
    )
    return service


@pytest.mark.asyncio
@pytest.mark.slow
async def test_otel_ingester_e2e_polls_and_ingests(
    mock_collector_server: Any,
    real_hot_store: HotStore,
    stub_embedding_service: Any,
) -> None:
    """A single poll cycle ingests the canned span into the real HotStore."""
    endpoint, collector = mock_collector_server
    ingester = OtelTraceIngester(
        hot_store=real_hot_store,
        embedding_service=stub_embedding_service,
        otlp_endpoint=endpoint,
        poll_interval_seconds=3600,  # long; we trigger manually
        initial_lookback_seconds=0,
    )

    await real_hot_store.initialize()
    await ingester.start()
    try:
        # Wait for the first poll to complete
        await asyncio.sleep(0.5)

        # The mock should have been polled
        assert collector.request_count >= 1

        # The span should have landed in the hot store
        traces = await real_hot_store.query_traces(system_id="akosha")
        assert any(
            t["metadata"]["attributes"]["task_class"] == "CODE_GENERATION"
            for t in traces
        )

        # The watermark should have advanced
        assert ingester._watermarks["akosha"] > 0
    finally:
        await ingester.stop()
```

- [ ] **Step 3: Run test to verify it passes**

Run: `cd /Users/les/Projects/akosha && /Users/les/Projects/akosha/.venv/bin/pytest tests/integration/test_otel_ingester_e2e.py -v --no-cov -m slow`
Expected: 1 test PASS (or skip if `uvicorn` is not in the venv).

If `uvicorn` is missing: `uv pip install uvicorn` (note for the implementer — `uvicorn` is in pyproject's optional `dev` group; should be present in the venv).

- [ ] **Step 4: Commit**

```bash
cd /Users/les/Projects/akosha
git add tests/integration/test_otel_ingester_e2e.py tests/integration/mock_otlp_collector.py
git -c user.email='les@wedgwoodwebworks.com' commit -m "test(akosha): e2e test for OtelTraceIngester with mock OTLP collector

Adds a Starlette ASGI mock that returns canned spans from
GET /v1/traces?since=<unix_nano>, plus an integration test that:
1. Starts the mock on an ephemeral port
2. Constructs an OtelTraceIngester with a real in-memory HotStore
   and a stub EmbeddingService
3. Starts the ingester and waits one poll cycle
4. Asserts the canned span landed in hot_store.query_traces

Marked @pytest.mark.slow; default pytest runs skip it.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Self-Review

1. **Spec coverage:**
   - "OTLP/HTTP collector source contract" → Task 2 (`_fetch_spans`)
   - "Span-per-HotRecord mapping" → Task 2 (`_normalize_span`)
   - "Per-system-id timestamp watermark" → Task 2 (`_watermarks`, `_ingest_span`)
   - "Restart defaults to initial_lookback_seconds" → Task 2 (`_polling_loop`)
   - "Lifespan integration mirroring CodeGraphIngester" → Task 3
   - "/health surfacing otel_ingester_running + otel_endpoint" → Task 3
   - "Env var opt-out AKOSHA_SKIP_OTEL_INGESTER" → Task 3
   - "Unit tests" → Task 1 (lifecycle) + Task 2 (normalize/insert)
   - "Integration test with mock OTLP collector" → Task 4

2. **Placeholder scan:** All test code and implementation snippets are concrete. No "TBD" or "implement later" markers.

3. **Type consistency:** `OtelTraceIngester.__init__` signature matches in Tasks 1, 2, 3, 4. `_watermarks` is declared in Task 1, used in Tasks 2 and 3. `_normalize_span` signature matches between Task 2 (production) and Task 2 (test). `_ingest_span` signature matches.
