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
        "pat_e2e_1", "anomaly_burst", "burst", 0.95, {"k": "v"}, publisher=transport
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
        "anom_e2e_1",
        "spike",
        "high",
        "latency spike",
        {"p99_ms": 500.0},
        publisher=transport,
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
        "ins_e2e_1",
        "trend",
        "Upward trend",
        "latency trending up",
        {"slope": 0.15},
        publisher=transport,
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
        "agg_e2e_1",
        "telemetry",
        record_count=1024,
        summary={"avg": 12.5},
        publisher=transport,
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