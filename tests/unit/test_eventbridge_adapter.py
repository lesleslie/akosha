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
