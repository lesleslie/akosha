"""Unit tests for ``akosha.observability.eventbridge_publisher``.

Mirrors the test pattern from
``mahavishnu/tests/unit/test_mahavishnu_publisher.py`` and the parallel
``tests/unit/test_eventbridge_publisher.py`` in Crackerjack. Same envelope
shape, same never-raises guarantee, same duck-typed publisher injection.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import pytest
from oneiric.runtime.events import EventEnvelope

if TYPE_CHECKING:
    from collections.abc import Generator

from akosha.observability.eventbridge_publisher import (
    EVENT_VERSION,
    SOURCE,
    TOPIC_AGGREGATION_COMPLETED,
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
    offset = parsed.astimezone(UTC).utcoffset()
    assert offset is not None and offset == timedelta(0)


@pytest.mark.asyncio
async def test_publish_pattern_detected_invokes_injected_publisher() -> None:
    """publish_pattern_detected calls publisher.publish with the right envelope."""
    publisher = AsyncMock()
    publisher.publish.return_value = None

    await publish_pattern_detected(
        "pat_xyz", "anomaly_burst", "burst detected", 0.95, {"k": "v"}, publisher=publisher
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
        "anom_1",
        "spike",
        "high",
        "latency spike",
        {"p99_ms": 500.0},
        publisher=publisher,
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
        "ins_1",
        "trend",
        "Upward trend in p99",
        "Latency trended up over the past hour",
        {"slope": 0.15},
        publisher=publisher,
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
        "agg_1",
        "telemetry",
        record_count=1024,
        summary={"avg": 12.5},
        publisher=publisher,
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
def _reset_module_publisher() -> Generator[None]:
    """Guarantee the module-level publisher is None around every test."""
    from akosha.observability import eventbridge_publisher

    eventbridge_publisher.set_eventbridge_publisher(None)
    yield
    eventbridge_publisher.set_eventbridge_publisher(None)


@pytest.mark.asyncio
async def test_publisher_with_none_is_a_noop() -> None:
    """publisher=None (and no module-level global) is silently accepted."""
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
    """Each ``Exception`` subclass is logged exactly once and never propagates."""
    publisher = AsyncMock()
    publisher.publish.side_effect = exc

    with caplog.at_level(
        logging.WARNING, logger="akosha.observability.eventbridge_publisher"
    ):
        await publish_pattern_detected("p", "t", "d", 0.5, {}, publisher=publisher)

    error_logs = [
        rec
        for rec in caplog.records
        if rec.levelno >= logging.WARNING
        and rec.name == "akosha.observability.eventbridge_publisher"
    ]
    assert len(error_logs) == 1, (
        f"expected exactly 1 log per call, got {len(error_logs)}"
    )
    assert "pattern.detected" in error_logs[0].getMessage()


@pytest.mark.asyncio
async def test_publisher_does_not_swallow_cancelled_error() -> None:
    """``asyncio.CancelledError`` is ``BaseException`` and propagates."""
    publisher = AsyncMock()
    publisher.publish.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await publish_pattern_detected("p", "t", "d", 0.5, {}, publisher=publisher)


@pytest.mark.asyncio
async def test_publisher_supports_sync_publish_returning_none() -> None:
    """Sync publisher returning ``None`` (non-awaitable) is supported."""
    sync_publisher = Mock()
    sync_publisher.publish.return_value = None

    await publish_pattern_detected(
        "p_sync", "burst", "d", 0.5, {}, publisher=sync_publisher
    )
    sync_publisher.publish.assert_called_once()


@pytest.mark.asyncio
async def test_publisher_swallows_coroutine_raising_after_await() -> None:
    """A coroutine returned by ``publish`` that raises mid-execution is swallowed.

    Uses ``side_effect`` with an ``async def`` so each ``publisher.publish``
    call returns a real coroutine that raises when awaited.
    """
    publisher = AsyncMock()

    async def boom(_envelope: object) -> None:
        raise ConnectionError("lost mid-flight")

    publisher.publish.side_effect = boom

    await publish_pattern_detected("p", "t", "d", 0.5, {}, publisher=publisher)


@pytest.mark.parametrize(
    "call, expected_topic",
    [
        (
            lambda pub: publish_pattern_detected(
                "p1", "burst", "d", 0.0, {}, publisher=pub
            ),
            "pattern.detected",
        ),
        (
            lambda pub: publish_anomaly_detected(
                "a1", "spike", "", "d", {}, publisher=pub
            ),
            "anomaly.detected",
        ),
        (
            lambda pub: publish_insight_generated(
                "i1", "trend", "title", "d", {}, publisher=pub
            ),
            "insight.generated",
        ),
        (
            lambda pub: publish_aggregation_completed(
                "ag1", "telemetry", 0, {}, publisher=pub
            ),
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
    await call(publisher)  # ty: ignore[call-non-callable]
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
