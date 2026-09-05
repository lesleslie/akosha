"""Tests for ``akosha.observability.eventbridge_adapter.EventBridgePublisher``.

The adapter translates ``publisher.publish(envelope)`` calls (the
shape :mod:`eventbridge_publisher` expects) into
``bridge.emit(topic, payload, headers)`` calls (the shape oneiric's
EventBridge exposes). It is the production injection point — without
it, the publisher has no production-compatible bridge to wire into.

Audit found this module at 0% coverage with a 1-test no-op
placeholder. These tests pin the actual translation contract.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from akosha.observability.eventbridge_adapter import EventBridgePublisher


@pytest.fixture
def fake_bridge() -> MagicMock:
    """A bridge stand-in: the only attribute accessed is ``emit`` (async)."""
    bridge = MagicMock()
    bridge.emit = AsyncMock(return_value=None)
    return bridge


@pytest.fixture
def fake_envelope() -> MagicMock:
    """An envelope stand-in exposing the three attributes the adapter reads."""
    env = MagicMock()
    env.topic = "test_topic"
    env.payload = {"key": "value", "count": 42}
    env.headers = {"x-trace-id": "abc-123"}
    return env


@pytest.fixture
def publisher(fake_bridge: MagicMock) -> EventBridgePublisher:
    return EventBridgePublisher(fake_bridge)


def test_constructor_stores_bridge_reference(
    publisher: EventBridgePublisher, fake_bridge: MagicMock
) -> None:
    """Pin: the adapter must hold the bridge for the publish() lifetime."""
    assert publisher._bridge is fake_bridge  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_publish_forwards_topic_payload_headers(
    publisher: EventBridgePublisher,
    fake_bridge: MagicMock,
    fake_envelope: MagicMock,
) -> None:
    """Three-attribute translation: topic, payload, headers."""
    await publisher.publish(fake_envelope)  # type: ignore[arg-type]
    fake_bridge.emit.assert_awaited_once_with(
        "test_topic",
        {"key": "value", "count": 42},
        {"x-trace-id": "abc-123"},
    )


@pytest.mark.asyncio
async def test_publish_propagates_bridge_runtime_error(
    publisher: EventBridgePublisher,
    fake_bridge: MagicMock,
    fake_envelope: MagicMock,
) -> None:
    """If ``bridge.emit`` raises, ``publish`` must surface it — no swallowing."""
    fake_bridge.emit.side_effect = RuntimeError("bridge down")
    with pytest.raises(RuntimeError, match="bridge down"):
        await publisher.publish(fake_envelope)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_publish_handles_empty_payload_and_headers(
    publisher: EventBridgePublisher,
    fake_bridge: MagicMock,
) -> None:
    """Empty payload/headers are valid envelopes — adapter must not crash."""
    env = MagicMock()
    env.topic = "minimal"
    env.payload = {}
    env.headers = {}

    await publisher.publish(env)  # type: ignore[arg-type]
    fake_bridge.emit.assert_awaited_once_with("minimal", {}, {})


@pytest.mark.asyncio
async def test_publish_called_twice_invokes_bridge_twice(
    publisher: EventBridgePublisher,
    fake_bridge: MagicMock,
) -> None:
    """No internal batching/state — each call is a discrete bridge.emit."""
    env = MagicMock()
    env.topic = "x"
    env.payload = {"n": 1}
    env.headers = {}

    await publisher.publish(env)  # type: ignore[arg-type]
    await publisher.publish(env)  # type: ignore[arg-type]
    assert fake_bridge.emit.await_count == 2


@pytest.mark.asyncio
async def test_publish_with_non_dict_payload(
    publisher: EventBridgePublisher,
    fake_bridge: MagicMock,
) -> None:
    """Envelopes are typed but payloads can be any structure — no coercion."""
    env = MagicMock()
    env.topic = "binary"
    env.payload = b"raw bytes"
    env.headers = {}

    await publisher.publish(env)  # type: ignore[arg-type]
    fake_bridge.emit.assert_awaited_once_with("binary", b"raw bytes", {})
