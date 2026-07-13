"""Real Oneiric EventBridge transport integration tests for Aksha.

Mirrors the parallel test in
``crackerjack/tests/integration/test_oneiric_transport_roundtrip.py``.
Exercises the full ``publish_* -> EventBridgePublisher -> emit`` path
against a real ``oneiric.domains.events.EventBridge``.
"""
from __future__ import annotations

import inspect
from typing import Any

import pytest

from oneiric.core.config import LayerSettings
from oneiric.core.lifecycle import LifecycleManager
from oneiric.core.resolution import Resolver
from oneiric.domains.events import EventBridge
from oneiric.runtime.events import EventEnvelope, EventHandler, HandlerResult

from akosha.observability.eventbridge_adapter import EventBridgePublisher
from akosha.observability.eventbridge_publisher import (
    publish_aggregation_completed,
    publish_anomaly_detected,
    publish_insight_generated,
    publish_pattern_detected,
)


pytestmark = [pytest.mark.integration, pytest.mark.timeout(30)]


class _CapturingDispatcher:
    def __init__(self) -> None:
        self.captured: list[EventEnvelope] = []
        self._handlers: list[EventHandler] = []

    async def dispatch(self, envelope: EventEnvelope) -> list[HandlerResult]:
        self.captured.append(envelope)
        results: list[HandlerResult] = []
        for handler in self._handlers:
            if not handler.accepts(envelope):
                continue
            try:
                value = handler.callback(envelope)
                if inspect.isawaitable(value):
                    await value
                results.append(
                    HandlerResult(handler=handler.name, success=True, duration=0.0)
                )
            except Exception as exc:  # noqa: BLE001
                results.append(
                    HandlerResult(
                        handler=handler.name,
                        success=False,
                        duration=0.0,
                        error=str(exc),
                    )
                )
        return results

    def register(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    def handlers(self) -> list[EventHandler]:
        return list(self._handlers)


def _build_real_eventbridge() -> tuple[EventBridge, _CapturingDispatcher]:
    resolver = Resolver()
    lifecycle = LifecycleManager(resolver)
    settings = LayerSettings()
    bridge = EventBridge(resolver, lifecycle, settings)
    dispatcher = _CapturingDispatcher()
    bridge._dispatcher = dispatcher  # noqa: SLF001 -- test-only
    return bridge, dispatcher


@pytest.mark.asyncio
async def test_publish_pattern_detected_round_trips_through_real_eventbridge() -> None:
    bridge, dispatcher = _build_real_eventbridge()
    publisher = EventBridgePublisher(bridge)

    await publish_pattern_detected(
        "pat_rt", "burst", "burst detected", 0.9, {}, publisher=publisher
    )

    envelope = dispatcher.captured[0]
    assert envelope.topic == "pattern.detected"
    assert envelope.headers.get("source") == "akosha"
    assert envelope.payload.get("confidence") == 0.9


@pytest.mark.asyncio
async def test_publish_anomaly_detected_round_trips() -> None:
    bridge, dispatcher = _build_real_eventbridge()
    publisher = EventBridgePublisher(bridge)

    await publish_anomaly_detected(
        "anom_rt",
        "spike",
        "high",
        "spike detected",
        {"p99_ms": 500.0},
        publisher=publisher,
    )

    envelope = dispatcher.captured[0]
    assert envelope.topic == "anomaly.detected"
    assert envelope.payload.get("anomaly_id") == "anom_rt"


@pytest.mark.asyncio
async def test_publish_insight_generated_round_trips() -> None:
    bridge, dispatcher = _build_real_eventbridge()
    publisher = EventBridgePublisher(bridge)

    await publish_insight_generated(
        "ins_rt",
        "trend",
        "Up trend",
        "Latency trending up",
        {"slope": 0.2},
        publisher=publisher,
    )

    envelope = dispatcher.captured[0]
    assert envelope.topic == "insight.generated"
    assert envelope.payload.get("title") == "Up trend"


@pytest.mark.asyncio
async def test_publish_aggregation_completed_round_trips() -> None:
    bridge, dispatcher = _build_real_eventbridge()
    publisher = EventBridgePublisher(bridge)

    await publish_aggregation_completed(
        "agg_rt",
        "telemetry",
        record_count=2048,
        summary={"avg": 10.0},
        publisher=publisher,
    )

    envelope = dispatcher.captured[0]
    assert envelope.topic == "aggregation.completed"
    assert envelope.payload.get("record_count") == 2048


@pytest.mark.asyncio
async def test_publisher_with_none_does_not_invoke_bridge() -> None:
    """Module-level publisher=None: publish_* short-circuits before bridge."""
    bridge, dispatcher = _build_real_eventbridge()
    await publish_pattern_detected("p", "t", "d", 0.5, {})
    assert dispatcher.captured == []
