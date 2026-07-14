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

import inspect
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

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
    """Build the canonical Oneiric ``EventEnvelope`` for an Akosha event."""
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
    ``publish`` results.
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

    Never raises under normal failure modes. ``asyncio.CancelledError``
    propagates (it is ``BaseException``, not ``Exception``).
    """
    effective_publisher = publisher if publisher is not None else _get_publisher()
    if effective_publisher is None:
        return
    try:
        payload: dict[str, Any] = {
            "pattern_id": pattern_id,
            "pattern_type": pattern_type,
            "description": description,
            "confidence": confidence,
        } | metadata
        envelope = _make_envelope(TOPIC_PATTERN_DETECTED, SOURCE, payload)
        await _publish(envelope, effective_publisher)
    except Exception:
        logger.exception(
            "akosha.publisher: failed to publish pattern.detected event pattern_id=%s",
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
    """Publish an ``anomaly.detected`` event to the Bodai queue."""
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
            "akosha.publisher: failed to publish anomaly.detected event anomaly_id=%s",
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
    """Publish an ``insight.generated`` event to the Bodai queue."""
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
            "akosha.publisher: failed to publish insight.generated event insight_id=%s",
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
    """Publish an ``aggregation.completed`` event to the Bodai queue."""
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
            "akosha.publisher: failed to publish aggregation.completed event aggregation_id=%s",
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
