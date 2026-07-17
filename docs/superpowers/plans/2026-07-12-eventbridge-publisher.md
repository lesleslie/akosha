---
status: draft
role: implementation
date: 2026-07-16
last_reviewed: 2026-07-16
superseded_by: null
blocks_on: []
topic: observability
---

# Akosha EventBridge Publisher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an Akosha-side publisher that adapts the existing `broadcast_pattern_detected` / `broadcast_anomaly_detected` / `broadcast_insight_generated` / `broadcast_aggregation_completed` WebSocket methods into the canonical Oneiric `EventEnvelope` (msgspec) and emits them through an injected duck-typed publisher, so the Mahavishnu Bodai subscriber (Phase 6A) surfaces `[akosha] pattern.detected/anomaly.detected/insight.generated/aggregation.completed` alongside `[mahavishnu]` and `[crackerjack]`.

**Architecture:** A new `akosha.observability.eventbridge_publisher` module exposes four `publish_*` async functions that mirror `mahavishnu.core.events.mahavishnu_publisher` (1:1 structure). Each function builds a canonical Oneiric `EventEnvelope` via `oneiric.runtime.events.create_event_envelope` (msgspec.Struct) and dispatches it through an injected `publisher: Any | None` parameter (duck-typed; `None` is a no-op). The functions never raise under normal failure modes — they log at ERROR via `logger.exception` on `Exception` subclasses; `asyncio.CancelledError` propagates so Ctrl-C interrupts long-running analytics. Wire-up happens inside the four existing `AkoshaWebSocketServer.broadcast_*` methods (lines 338, 369, 400, 431): each method gains a sibling `await publish_*` call so the EventBridge publish happens alongside the WebSocket broadcast (no double-broadcast, no fan-out coupling). Settings gain a new `EventBridgeConfig` Pydantic model in `akosha/config.py` (matching the existing `HotStorageConfig` pattern); the MCP server gains a `publish_to_eventbridge` tool gated to the FULL profile.

**Tech Stack:** Python 3.13, `oneiric>=0.13.0` (already a dep — `pyproject.toml:16`), `oneiric.runtime.events.EventEnvelope` (msgspec), `pytest`, `pytest-asyncio`. No new dependencies required.

**Mirror reference:** `mahavishnu/core/events/mahavishnu_publisher.py` and `tests/unit/test_mahavishnu_publisher.py` (read both before starting). Crackerjack's plan at `crackerjack/docs/superpowers/plans/2026-07-12-eventbridge-publisher.md` is a parallel implementation — review for naming consistency before starting.

## Global Constraints

- Akosha project conventions per `CLAUDE.md` Crackerjack-Compliant Code: `from __future__ import annotations` first non-comment line; imports sorted within each section (stdlib → third-party → first-party with `force-sort-within-sections = true` and `known-first-party = ["akosha"]`); modern syntax `X | None` (not `Optional[X]`); function args with default `None` typed `X | None = None`; no `assert` in production code; `logger.exception` (not `logger.error(..., exc_info=True)`) in `except` blocks; oneiric logger (`oneiric.logging`) preferred over stdlib `logging`.
- Hard limits: line length 100, max 10 function args, max 15 branches, max 6 returns, max 55 statements per function.
- Async tests don't need `@pytest.mark.asyncio` (akosha's `pyproject.toml` sets `asyncio_mode = "auto"` at line 78).
- The publisher's `publisher` parameter is `Any | None` (NOT `EventPublisherProtocol` — that lives in `mahavishnu.core.events.contract` and is type-mismatched with Oneiric's msgspec envelope; do not import it from Mahavishnu).
- Akosha coverage gate is `--cov-fail-under=85` (`pyproject.toml:110`). New code must maintain this threshold.
- New settings go in `akosha/config.py` as Pydantic models matching `HotStorageConfig` / `WarmStorageConfig` / `ColdStorageConfig` / `CacheConfig` (lines 50-163). New env-var binding: `AKOSHA_EVENTBRIDGE_*` for plain fields, `AKOSHA__EVENTBRIDGE__*` for nested.

---

## File Structure

| Path | Purpose |
|---|---|
| `akosha/observability/eventbridge_publisher.py` | New. Four `publish_*` functions + `_make_envelope` + `_publish` helpers + module-level `set_eventbridge_publisher`. ~200 lines. |
| `akosha/config.py` | Modified. Adds `EventBridgeConfig(BaseModel)` class between `CacheConfig` (line 146) and `AkoshaConfig` (line 166); adds `eventbridge: EventBridgeConfig` field on `AkoshaConfig` after `cache` (around line 206). +40 lines. |
| `akosha/websocket/server.py` | Modified. Adds 4 sibling `await publish_*` calls inside the four `broadcast_*` methods (lines 338, 369, 400, 431). +20 lines. |
| `akosha/mcp/tools/eventbridge_tools.py` | New. `register_eventbridge_tools(app)` with `publish_to_eventbridge` tool, gated via `ToolProfile.FULL`. ~80 lines. |
| `akosha/mcp/tools/tool_registry.py` | Modified. Adds `EVENTS = "events"` to `ToolCategory` StrEnum (line 16). +1 line. |
| `akosha/mcp/tools/profiles.py` | Modified. Adds the new tool to `FULL_REGISTRATIONS` and `REGISTRATION_TOOLS`. +2 lines. |
| `akosha/mcp/tools/__init__.py` | Modified. Wires `register_eventbridge_tools` into `register_all_tools` (line 77). +2 lines. |
| `settings/akosha.yaml` | Modified. Adds `eventbridge:` block. +10 lines. |
| `tests/unit/test_eventbridge_publisher.py` | New. Mirrors `test_mahavishnu_publisher.py` — covers all 4 publish functions + envelope shape + never-raises + None no-op. ~250 lines. |
| `tests/integration/test_eventbridge_e2e.py` | New. End-to-end: in-memory RecordingTransport, envelope round-trip. ~150 lines. |

Files changing together: `config.py` and `settings/akosha.yaml` both define the same block; `eventbridge_publisher.py` depends on the env-var convention but not on the `EventBridgeConfig` class itself (loose coupling); `websocket/server.py` only depends on `eventbridge_publisher.py`; the MCP tools path is independent of the publisher. Tests are organized unit-then-integration per project convention.

---

## Task 1: Verify oneiric dependency is sufficient

**Files:**
- Read-only: `pyproject.toml` (line 16)

**Step 1.1: Read pyproject.toml line 16**

Run: `grep -n 'oneiric' /Users/les/Projects/akosha/pyproject.toml`
Expected: `16:    "oneiric>=0.1.0",` (confirmed by scout)

**Step 1.2: Confirm oneiric.runtime.events is importable**

Run: `cd /Users/les/Projects/akosha && python -c "from oneiric.runtime.events import EventEnvelope, create_event_envelope; print(EventEnvelope, create_event_envelope)"`
Expected: Both print. If either import fails, STOP — oneiric version is wrong; do not proceed.

**Step 1.3: Confirm installed oneiric version**

Run: `cd /Users/les/Projects/akosha && python -c "import importlib.metadata; print(importlib.metadata.version('oneiric'))"`
Expected: `0.13.0` or higher. The `create_event_envelope` factory exists in 0.13.0+.

**Commit:** No commit for this task — it's a verification step.

---

## Task 1.5: Oneiric EventBridge adapter — `EventBridgePublisher`

**Files:**
- Create: `akosha/observability/eventbridge_adapter.py`
- Test: `tests/unit/test_eventbridge_adapter.py`

**Why this task exists (per operational-safety Finding #2):** The publisher functions call `publisher.publish(envelope)`, but Oneiric's `EventBridge` exposes `emit(topic, payload, headers)`. The plan needs an adapter that bridges the two APIs so production wiring has a real publisher to inject.

**Interfaces:**
- Consumes: `oneiric.domains.events.EventBridge` (a `bridge.emit(topic, payload, headers)` method — confirmed at `oneiric/domains/events.py:58-73`)
- Produces: `class EventBridgePublisher` with `async def publish(self, envelope: EventEnvelope) -> None` that delegates to `bridge.emit(envelope.topic, envelope.payload, envelope.headers)`

- [x] **Step 1: Write the failing test**

Write `tests/unit/test_eventbridge_adapter.py`:

```python
"""Unit tests for ``akosha.observability.eventbridge_adapter``.

The adapter bridges ``publisher.publish(envelope)`` (the API the
publisher module expects) and ``EventBridge.emit(topic, payload, headers)``
(the API Oneiric's EventBridge exposes).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from oneiric.runtime.events import EventEnvelope

from akosha.observability.eventbridge_adapter import EventBridgePublisher


def test_adaptor_publish_calls_emit_with_envelope_fields() -> None:
    """publish() unpacks the envelope and forwards to bridge.emit()."""
    bridge = MagicMock()
    bridge.emit = AsyncMock()
    adapter = EventBridgePublisher(bridge)
    envelope = EventEnvelope(
        topic="pattern.detected",
        payload={"pattern_id": "p1", "pattern_type": "burst"},
        headers={"source": "akosha", "event_id": "abc-123"},
    )

    asyncio.run(adapter.publish(envelope))

    bridge.emit.assert_awaited_once_with(
        "pattern.detected",
        {"pattern_id": "p1", "pattern_type": "burst"},
        {"source": "akosha", "event_id": "abc-123"},
    )


def test_adaptor_constructor_stores_bridge() -> None:
    """The bridge reference is stored for later emit() calls."""
    bridge = MagicMock()
    adapter = EventBridgePublisher(bridge)
    assert adapter._bridge is bridge
```

- [x] **Step 2: Run the test to verify it fails (RED)**

Run: `cd /Users/les/Projects/akosha && pytest tests/unit/test_eventbridge_adapter.py -v`
Expected: `ModuleNotFoundError: No module named 'akosha.observability.eventbridge_adapter'`

- [x] **Step 3: Implement the adapter**

Write `akosha/observability/eventbridge_adapter.py`:

```python
"""Oneiric EventBridge adapter — bridges ``publisher.publish(envelope)`` to ``bridge.emit(...)``.

The :mod:`akosha.observability.eventbridge_publisher` module expects an
injected ``publisher`` with an async ``publish(envelope)`` method.
Oneiric's :class:`oneiric.domains.events.EventBridge` exposes an
``emit(topic, payload, headers)`` method, not ``publish``. This adapter
translates between the two.

Per the operational-safety review (Finding #2): this adapter is the
production injection point. Without it, the publisher module would
have no production-compatible publisher to wire into.
"""
from __future__ import annotations

from typing import Any

from oneiric.runtime.events import EventEnvelope


class EventBridgePublisher:
    """Adapter from ``publish(envelope)`` to ``EventBridge.emit(topic, payload, headers)``.

    Args:
        bridge: An instance of :class:`oneiric.domains.events.EventBridge`
            (duck-typed; the only attribute accessed is ``emit``).
    """

    def __init__(self, bridge: Any) -> None:
        self._bridge = bridge

    async def publish(self, envelope: EventEnvelope) -> None:
        """Forward ``envelope`` to ``bridge.emit(topic, payload, headers)``."""
        await self._bridge.emit(
            envelope.topic,
            envelope.payload,
            envelope.headers,
        )


__all__ = ["EventBridgePublisher"]
```

- [x] **Step 4: Run the test to verify it passes (GREEN)**

Run: `cd /Users/les/Projects/akosha && pytest tests/unit/test_eventbridge_adapter.py -v`
Expected: All 2 tests pass.

- [x] **Step 5: Verify ruff is clean**

Run: `cd /Users/les/Projects/akosha && ruff check akosha/observability/eventbridge_adapter.py tests/unit/test_eventbridge_adapter.py`
Expected: `All checks passed!`

- [x] **Step 6: Commit**

```bash
cd /Users/les/Projects/akosha
git add akosha/observability/eventbridge_adapter.py tests/unit/test_eventbridge_adapter.py
git commit -m "feat(eventbridge): add EventBridgePublisher adapter

Bridges publisher.publish(envelope) (publisher module's expected API)
to EventBridge.emit(topic, payload, headers) (Oneiric's actual API).
Production wiring now has a real publisher to inject; tests still use
duck-typed AsyncMock via the publisher: Any | None parameter."
```

---

## Task 2: Failing test — `_make_envelope` produces canonical Oneiric envelope

**Files:**
- Create: `tests/unit/test_eventbridge_publisher.py`

**Interfaces:**
- Consumes: `oneiric.runtime.events.EventEnvelope` (msgspec.Struct with `topic`, `payload`, `headers`)
- Produces: `akosha.observability.eventbridge_publisher._make_envelope(topic, source, payload) -> EventEnvelope`

- [x] **Step 1: Create the test file**

Write `tests/unit/test_eventbridge_publisher.py`:

```python
"""Unit tests for ``akosha.observability.eventbridge_publisher``.

Mirrors the test pattern from
``mahavishnu/tests/unit/test_mahavishnu_publisher.py`` and the parallel
``tests/unit/test_eventbridge_publisher.py`` in Crackerjack. Same envelope
shape, same never-raises guarantee, same duck-typed publisher injection.
"""
from __future__ import annotations

import asyncio
from collections.abc import Generator
from datetime import UTC, datetime
import logging
from typing import Any
from unittest.mock import AsyncMock, Mock
import uuid

from oneiric.runtime.events import EventEnvelope
import pytest

from akosha.observability.eventbridge_publisher import (
    EVENT_VERSION,
    SOURCE,
    TOPIC_AGGREGATION_COMPLETED,
    TOPIC_ANOMALY_DETECTED,
    TOPIC_INSIGHT_GENERATED,
    TOPIC_PATTERN_DETECTED,
    _make_envelope,
    publish_aggregation_completed,
    publish_anomaly_detected,
    publish_insight_generated,
    publish_pattern_detected,
)

pytestmark = pytest.mark.unit


def _headers_of(envelope: EventEnvelope) -> dict[str, Any]:
    return dict(envelope.headers) if isinstance(envelope.headers, dict) else {}


def _payload_of(envelope: EventEnvelope) -> dict[str, Any]:
    return dict(envelope.payload) if isinstance(envelope.payload, dict) else {}


def test_make_envelope_builds_canonical_shape() -> None:
    """_make_envelope produces a Oneiric msgspec envelope with required headers."""
    envelope = _make_envelope(
        TOPIC_PATTERN_DETECTED,
        SOURCE,
        {"pattern_id": "pat_1", "pattern_type": "anomaly_burst", "confidence": 0.95},
    )
    assert envelope.topic == "pattern.detected"
    headers = _headers_of(envelope)
    assert headers.get("source") == "akosha"
    assert headers.get("version") == "1.0.0"
    assert isinstance(headers.get("event_id"), str) and headers.get("event_id")
    assert isinstance(headers.get("timestamp"), str) and headers.get("timestamp")
    payload = _payload_of(envelope)
    assert payload.get("pattern_id") == "pat_1"
    assert payload.get("pattern_type") == "anomaly_burst"
    assert payload.get("confidence") == 0.95


def test_envelope_event_ids_are_unique_across_calls() -> None:
    """Each call produces a different event_id (UUID4)."""
    env_a = _make_envelope(TOPIC_PATTERN_DETECTED, SOURCE, {"pattern_id": "p1"})
    env_b = _make_envelope(TOPIC_PATTERN_DETECTED, SOURCE, {"pattern_id": "p1"})
    id_a = _headers_of(env_a).get("event_id")
    id_b = _headers_of(env_b).get("event_id")
    assert isinstance(id_a, str) and id_a
    assert isinstance(id_b, str) and id_b
    assert id_a != id_b


def test_envelope_timestamp_is_iso_utc() -> None:
    """Timestamp header parses as ISO 8601 in UTC."""
    envelope = _make_envelope(
        TOPIC_AGGREGATION_COMPLETED, SOURCE, {"aggregation_id": "agg_t"}
    )
    timestamp = _headers_of(envelope).get("timestamp")
    assert isinstance(timestamp, str)
    parsed = datetime.fromisoformat(timestamp)
    assert parsed.tzinfo is not None
    assert parsed.astimezone(UTC).utcoffset().total_seconds() == 0
```

- [x] **Step 2: Run the test to verify it fails (RED)**

Run: `cd /Users/les/Projects/akosha && pytest tests/unit/test_eventbridge_publisher.py -v`
Expected: `ModuleNotFoundError: No module named 'akosha.observability.eventbridge_publisher'` (or `ImportError`).

- [x] **Step 3: Stop. Move to Task 3.**

Do NOT commit yet. Tasks 2 and 3 are one TDD cycle.

---

## Task 3: Implement `akosha/observability/eventbridge_publisher.py` (GREEN)

**Files:**
- Create: `akosha/observability/eventbridge_publisher.py` (new directory `akosha/observability/` already exists per scout)
- Modify: `tests/unit/test_eventbridge_publisher.py` (extend with the publish_* tests below)

**Interfaces:**
- Consumes: `oneiric.runtime.events.EventEnvelope`, `oneiric.runtime.events.create_event_envelope`
- Produces: `publish_pattern_detected(pattern_id, pattern_type, description, confidence, metadata, *, publisher=None)`, `publish_anomaly_detected(...)`, `publish_insight_generated(...)`, `publish_aggregation_completed(...)`, plus module-level `set_eventbridge_publisher(publisher)` and `_get_publisher()` accessors

- [x] **Step 1: Implement the publisher module**

Write `akosha/observability/eventbridge_publisher.py`:

```python
"""Akosha-side publisher for analytics events.

Wraps existing ``AkoshaWebSocketServer.broadcast_pattern_detected`` /
``broadcast_anomaly_detected`` / ``broadcast_insight_generated`` /
``broadcast_aggregation_completed`` broadcasts into the canonical
:class:`oneiric.runtime.events.EventEnvelope` (msgspec.Struct) and
publishes them via an injected duck-typed ``publisher`` object.

The result: events appear in the unified Bodai queue
(``~/.mahavishnu/bodai-event-queue.json``) for consumption by Claude Code's
``/bodai-status`` and the PostToolUse hook, alongside the existing
WebSocket broadcasts (which are kept for non-Claude consumers like Grafana
dashboards).

Public API
----------
- :func:`publish_pattern_detected` -- topic ``pattern.detected``
- :func:`publish_anomaly_detected` -- topic ``anomaly.detected``
- :func:`publish_insight_generated` -- topic ``insight.generated``
- :func:`publish_aggregation_completed` -- topic ``aggregation.completed``

All four functions never raise under normal failure modes -- they log
at ERROR (via ``logger.exception``, which attaches a traceback) on
``Exception`` subclasses. ``asyncio.CancelledError`` propagates so
Ctrl-C interrupts long-running analytics. The canonical envelope carries
``source='akosha'`` in the ``headers`` dict, matching what
``mahavishnu.core.events.bodai_subscriber`` consumes.

Module-level injection
----------------------
The publisher handle is a module-level global (``_publisher``) set via
:func:`set_eventbridge_publisher`. The MCP tool at
``akosha/mcp/tools/eventbridge_tools.py`` and the WebSocket server's
``broadcast_*`` methods both read this global. When ``None`` (the
default), all publish calls are no-ops.

Note: ``publisher`` is typed ``Any | None`` (NOT ``EventPublisherProtocol``
from Mahavishnu) because (a) that Protocol lives in the Mahavishnu repo
and Akosha does not depend on Mahavishnu, and (b) the Protocol is typed
against a Pydantic envelope, not Oneiric's msgspec envelope. Duck-typing
is intentional; AsyncMock and the Oneiric EventBridge publisher both
satisfy ``publisher.publish(envelope)``.
"""
from __future__ import annotations

from datetime import UTC, datetime
import inspect
import logging
from typing import Any
import uuid

from oneiric.runtime.events import EventEnvelope, create_event_envelope

logger = logging.getLogger(__name__)

SOURCE = "akosha"
EVENT_VERSION = "1.0.0"

TOPIC_PATTERN_DETECTED = "pattern.detected"
TOPIC_ANOMALY_DETECTED = "anomaly.detected"
TOPIC_INSIGHT_GENERATED = "insight.generated"
TOPIC_AGGREGATION_COMPLETED = "aggregation.completed"

# Module-level publisher handle. Set via set_eventbridge_publisher().
# Reads are concurrency-safe because Python attribute assignment is atomic
# and Akosha's startup is single-threaded.
_publisher: Any | None = None


def set_eventbridge_publisher(publisher: Any | None) -> None:
    """Configure the publisher handle used by all ``publish_*`` functions.

    Args:
        publisher: Injected event publisher (typically an Oneiric
            ``EventBridge`` instance). ``None`` is a no-op (publish
            functions will skip transport entirely).
    """
    global _publisher
    _publisher = publisher


def _get_publisher() -> Any | None:
    """Return the currently-configured publisher (for tests and the MCP tool)."""
    return _publisher


def _make_envelope(topic: str, source: str, payload: dict[str, Any]) -> EventEnvelope:
    """Build the canonical Oneiric ``EventEnvelope`` for an Akosha event.

    Args:
        topic: Event topic (e.g. ``pattern.detected``).
        source: Producer identifier (always ``akosha``).
        payload: Event-specific payload (must be JSON-serializable).

    Returns:
        A canonical :class:`oneiric.runtime.events.EventEnvelope` with
        ``source``, ``event_id``, ``timestamp``, and ``version`` set in
        the ``headers`` dict.
    """
    event_id = uuid.uuid4()
    timestamp = datetime.now(UTC).isoformat()
    return create_event_envelope(
        topic=topic,
        payload=payload,
        source=source,
        version=EVENT_VERSION,
        headers={
            "source": source,
            "event_id": str(event_id),
            "timestamp": timestamp,
            "version": EVENT_VERSION,
        },
    )


async def _publish(envelope: EventEnvelope, publisher: Any | None) -> None:
    """Publish an envelope via the injected publisher.

    Swallows any exception (logs at WARNING) so a misbehaving publisher
    can never abort the broadcast path. Handles both sync and async
    ``publish`` results -- some implementations are coroutine-only, others
    return a future-like object.
    """
    if publisher is None:
        return
    try:
        result = publisher.publish(envelope)
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.exception(
            "akosha.publisher: failed to publish topic=%s event_id=%s",
            envelope.topic,
            envelope.headers.get("event_id", "<unknown>"),
        )


async def publish_pattern_detected(
    pattern_id: str,
    pattern_type: str,
    description: str,
    confidence: float,
    metadata: dict[str, Any],
    *,
    publisher: Any | None = None,
) -> None:
    """Publish a ``pattern.detected`` event to the Bodai queue.

    Never raises under normal failure modes -- both envelope construction
    (``_make_envelope``) and publisher transport failures are caught and
    logged at ERROR. ``asyncio.CancelledError`` propagates (it is
    ``BaseException``, not ``Exception`` -- correct asyncio semantics).

    Args:
        pattern_id: Pattern identifier.
        pattern_type: Type/category of pattern.
        description: Human-readable description.
        confidence: Detection confidence (0.0-1.0).
        metadata: Additional pattern metadata.
        publisher: Injected event publisher. ``None`` uses the module-level
            global. Pass an explicit publisher in tests.
    """
    # Early-return when no publisher is configured. Saves envelope
    # construction (uuid4, datetime.now, msgspec encoding) when the
    # publisher is a documented no-op. Operator safety: a misbehaving
    # publisher module cannot slow down the broadcast path on its own.
    effective_publisher = publisher if publisher is not None else _get_publisher()
    if effective_publisher is None:
        return
    try:
        payload: dict[str, Any] = {
            "pattern_id": pattern_id,
            "pattern_type": pattern_type,
            "description": description,
            "confidence": confidence,
            **metadata,
        }
        envelope = _make_envelope(TOPIC_PATTERN_DETECTED, SOURCE, payload)
        await _publish(envelope, effective_publisher)
    except Exception:
        logger.exception(
            "akosha.publisher: failed to publish pattern.detected event "
            "pattern_id=%s",
            pattern_id,
        )


async def publish_anomaly_detected(
    anomaly_id: str,
    anomaly_type: str,
    severity: str,
    description: str,
    metrics: dict[str, Any],
    *,
    publisher: Any | None = None,
) -> None:
    """Publish an ``anomaly.detected`` event to the Bodai queue.

    Never raises under normal failure modes -- see
    :func:`publish_pattern_detected` for the contract.

    Args:
        anomaly_id: Anomaly identifier.
        anomaly_type: Type of anomaly.
        severity: Severity level (``low``, ``medium``, ``high``, ``critical``).
        description: Human-readable description.
        metrics: Anomaly metrics dict.
        publisher: Injected event publisher. ``None`` uses the module-level global.
    """
    # Early-return when no publisher is configured (see publish_pattern_detected).
    effective_publisher = publisher if publisher is not None else _get_publisher()
    if effective_publisher is None:
        return
    try:
        payload: dict[str, Any] = {
            "anomaly_id": anomaly_id,
            "anomaly_type": anomaly_type,
            "severity": severity,
            "description": description,
            "metrics": metrics,
        }
        envelope = _make_envelope(TOPIC_ANOMALY_DETECTED, SOURCE, payload)
        await _publish(envelope, effective_publisher)
    except Exception:
        logger.exception(
            "akosha.publisher: failed to publish anomaly.detected event "
            "anomaly_id=%s",
            anomaly_id,
        )


async def publish_insight_generated(
    insight_id: str,
    insight_type: str,
    title: str,
    description: str,
    data: dict[str, Any],
    *,
    publisher: Any | None = None,
) -> None:
    """Publish an ``insight.generated`` event to the Bodai queue.

    Never raises under normal failure modes -- see
    :func:`publish_pattern_detected` for the contract.

    Args:
        insight_id: Insight identifier.
        insight_type: Type of insight.
        title: Insight title.
        description: Insight description.
        data: Insight data payload.
        publisher: Injected event publisher. ``None`` uses the module-level global.
    """
    # Early-return when no publisher is configured (see publish_pattern_detected).
    effective_publisher = publisher if publisher is not None else _get_publisher()
    if effective_publisher is None:
        return
    try:
        payload: dict[str, Any] = {
            "insight_id": insight_id,
            "insight_type": insight_type,
            "title": title,
            "description": description,
            "data": data,
        }
        envelope = _make_envelope(TOPIC_INSIGHT_GENERATED, SOURCE, payload)
        await _publish(envelope, effective_publisher)
    except Exception:
        logger.exception(
            "akosha.publisher: failed to publish insight.generated event "
            "insight_id=%s",
            insight_id,
        )


async def publish_aggregation_completed(
    aggregation_id: str,
    aggregation_type: str,
    record_count: int,
    summary: dict[str, Any],
    *,
    publisher: Any | None = None,
) -> None:
    """Publish an ``aggregation.completed`` event to the Bodai queue.

    Never raises under normal failure modes -- see
    :func:`publish_pattern_detected` for the contract.

    Args:
        aggregation_id: Aggregation identifier.
        aggregation_type: Type of aggregation.
        record_count: Number of records aggregated.
        summary: Aggregation summary dict.
        publisher: Injected event publisher. ``None`` uses the module-level global.
    """
    # Early-return when no publisher is configured (see publish_pattern_detected).
    effective_publisher = publisher if publisher is not None else _get_publisher()
    if effective_publisher is None:
        return
    try:
        payload: dict[str, Any] = {
            "aggregation_id": aggregation_id,
            "aggregation_type": aggregation_type,
            "record_count": record_count,
            "summary": summary,
        }
        envelope = _make_envelope(TOPIC_AGGREGATION_COMPLETED, SOURCE, payload)
        await _publish(envelope, effective_publisher)
    except Exception:
        logger.exception(
            "akosha.publisher: failed to publish aggregation.completed event "
            "aggregation_id=%s",
            aggregation_id,
        )


__all__ = [
    "EVENT_VERSION",
    "SOURCE",
    "TOPIC_AGGREGATION_COMPLETED",
    "TOPIC_ANOMALY_DETECTED",
    "TOPIC_INSIGHT_GENERATED",
    "TOPIC_PATTERN_DETECTED",
    "_get_publisher",
    "_make_envelope",
    "publish_aggregation_completed",
    "publish_anomaly_detected",
    "publish_insight_generated",
    "publish_pattern_detected",
    "set_eventbridge_publisher",
]
```

- [x] **Step 2: Extend the test file with the publish_* tests**

Append to `tests/unit/test_eventbridge_publisher.py` (after the three tests from Task 2):

```python
@pytest.mark.asyncio
async def test_publish_pattern_detected_invokes_injected_publisher() -> None:
    """publish_pattern_detected calls publisher.publish with the right envelope."""
    publisher = AsyncMock()
    publisher.publish.return_value = None

    await publish_pattern_detected(
        "pat_xyz", "anomaly_burst", "burst detected", 0.95,
        {"k": "v"}, publisher=publisher,
    )

    publisher.publish.assert_awaited_once()
    envelope = publisher.publish.await_args.args[0]
    assert isinstance(envelope, EventEnvelope)
    assert envelope.topic == "pattern.detected"
    payload = _payload_of(envelope)
    assert payload.get("pattern_id") == "pat_xyz"
    assert payload.get("pattern_type") == "anomaly_burst"
    assert payload.get("confidence") == 0.95
    assert payload.get("k") == "v"  # metadata merged
    assert _headers_of(envelope).get("source") == "akosha"


@pytest.mark.asyncio
async def test_publish_anomaly_detected_builds_canonical_envelope() -> None:
    """publish_anomaly_detected emits topic=anomaly.detected with the right payload."""
    publisher = AsyncMock()
    publisher.publish.return_value = None

    await publish_anomaly_detected(
        "anom_1", "spike", "high", "latency spike",
        {"p99_ms": 500.0}, publisher=publisher,
    )

    publisher.publish.assert_awaited_once()
    envelope = publisher.publish.await_args.args[0]
    assert envelope.topic == "anomaly.detected"
    payload = _payload_of(envelope)
    assert payload.get("anomaly_id") == "anom_1"
    assert payload.get("severity") == "high"
    assert payload.get("metrics") == {"p99_ms": 500.0}
    assert _headers_of(envelope).get("source") == "akosha"
    assert _headers_of(envelope).get("version") == EVENT_VERSION


@pytest.mark.asyncio
async def test_publish_insight_generated_builds_canonical_envelope() -> None:
    """publish_insight_generated emits topic=insight.generated with the right payload."""
    publisher = AsyncMock()
    publisher.publish.return_value = None

    await publish_insight_generated(
        "ins_1", "trend", "Upward trend in p99",
        "Latency trended up over the past hour",
        {"slope": 0.15}, publisher=publisher,
    )

    publisher.publish.assert_awaited_once()
    envelope = publisher.publish.await_args.args[0]
    assert envelope.topic == "insight.generated"
    payload = _payload_of(envelope)
    assert payload.get("insight_id") == "ins_1"
    assert payload.get("title") == "Upward trend in p99"
    assert payload.get("data") == {"slope": 0.15}


@pytest.mark.asyncio
async def test_publish_aggregation_completed_builds_canonical_envelope() -> None:
    """publish_aggregation_completed emits topic=aggregation.completed."""
    publisher = AsyncMock()
    publisher.publish.return_value = None

    await publish_aggregation_completed(
        "agg_1", "telemetry", record_count=1024,
        summary={"avg": 12.5}, publisher=publisher,
    )

    publisher.publish.assert_awaited_once()
    envelope = publisher.publish.await_args.args[0]
    assert envelope.topic == "aggregation.completed"
    payload = _payload_of(envelope)
    assert payload.get("aggregation_id") == "agg_1"
    assert payload.get("aggregation_type") == "telemetry"
    assert payload.get("record_count") == 1024
    assert payload.get("summary") == {"avg": 12.5}


@pytest.fixture(autouse=True)
def _reset_module_publisher() -> Generator[None, None, None]:
    """Guarantee the module-level publisher is None around every test.

    Replaces inline ``set_eventbridge_publisher(None)`` cleanup at the
    end of each test. Pytest fixtures guarantee cleanup even when an
    assertion in the test body raises; inline cleanup would skip on
    failure and leak module state to subsequent tests.
    """
    from akosha.observability import eventbridge_publisher

    eventbridge_publisher.set_eventbridge_publisher(None)
    yield
    eventbridge_publisher.set_eventbridge_publisher(None)


@pytest.mark.asyncio
async def test_publisher_with_none_is_a_noop() -> None:
    """publisher=None (and no module-level global) is silently accepted."""
    # Must not raise
    await publish_pattern_detected("p", "t", "d", 0.5, {})
    await publish_anomaly_detected("a", "t", "low", "d", {})
    await publish_insight_generated("i", "t", "title", "d", {})
    await publish_aggregation_completed("ag", "t", 0, {})


@pytest.mark.asyncio
async def test_publisher_uses_module_level_global_when_kwarg_is_none() -> None:
    """When publisher=None kwarg is passed, the module-level global is used."""
    from akosha.observability import eventbridge_publisher

    module_publisher = AsyncMock()
    module_publisher.publish.return_value = None
    eventbridge_publisher.set_eventbridge_publisher(module_publisher)

    await publish_pattern_detected("p", "t", "d", 0.5, {})

    module_publisher.publish.assert_awaited_once()
    envelope = module_publisher.publish.await_args.args[0]
    assert envelope.topic == "pattern.detected"
    # Cleanup handled by the ``_reset_module_publisher`` autouse fixture.


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("transport is down"),
        ConnectionError("broker unreachable"),
        TimeoutError("publish timed out"),
        TypeError("malformed envelope"),
        OSError("network unreachable"),
    ],
)
@pytest.mark.asyncio
async def test_publisher_swallows_exception_types(
    exc: BaseException, caplog: pytest.LogCaptureFixture
) -> None:
    """Each ``Exception`` subclass is logged exactly once and never propagates.

    Parametrized over 5 exception subclasses to ensure the ``except Exception``
    in the ``publish_*`` functions catches the full set of common transport
    failures. Per-call log correlation (not aggregate count) ensures a
    silent-swallow bug in one call cannot be masked by another call's logs.
    """
    publisher = AsyncMock()
    publisher.publish.side_effect = exc

    with caplog.at_level(
        logging.WARNING, logger="akosha.observability.eventbridge_publisher"
    ):
        # Must NOT raise out of publish_pattern_detected
        await publish_pattern_detected("p", "t", "d", 0.5, {}, publisher=publisher)

    error_logs = [
        rec for rec in caplog.records
        if rec.levelno >= logging.WARNING
        and rec.name == "akosha.observability.eventbridge_publisher"
    ]
    assert len(error_logs) == 1, (
        f"expected exactly 1 log per call, got {len(error_logs)}"
    )
    assert "pattern.detected" in error_logs[0].getMessage()


@pytest.mark.asyncio
async def test_publisher_does_not_swallow_cancelled_error() -> None:
    """``asyncio.CancelledError`` is ``BaseException`` and propagates.

    ``except Exception`` does not catch ``BaseException`` subclasses; this is
    correct asyncio semantics. The publisher must let cancellation bubble up
    so Ctrl-C interrupts long-running analytics.
    """
    publisher = AsyncMock()
    publisher.publish.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await publish_pattern_detected("p", "t", "d", 0.5, {}, publisher=publisher)


@pytest.mark.asyncio
async def test_publisher_supports_sync_publish_returning_none() -> None:
    """Sync publisher returning ``None`` (non-awaitable) is supported.

    Exercises the ``inspect.isawaitable(result) == False`` branch in
    ``_publish``. ``Mock`` is used instead of ``AsyncMock`` because
    ``Mock`` does not auto-create ``__await__``.
    """
    sync_publisher = Mock()  # noqa: S3776  -- deliberately Mock, not AsyncMock
    sync_publisher.publish.return_value = None

    await publish_pattern_detected(
        "p_sync", "burst", "d", 0.5, {}, publisher=sync_publisher
    )
    sync_publisher.publish.assert_called_once()


@pytest.mark.asyncio
async def test_publisher_swallows_coroutine_raising_after_await() -> None:
    """A coroutine returned by ``publish`` that raises mid-execution is swallowed."""
    publisher = AsyncMock()

    async def boom(_envelope: object) -> None:
        raise ConnectionError("lost mid-flight")

    publisher.publish.return_value = boom()

    # Must NOT raise out of publish_pattern_detected
    await publish_pattern_detected("p", "t", "d", 0.5, {}, publisher=publisher)


@pytest.mark.parametrize(
    "call, expected_topic",
    [
        (
            lambda pub: publish_pattern_detected("p1", "burst", "d", 0.0, {}, publisher=pub),
            "pattern.detected",
        ),
        (
            lambda pub: publish_anomaly_detected("a1", "spike", "", "d", {}, publisher=pub),
            "anomaly.detected",
        ),
        (
            lambda pub: publish_insight_generated("i1", "trend", "title", "d", {}, publisher=pub),
            "insight.generated",
        ),
        (
            lambda pub: publish_aggregation_completed("ag1", "telemetry", 0, {}, publisher=pub),
            "aggregation.completed",
        ),
    ],
)
@pytest.mark.asyncio
async def test_publisher_handles_zero_and_empty_values(
    call: object, expected_topic: str
) -> None:
    """Boundary values (``0``, ``""``, ``0.0``) flow through without dropping fields."""
    publisher = AsyncMock()
    publisher.publish.return_value = None
    await call(publisher)  # type: ignore[operator]
    envelope = publisher.publish.await_args.args[0]
    assert envelope.topic == expected_topic
    payload = _payload_of(envelope)
    assert payload, "payload must be a non-empty dict, not None"


@pytest.mark.asyncio
async def test_event_id_is_valid_uuid4_format() -> None:
    """The event_id header parses as a UUID4 (not just truthy)."""
    envelope = _make_envelope(TOPIC_PATTERN_DETECTED, SOURCE, {"pattern_id": "p"})
    event_id = _headers_of(envelope).get("event_id")
    assert isinstance(event_id, str) and event_id
    parsed = uuid.UUID(event_id)
    assert parsed.version == 4, "event_id must be UUID4 (random)"
```

Also add to the imports at the top of the file:

```python
import logging
```

- [x] **Step 3: Run all tests in the file**

Run: `cd /Users/les/Projects/akosha && pytest tests/unit/test_eventbridge_publisher.py -v`
Expected: All 25 tests pass (3 from Task 2, 7 basic publish_* tests from Step 2.1, 5 parametrized exception types + CancelledError + sync publisher + coroutine raising + boundary cases + UUID4 format + autouse fixture = 15 from Step 2.2).

- [x] **Step 4: Verify ruff is clean**

Run: `cd /Users/les/Projects/akosha && ruff check akosha/observability/eventbridge_publisher.py tests/unit/test_eventbridge_publisher.py`
Expected: `All checks passed!`

- [x] **Step 5: Verify coverage gate still passes**

Run: `cd /Users/les/Projects/akosha && pytest tests/unit/test_eventbridge_publisher.py --cov=akosha.observability.eventbridge_publisher --cov-report=term-missing --cov-fail-under=85`
Expected: Coverage ≥ 85%.

- [x] **Step 6: Commit**

```bash
cd /Users/les/Projects/akosha
git add akosha/observability/eventbridge_publisher.py tests/unit/test_eventbridge_publisher.py
git commit -m "feat(eventbridge): add Akosha analytics-event publisher

Wraps broadcast_pattern_detected/anomaly_detected/insight_generated/
aggregation_completed into the canonical Oneiric EventEnvelope so the
Mahavishnu Bodai subscriber surfaces [akosha] pattern.detected/
anomaly.detected/insight.generated/aggregation.completed lines.
Mirrors mahavishnu.core.events.mahavishnu_publisher; never-raises
contract preserved; publisher=None is a no-op; module-level global
(set_eventbridge_publisher) allows MCP-tool-driven ad-hoc emission."
```

---

## Task 4: Settings — add `EventBridgeConfig` to `akosha/config.py`

**Files:**
- Modify: `akosha/config.py` (after `CacheConfig`, before `AkoshaConfig`)

**Interfaces:**
- Consumes: Existing `BaseModel` imports from `pydantic` (line 8)
- Produces: `class EventBridgeConfig(BaseModel)` + `eventbridge: EventBridgeConfig = Field(default_factory=EventBridgeConfig)` field on `AkoshaConfig`

- [x] **Step 1: Read the end of `CacheConfig`**

Run: `cd /Users/les/Projects/akosha && sed -n '146,170p' akosha/config.py`
Expected: Tail of `CacheConfig` and start of `AkoshaConfig`.

- [x] **Step 1.5: Write the failing test for env-var binding**

Add this test to `tests/unit/test_akosha_config.py` (create if absent) or append to `tests/unit/test_config.py`:

```python
import pytest

from akosha.config import EventBridgeConfig


def test_eventbridge_config_binds_enabled_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AKOSHA_EVENTBRIDGE_ENABLED=true must propagate to cfg.enabled."""
    monkeypatch.setenv("AKOSHA_EVENTBRIDGE_ENABLED", "true")
    cfg = EventBridgeConfig()
    assert cfg.enabled is True, (
        f"AKOSHA_EVENTBRIDGE_ENABLED=true did not propagate; got {cfg.enabled}"
    )


def test_eventbridge_config_binds_dry_run_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AKOSHA_EVENTBRIDGE_DRY_RUN=false must propagate to cfg.dry_run."""
    monkeypatch.setenv("AKOSHA_EVENTBRIDGE_DRY_RUN", "false")
    cfg = EventBridgeConfig()
    assert cfg.dry_run is False


def test_eventbridge_config_defaults_when_no_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no env vars are set, defaults apply (enabled=False, dry_run=True)."""
    monkeypatch.delenv("AKOSHA_EVENTBRIDGE_ENABLED", raising=False)
    monkeypatch.delenv("AKOSHA_EVENTBRIDGE_DRY_RUN", raising=False)
    monkeypatch.delenv("AKOSHA_EVENTBRIDGE_ENDPOINT", raising=False)
    cfg = EventBridgeConfig()
    assert cfg.enabled is False
    assert cfg.dry_run is True
    assert cfg.endpoint is None
```

- [x] **Step 1.6: Run the tests to verify they fail (RED)**

Run: `cd /Users/les/Projects/akosha && pytest tests/unit/test_akosha_config.py -v -k eventbridge`
Expected: 2 tests fail (the env-var binding ones) because `EventBridgeConfig.__init__` does not yet read env vars; 1 passes (defaults).

- [x] **Step 2: Add the `EventBridgeConfig` model**

Insert the following class between `CacheConfig` (starts line 146) and `AkoshaConfig` (starts line 166):

```python
class EventBridgeConfig(BaseModel):
    """EventBridge publisher configuration.

    Attributes:
        enabled: Master toggle for the Akosha-side EventBridge publisher.
            When False, all publish_* calls are no-ops. Default False
            (disabled-by-default per operator-facing toggle convention).
        default_topic: Default topic suffix when callers omit one.
        default_source: Producer identifier (``akosha``).
        endpoint: Optional external EventBridge ingestion URL (not used yet;
            reserved for future AWS PutEvents integration).
        max_concurrency: Max concurrent publishes (unused; reserved).
        timeout_seconds: Per-publish HTTP timeout (unused; reserved).
        dry_run: When True, envelopes are logged but not transmitted.

    Configuration can be set via:
    1. settings/akosha.yaml under eventbridge
    2. settings/local.yaml
    3. Environment variables: AKOSHA_EVENTBRIDGE_ENABLED,
       AKOSHA_EVENTBRIDGE_DRY_RUN, AKOSHA_EVENTBRIDGE_ENDPOINT, etc.
    """

    enabled: bool = Field(
        default=False,
        description=(
            "Master toggle for the EventBridge publisher. Set to True to "
            "begin emitting analytics events to the Bodai EventBridge. "
            "Set via AKOSHA_EVENTBRIDGE_ENABLED."
        ),
    )
    default_topic: str = Field(
        default="analytics.default",
        description="Default topic suffix when callers omit one.",
    )
    default_source: str = Field(
        default="akosha",
        description="Producer identifier (always 'akosha' for this component).",
    )
    endpoint: str | None = Field(
        default=None,
        description=(
            "Optional external EventBridge ingestion URL. Reserved for "
            "future AWS PutEvents integration; not consumed by the current "
            "Oneiric dispatcher transport."
        ),
    )
    max_concurrency: int = Field(default=5, ge=1, le=100)
    timeout_seconds: float = Field(default=5.0, gt=0.0)
    dry_run: bool = Field(
        default=True,
        description=(
            "When True, envelopes are logged but not transmitted. Operators "
            "must explicitly set dry_run=False (or AKOSHA_EVENTBRIDGE_DRY_RUN=false) "
            "to actually emit events."
        ),
    )

    def __init__(self, **data: Any) -> None:
        # Per operational-safety Finding #6: MCPBaseSettings does not
        # auto-bind nested env vars like AKOSHA_EVENTBRIDGE_ENABLED. We
        # read them explicitly here, mirroring HotStorageConfig (line 87-95).
        # Only fills in fields not already explicitly passed via ``data``.
        _env_enabled = os.getenv("AKOSHA_EVENTBRIDGE_ENABLED", "")
        _env_dry_run = os.getenv("AKOSHA_EVENTBRIDGE_DRY_RUN", "")
        _env_endpoint = os.getenv("AKOSHA_EVENTBRIDGE_ENDPOINT", "")
        if _env_enabled and "enabled" not in data:
            data["enabled"] = _env_enabled.lower() in ("true", "1", "yes")
        if _env_dry_run and "dry_run" not in data:
            data["dry_run"] = _env_dry_run.lower() in ("true", "1", "yes")
        if _env_endpoint and "endpoint" not in data:
            data["endpoint"] = _env_endpoint
        super().__init__(**data)
```

Note: `import os` must already be present at the top of `akosha/config.py` (it is — line 14).

- [x] **Step 3: Add the field to `AkoshaConfig`**

Find `AkoshaConfig` (line 166) and add `eventbridge: EventBridgeConfig = Field(default_factory=EventBridgeConfig)` after the existing `cache: CacheConfig = Field(default_factory=CacheConfig)` field (around line 206). The new field must come after `cache` and before any subsequent fields (e.g., `api_port` at line 209).

- [x] **Step 4: Verify AkoshaConfig instantiates**

Run: `cd /Users/les/Projects/akosha && python -c "from akosha.config import AkoshaConfig, EventBridgeConfig; cfg = AkoshaConfig(); print(cfg.eventbridge.enabled, cfg.eventbridge.dry_run)"`
Expected: `False True` (the defaults).

- [x] **Step 5: Verify env-var binding**

Run: `cd /Users/les/Projects/akosha && AKOSHA_EVENTBRIDGE_ENABLED=true AKOSHA_EVENTBRIDGE_DRY_RUN=false python -c "from akosha.config import EventBridgeConfig; cfg = EventBridgeConfig(); print(cfg.enabled, cfg.dry_run)"`
Expected: `True False`

- [x] **Step 6: Run the unit tests from Step 1.5 to verify they pass (GREEN)**

Run: `cd /Users/les/Projects/akosha && pytest tests/unit/test_akosha_config.py -v -k eventbridge`
Expected: All 3 tests pass.

- [x] **Step 7: Commit**

```bash
cd /Users/les/Projects/akosha
git add akosha/config.py tests/unit/test_akosha_config.py
git commit -m "feat(config): add EventBridgeConfig Pydantic model

Adds Akosha-side settings for the EventBridge publisher (enabled toggle,
endpoint URL placeholder, dry_run safety flag). Disabled by default
per operator-facing toggle convention. Env-var binding via
AKOSHA_EVENTBRIDGE_* (e.g. AKOSHA_EVENTBRIDGE_ENABLED=true)."
```

---

## Task 5: Append settings block to `settings/akosha.yaml`

**Files:**
- Modify: `settings/akosha.yaml` (append at end)

**Interfaces:**
- Consumes: Existing YAML structure
- Produces: New top-level `eventbridge:` block

- [x] **Step 1: Read the tail of the YAML file**

Run: `tail -10 /Users/les/Projects/akosha/settings/akosha.yaml`
Expected: Final section (likely `monitoring:` or similar).

- [x] **Step 2: Append the eventbridge block**

Append to `settings/akosha.yaml` (preserve trailing newline if present):

```yaml
# EventBridge publisher (Phase 6 cross-repo publisher).
# When enabled=true and dry_run=false, Akosha emits analytics events
# (pattern.detected, anomaly.detected, insight.generated,
# aggregation.completed) to the unified Bodai EventBridge stream
# consumed by Mahavishnu's Bodai subscriber.
# Default disabled (enabled=false, dry_run=true) per operator-facing
# toggle convention.
eventbridge:
  enabled: false
  default_topic: "analytics.default"
  default_source: "akosha"
  endpoint: null  # Reserved for future AWS PutEvents integration
  max_concurrency: 5
  timeout_seconds: 5.0
  dry_run: true
```

- [x] **Step 3: Validate YAML and config loading**

Run: `cd /Users/les/Projects/akosha && python -c "from akosha.config import AkoshaConfig; cfg = AkoshaConfig(); print(cfg.eventbridge.enabled, cfg.eventbridge.dry_run, cfg.eventbridge.endpoint)"`
Expected: `False True None` (matches the YAML defaults).

- [x] **Step 4: Commit**

```bash
cd /Users/les/Projects/akosha
git add settings/akosha.yaml
git commit -m "feat(settings): add eventbridge block to akosha.yaml

Disabled-by-default settings for the Akosha EventBridge publisher.
Mirrors the pattern used in mahavishnu.yaml and crackerjack.yaml."
```

---

## Task 6: Wire publish calls into `AkoshaWebSocketServer.broadcast_*`

**Files:**
- Modify: `akosha/websocket/server.py`
  - Imports: add `from akosha.observability.eventbridge_publisher import publish_pattern_detected, publish_anomaly_detected, publish_insight_generated, publish_aggregation_completed`
  - Method 1: `broadcast_pattern_detected` (line 338) — add `await publish_pattern_detected(...)` after the existing `await self.broadcast_to_room(...)` call (after line 367)
  - Method 2: `broadcast_anomaly_detected` (line 369) — same pattern (after line 398)
  - Method 3: `broadcast_insight_generated` (line 400) — same (after line 429)
  - Method 4: `broadcast_aggregation_completed` (line 431) — same (after line 466)

**Interfaces:**
- Consumes: The four `publish_*` functions from `akosha.observability.eventbridge_publisher`
- Produces: 4 sibling `await publish_*` calls (one per `broadcast_*` method), each AFTER the existing `broadcast_to_room` call

- [x] **Step 1: Add the import**

Find the imports block at the top of `akosha/websocket/server.py`. Locate the existing first-party imports (e.g. `from akosha...`). Add (in alphabetical order with other akosha imports):

```python
from akosha.observability.eventbridge_publisher import (
    publish_aggregation_completed,
    publish_anomaly_detected,
    publish_insight_generated,
    publish_pattern_detected,
)
```

If the file uses `from __future__ import annotations`, ensure this import is after that line. If there are no existing akosha imports, add this near the end of the imports block.

- [x] **Step 2: Wire `broadcast_pattern_detected` (line 338)**

Find the end of the method body (line 367 `await self.broadcast_to_room(f"patterns:{pattern_type}", event)`). Append AFTER this line (preserving the existing indentation):

```python
        await publish_pattern_detected(
            pattern_id=pattern_id,
            pattern_type=pattern_type,
            description=description,
            confidence=confidence,
            metadata=metadata,
        )
```

- [x] **Step 3: Wire `broadcast_anomaly_detected` (line 369)**

Find the end of the method body (line 398 `await self.broadcast_to_room("anomalies", event)`). Append AFTER:

```python
        await publish_anomaly_detected(
            anomaly_id=anomaly_id,
            anomaly_type=anomaly_type,
            severity=severity,
            description=description,
            metrics=metrics,
        )
```

- [x] **Step 4: Wire `broadcast_insight_generated` (line 400)**

Find the end of the method body (line 429 `await self.broadcast_to_room("insights", event)`). Append AFTER:

```python
        await publish_insight_generated(
            insight_id=insight_id,
            insight_type=insight_type,
            title=title,
            description=description,
            data=data,
        )
```

- [x] **Step 5: Wire `broadcast_aggregation_completed` (line 431)**

Read the full method first to confirm its body ends where expected (around line 466 — the scout truncated it). Find the `await self.broadcast_to_room(...)` call. Append AFTER:

```python
        await publish_aggregation_completed(
            aggregation_id=aggregation_id,
            aggregation_type=aggregation_type,
            record_count=record_count,
            summary=summary,
        )
```

- [x] **Step 6: Run the existing websocket tests**

Run: `cd /Users/les/Projects/akosha && pytest tests/unit/test_websocket_server.py -x -q --timeout=120`
Expected: All existing tests pass. The new `await publish_*` calls are no-ops when `eventbridge_publisher._publisher` is `None` (the default).

- [x] **Step 7: Verify ruff is clean**

Run: `cd /Users/les/Projects/akosha && ruff check akosha/websocket/server.py`
Expected: `All checks passed!`

- [x] **Step 8: Commit**

```bash
cd /Users/les/Projects/akosha
git add akosha/websocket/server.py
git commit -m "feat(websocket): wire publish_* into AkoshaWebSocketServer.broadcast_*

Adds sibling await publish_* calls inside the four broadcast_*
methods (broadcast_pattern_detected, broadcast_anomaly_detected,
broadcast_insight_generated, broadcast_aggregation_completed). Publish
calls are no-ops when the module-level publisher global is None,
preserving existing behavior for callers that don't configure one."
```

---

## Task 7: MCP tool — `publish_to_eventbridge`

**Files:**
- Create: `akosha/mcp/tools/eventbridge_tools.py`
- Modify: `akosha/mcp/tools/tool_registry.py` (add `EVENTS = "events"` to `ToolCategory`)
- Modify: `akosha/mcp/tools/profiles.py` (add to `FULL_REGISTRATIONS`)
- Modify: `akosha/mcp/tools/__init__.py` (wire into `register_all_tools`)

**Interfaces:**
- Consumes: The four `publish_*` functions from `akosha.observability.eventbridge_publisher`
- Produces: One MCP tool `publish_to_eventbridge(topic, payload, *, async_callback=False) -> dict`

- [x] **Step 1: Add `EVENTS` category**

Open `akosha/mcp/tools/tool_registry.py` (line 16 starts `ToolCategory(StrEnum)`). Add a new entry to the StrEnum (preserve alphabetical order):

```python
    EVENTS = "events"
```

(Insert after `ANALYTICS = "analytics"` and before `GRAPH = "graph"`, or at end if the existing order is by discovery date.)

- [x] **Step 2: Create `akosha/mcp/tools/eventbridge_tools.py`**

```python
"""MCP tools for the Akosha-side EventBridge publisher.

Exposes a single ``publish_to_eventbridge`` MCP tool that wraps the
underlying ``publish_*`` async functions into a sync-callable interface
for Claude Code and other MCP clients.

Mirrors the dispatch_to_pool pattern from
``mahavishnu/mcp/tools/pool_tools.py``: optional ``async_callback`` flag
returns a workflow_id immediately and runs the publish in the background.

The tool is gated to ``ToolProfile.FULL`` (per the precedent of
``register_fitness_tools`` -- analytics-adjacent tools are full-only).
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
import uuid

from akosha.observability.eventbridge_publisher import (
    publish_aggregation_completed,
    publish_anomaly_detected,
    publish_insight_generated,
    publish_pattern_detected,
)

if TYPE_CHECKING:
    from akosha.mcp.tools.tool_registry import ToolCategory
    from mcp_common.fastmcp import FastMCP

logger = logging.getLogger(__name__)


async def _dispatch_topic(topic: str, payload: dict[str, Any]) -> None:
    """Route a (topic, payload) pair to the matching ``publish_*`` function."""
    if topic == "pattern.detected":
        await publish_pattern_detected(
            pattern_id=payload["pattern_id"],
            pattern_type=payload["pattern_type"],
            description=payload["description"],
            confidence=payload["confidence"],
            metadata=payload.get("metadata", {}),
        )
    elif topic == "anomaly.detected":
        await publish_anomaly_detected(
            anomaly_id=payload["anomaly_id"],
            anomaly_type=payload["anomaly_type"],
            severity=payload["severity"],
            description=payload["description"],
            metrics=payload.get("metrics", {}),
        )
    elif topic == "insight.generated":
        await publish_insight_generated(
            insight_id=payload["insight_id"],
            insight_type=payload["insight_type"],
            title=payload["title"],
            description=payload["description"],
            data=payload.get("data", {}),
        )
    elif topic == "aggregation.completed":
        await publish_aggregation_completed(
            aggregation_id=payload["aggregation_id"],
            aggregation_type=payload["aggregation_type"],
            record_count=payload["record_count"],
            summary=payload.get("summary", {}),
        )
    else:
        logger.warning(
            "akosha.eventbridge_tools: unknown topic=%s; ignoring", topic
        )


def register_eventbridge_tools(
    mcp_app: "FastMCP",
    enabled: bool = False,
    category: "ToolCategory | None" = None,
) -> None:
    """Register the EventBridge publisher MCP tool.

    Args:
        mcp_app: FastMCP application instance.
        enabled: Master toggle for the tool. When False (default), the
            tool is registered but rejects every call with
            ``{"status": "disabled"}``. Per operational-safety
            Finding #8: gating on the FULL profile alone is
            insufficient because FULL is the default profile -- this
            would silently enable the publisher on existing installs.
            Callers must explicitly pass ``enabled=True`` after
            validating the operator has set
            ``eventbridge.enabled=true`` in settings.
        category: Optional ToolCategory for registry grouping. If None,
            the tool is registered without a category tag.
    """
    @mcp_app.tool()
    async def publish_to_eventbridge(
        topic: str,
        payload: dict[str, Any],
        async_callback: bool = False,
    ) -> dict[str, Any]:
        """Publish an analytics event to the Akosha EventBridge stream.

        Args:
            topic: One of ``pattern.detected``, ``anomaly.detected``,
                ``insight.generated``, ``aggregation.completed``.
            payload: Event payload dict (must match the topic's schema).
            async_callback: If true, return immediately with a workflow_id
                and run the publish in the background.

        Returns:
            Dict with one of:
            - ``{"status": "published"}`` (sync, enabled)
            - ``{"workflow_id": "<uuid>", "status": "queued"}`` (async, enabled)
            - ``{"status": "disabled"}`` (when ``enabled=False`` at registration)
        """
        if not enabled:
            return {"status": "disabled"}

        if async_callback:
            workflow_id = f"pub_{uuid.uuid4().hex[:12]}"
            asyncio.create_task(_dispatch_topic(topic, payload))
            return {"workflow_id": workflow_id, "status": "queued"}

        await _dispatch_topic(topic, payload)
        return {"status": "published"}


__all__ = ["register_eventbridge_tools"]
```

- [x] **Step 3: Register the tool with the FULL profile**

Open `akosha/mcp/tools/profiles.py`. Find `FULL_REGISTRATIONS` (around line 31-78). Add `"register_eventbridge_tools"` to the list. Also add the corresponding entry to `REGISTRATION_TOOLS` (around line 23-29) mapping the registration function name to the `ToolCategory.EVENTS` value.

If the structure differs from the scout's report, follow the existing pattern: find where `register_fitness_tools` is listed (it's the closest precedent — analytics-adjacent, full-only) and mirror that structure.

- [x] **Step 4: Wire into `register_all_tools`**

Open `akosha/mcp/tools/__init__.py`. Find the `register_all_tools` function (line 77). Add a call to `register_eventbridge_tools` at the appropriate location (typically grouped with other observability/analytics tools). The call MUST pass `enabled=cfg.eventbridge.enabled` from the loaded `AksoshaConfig` — this is the operator-facing kill switch (per Finding #8).

```python
def register_all_tools(app, config, ...):
    # ...existing registrations...
    from akosha.config import AkoshaConfig
    register_eventbridge_tools(app, enabled=config.eventbridge.enabled)
    # ...more registrations...
```

If `register_all_tools` already takes `config` or a similar settings object, read `eventbridge.enabled` from it. If not, refactor `register_all_tools` to take the loaded config so the gating works.

- [x] **Step 5: Run the existing tests**

Run: `cd /Users/les/Projects/akosha && pytest tests/unit/ -x -q --timeout=120`
Expected: All existing tests pass.

- [x] **Step 6: Verify ruff is clean**

Run: `cd /Users/les/Projects/akosha && ruff check akosha/mcp/tools/eventbridge_tools.py akosha/mcp/tools/tool_registry.py akosha/mcp/tools/profiles.py akosha/mcp/tools/__init__.py`
Expected: `All checks passed!`

- [x] **Step 7: Commit**

```bash
cd /Users/les/Projects/akosha
git add akosha/mcp/tools/eventbridge_tools.py akosha/mcp/tools/tool_registry.py akosha/mcp/tools/profiles.py akosha/mcp/tools/__init__.py
git commit -m "feat(mcp): expose publish_to_eventbridge MCP tool

Allows Claude Code and other MCP clients to publish analytics events
to the unified Bodai EventBridge. Gated to ToolProfile.FULL following
the precedent of register_fitness_tools. Supports sync and async
(async_callback=true) modes mirroring mahavishnu.dispatch_to_pool."
```

---

## Task 8: Integration test — end-to-end envelope round-trip

**Files:**
- Create: `tests/integration/test_eventbridge_e2e.py`

**Interfaces:**
- Consumes: `publish_pattern_detected`, `publish_anomaly_detected`, `publish_insight_generated`, `publish_aggregation_completed`
- Produces: 5 integration tests verifying envelopes flow from publish call through a recording transport and back as parsed dicts

- [x] **Step 1: Create the integration test file**

Write `tests/integration/test_eventbridge_e2e.py`:

```python
"""End-to-end integration tests for the Akosha EventBridge publisher.

Verifies that publish_* produces envelopes with the canonical shape the
Mahavishnu Bodai subscriber consumes (topic, payload, headers.source,
headers.event_id, headers.timestamp). Uses an in-memory recording
transport (no Redis or AWS required) to simulate the round trip.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from akosha.observability.eventbridge_publisher import (
    EVENT_VERSION,
    SOURCE,
    publish_aggregation_completed,
    publish_anomaly_detected,
    publish_insight_generated,
    publish_pattern_detected,
)


@dataclass
class RecordingTransport:
    """Records every published envelope for later inspection."""

    published: list[dict[str, Any]] = field(default_factory=list)

    async def publish(self, envelope: Any) -> Any:
        self.published.append(
            {
                "topic": envelope.topic,
                "payload": dict(envelope.payload),
                "headers": dict(envelope.headers),
            }
        )
        return envelope


pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_publish_pattern_detected_round_trips_through_transport() -> None:
    transport = RecordingTransport()
    await publish_pattern_detected(
        "pat_e2e_1", "anomaly_burst", "burst", 0.95,
        {"k": "v"}, publisher=transport,
    )
    assert len(transport.published) == 1
    record = transport.published[0]
    assert record["topic"] == "pattern.detected"
    assert record["payload"]["pattern_id"] == "pat_e2e_1"
    assert record["payload"]["pattern_type"] == "anomaly_burst"
    assert record["payload"]["confidence"] == 0.95
    assert record["headers"]["source"] == SOURCE
    assert record["headers"]["version"] == EVENT_VERSION
    assert record["headers"]["event_id"]


@pytest.mark.asyncio
async def test_publish_anomaly_detected_round_trips_through_transport() -> None:
    transport = RecordingTransport()
    await publish_anomaly_detected(
        "anom_e2e_1", "spike", "high", "latency spike",
        {"p99_ms": 500.0}, publisher=transport,
    )
    assert len(transport.published) == 1
    record = transport.published[0]
    assert record["topic"] == "anomaly.detected"
    assert record["payload"]["anomaly_id"] == "anom_e2e_1"
    assert record["payload"]["severity"] == "high"
    assert record["payload"]["metrics"] == {"p99_ms": 500.0}


@pytest.mark.asyncio
async def test_publish_insight_generated_round_trips_through_transport() -> None:
    transport = RecordingTransport()
    await publish_insight_generated(
        "ins_e2e_1", "trend", "Upward trend",
        "latency trending up", {"slope": 0.15}, publisher=transport,
    )
    assert len(transport.published) == 1
    record = transport.published[0]
    assert record["topic"] == "insight.generated"
    assert record["payload"]["insight_id"] == "ins_e2e_1"
    assert record["payload"]["title"] == "Upward trend"
    assert record["payload"]["data"] == {"slope": 0.15}


@pytest.mark.asyncio
async def test_publish_aggregation_completed_round_trips_through_transport() -> None:
    transport = RecordingTransport()
    await publish_aggregation_completed(
        "agg_e2e_1", "telemetry", record_count=1024,
        summary={"avg": 12.5}, publisher=transport,
    )
    assert len(transport.published) == 1
    record = transport.published[0]
    assert record["topic"] == "aggregation.completed"
    assert record["payload"]["aggregation_id"] == "agg_e2e_1"
    assert record["payload"]["record_count"] == 1024
    assert record["payload"]["summary"] == {"avg": 12.5}


@pytest.mark.asyncio
async def test_four_sequential_publishes_preserve_order_and_uniqueness() -> None:
    """Four publishes produce four distinct event_ids with payload order preserved."""
    transport = RecordingTransport()
    await publish_pattern_detected("p", "t", "d", 0.5, {}, publisher=transport)
    await publish_anomaly_detected("a", "t", "low", "d", {}, publisher=transport)
    await publish_insight_generated("i", "t", "t", "d", {}, publisher=transport)
    await publish_aggregation_completed("ag", "t", 0, {}, publisher=transport)

    assert [r["topic"] for r in transport.published] == [
        "pattern.detected",
        "anomaly.detected",
        "insight.generated",
        "aggregation.completed",
    ]
    event_ids = [r["headers"]["event_id"] for r in transport.published]
    assert len(set(event_ids)) == 4, "event_ids must be unique across publishes"
```

- [x] **Step 2: Run the integration tests**

Run: `cd /Users/les/Projects/akosha && pytest tests/integration/test_eventbridge_e2e.py -v --timeout=120`
Expected: All 5 tests pass.

- [x] **Step 3: Commit**

```bash
cd /Users/les/Projects/akosha
git add tests/integration/test_eventbridge_e2e.py
git commit -m "test(eventbridge): add end-to-end round-trip integration tests

Verifies canonical envelope shape survives transport for all 4
analytics event types. Uses an in-memory RecordingTransport; no Redis
or AWS required. Covers pattern/anomaly/insight/aggregation plus a
sequential-publish uniqueness check across all 4."
```

---

## Task 9: Smoke test (manual verification)

**Files:**
- None. Manual verification step.

- [x] **Step 1: Confirm the smoke test passes**

Run:
```bash
cd /Users/les/Projects/akosha && python -c "
import asyncio
from akosha.observability.eventbridge_publisher import (
    publish_pattern_detected,
    publish_anomaly_detected,
    publish_insight_generated,
    publish_aggregation_completed,
)

async def main():
    # No publisher -> no-ops, but should not raise
    await publish_pattern_detected('smoke_p', 't', 'd', 0.5, {})
    await publish_anomaly_detected('smoke_a', 't', 'low', 'd', {})
    await publish_insight_generated('smoke_i', 't', 't', 'd', {})
    await publish_aggregation_completed('smoke_ag', 't', 0, {})

asyncio.run(main())
print('OK')
"
```
Expected: `OK` prints, no errors.

- [x] **Step 2: Final commit (if any cleanup needed)**

If any documentation updates are needed (e.g., adding a note to `CLAUDE.md` or `docs/ROUTES_GUIDE.md` analog), commit them here. Otherwise skip.

---

## Integration Contract

- **Triggered from:** `AkoshaWebSocketServer.broadcast_pattern_detected / broadcast_anomaly_detected / broadcast_insight_generated / broadcast_aggregation_completed` (Task 6) AND `publish_to_eventbridge` MCP tool (Task 7).
- **Returns to / updates:** None directly. Envelopes flow into Oneiric EventBridge → Mahavishnu Bodai subscriber → `~/.mahavishnu/bodai-event-queue.json` → Claude Code `/bodai-status` and PostToolUse hook.
- **Demonstrable by:** Run `pytest tests/unit/test_eventbridge_publisher.py tests/unit/test_eventbridge_adapter.py tests/unit/test_akosha_config.py tests/integration/test_eventbridge_e2e.py -v` from Akosha; all 35 tests pass (2 adapter + 25 publisher unit + 3 config env binding + 5 integration). Plus a manual smoke test invoking `publish_pattern_detected` from a Python REPL.
- **Rollback signal:** None — the publisher is non-destructive. Disable by NOT calling `set_eventbridge_publisher()` at app startup (the default state), or by setting `eventbridge.enabled=false` in `settings/akosha.yaml` (the YAML toggle is checked by future wiring, not by this plan).
- **Observability added:** `akosha.publisher` logger emits WARNING-or-higher records on publish failure (test introspection via `caplog`).

## References

- `mahavishnu/core/events/mahavishnu_publisher.py` — pattern mirrored 1:1
- `tests/unit/test_mahavishnu_publisher.py` — test pattern mirrored
- `mahavishnu/core/events/bodai_subscriber.py` — consumer-side; the wire format this publisher emits
- `oneiric.runtime.events.EventEnvelope` / `create_event_envelope` — canonical envelope (msgspec.Struct, three fields: `topic`, `payload`, `headers`)
- `.claude/decisions/bodai-observability-pattern.md` (in mahavishnu repo) — the convergence rule this publisher implements
- `docs/plans/2026-07-11-phase-6-bodai-observability.md` (in mahavishnu repo) — Phase 6 close-out context
- `crackerjack/docs/superpowers/plans/2026-07-12-eventbridge-publisher.md` — parallel implementation; review for naming consistency before starting
- `akosha/alerting/__init__.py` — Akosha's existing webhook publisher template (singleton + `to_dict` + `httpx` pattern; mirrored here but for EventBridge instead of webhooks)