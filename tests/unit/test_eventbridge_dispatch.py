"""Tests for the topic-dispatch and async_callback paths in ``akosha.mcp.tools.eventbridge_tools``.

The companion file ``test_eventbridge_tools.py`` covers registration and
the disabled/no_publisher branches. This file targets the remaining
uncovered branches:

- ``_dispatch_topic`` routing for all 4 known topics (pattern.detected,
  anomaly.detected, insight.generated, aggregation.completed)
- ``_dispatch_topic`` else-branch (unknown topic → warning log)
- ``async_callback=True`` path: returns ``{"workflow_id": ..., "status": "queued"}``
  without awaiting the dispatch
- Sync path with a wired publisher: returns ``{"status": "published"}``

Each test wires a fake publisher via ``set_eventbridge_publisher`` so the
tool's ``_get_publisher()`` returns a known object whose ``publish`` method
we can spy on.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from akosha.mcp.tools.eventbridge_tools import register_eventbridge_tools
from akosha.observability.eventbridge_publisher import (
    set_eventbridge_publisher,
)


@pytest.fixture(autouse=True)
def _reset_publisher() -> Any:
    """Reset the module-level publisher between tests so leaks don't cascade.

    The publisher is a module-level singleton; without this fixture, an
    AsyncMock injected by one test would leak into the next.
    """
    set_eventbridge_publisher(None)
    yield
    set_eventbridge_publisher(None)


def _capture_tool_with_publisher(publisher: Any) -> Any:
    """Register the tool with ``enabled=True`` and return the captured callable.

    Wires ``publisher`` into the module-level singleton via
    ``set_eventbridge_publisher`` before registration.
    """
    set_eventbridge_publisher(publisher)
    captured: list[Any] = []

    def tool_decorator(*_args: Any, **_kwargs: Any) -> Any:
        def deco(func: Any) -> Any:
            captured.append(func)
            return func

        return deco

    app = MagicMock()
    app.tool = tool_decorator
    register_eventbridge_tools(app, enabled=True)
    assert len(captured) == 1
    return captured[0]


class TestDispatchTopics:
    """``_dispatch_topic`` routes each known topic to its matching publisher call."""

    @pytest.mark.asyncio
    async def test_pattern_detected_dispatched(self) -> None:
        """``pattern.detected`` triggers ``publish_pattern_detected`` with the payload fields."""
        publisher = AsyncMock()
        publish = _capture_tool_with_publisher(publisher)
        await publish(
            topic="pattern.detected",
            payload={
                "pattern_id": "pat_1",
                "pattern_type": "burst",
                "description": "spike in error rate",
                "confidence": 0.9,
                "metadata": {"src": "ak"},
            },
        )
        assert publisher.publish.await_count == 1
        # Verify the envelope payload (proves the dispatch path used the
        # right publish_* function). The publisher merges ``metadata`` into
        # the top-level payload via dict union, so keys from ``metadata``
        # appear at the top level (NOT nested under a "metadata" key).
        envelope = publisher.publish.await_args.args[0]
        assert envelope.topic == "pattern.detected"
        assert envelope.payload["pattern_id"] == "pat_1"
        assert envelope.payload["src"] == "ak"

    @pytest.mark.asyncio
    async def test_anomaly_detected_dispatched(self) -> None:
        """``anomaly.detected`` triggers ``publish_anomaly_detected``."""
        publisher = AsyncMock()
        publish = _capture_tool_with_publisher(publisher)
        await publish(
            topic="anomaly.detected",
            payload={
                "anomaly_id": "anom_1",
                "anomaly_type": "latency",
                "severity": "high",
                "description": "p99 spiked",
                "metrics": {"p99_ms": 1234},
            },
        )
        assert publisher.publish.await_count == 1
        envelope = publisher.publish.await_args.args[0]
        assert envelope.topic == "anomaly.detected"
        assert envelope.payload["severity"] == "high"

    @pytest.mark.asyncio
    async def test_insight_generated_dispatched(self) -> None:
        """``insight.generated`` triggers ``publish_insight_generated``."""
        publisher = AsyncMock()
        publish = _capture_tool_with_publisher(publisher)
        await publish(
            topic="insight.generated",
            payload={
                "insight_id": "ins_1",
                "insight_type": "trend",
                "title": "Weekly summary",
                "description": "...",
                "data": {"k": "v"},
            },
        )
        assert publisher.publish.await_count == 1
        envelope = publisher.publish.await_args.args[0]
        assert envelope.topic == "insight.generated"

    @pytest.mark.asyncio
    async def test_aggregation_completed_dispatched(self) -> None:
        """``aggregation.completed`` triggers ``publish_aggregation_completed``."""
        publisher = AsyncMock()
        publish = _capture_tool_with_publisher(publisher)
        await publish(
            topic="aggregation.completed",
            payload={
                "aggregation_id": "agg_1",
                "aggregation_type": "daily",
                "record_count": 42,
                "summary": {"ok": True},
            },
        )
        assert publisher.publish.await_count == 1
        envelope = publisher.publish.await_args.args[0]
        assert envelope.topic == "aggregation.completed"
        assert envelope.payload["record_count"] == 42

    @pytest.mark.asyncio
    async def test_unknown_topic_logs_warning_and_skips(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An unknown topic must NOT raise and must log a warning."""
        publisher = AsyncMock()
        publish = _capture_tool_with_publisher(publisher)
        with caplog.at_level(logging.WARNING, logger="akosha.mcp.tools.eventbridge_tools"):
            result = await publish(topic="not.a.real.topic", payload={"x": 1})
        # Tool returns "published" status because the dispatch was a no-op
        # (logger.warning only). The publish function for the unknown topic
        # silently no-ops, so we never reach publisher.publish — the result
        # is "published" because the tool's await still returns cleanly.
        assert result.get("status") == "published"
        # No envelope was emitted for an unknown topic.
        assert publisher.publish.await_count == 0
        # The unknown-topic warning was logged.
        assert any(
            "unknown topic" in rec.message and "not.a.real.topic" in rec.message
            for rec in caplog.records
        )

    @pytest.mark.asyncio
    async def test_missing_metadata_defaults_to_empty_dict(self) -> None:
        """``pattern.detected`` without ``metadata`` must still produce a valid payload."""
        publisher = AsyncMock()
        publish = _capture_tool_with_publisher(publisher)
        await publish(
            topic="pattern.detected",
            payload={
                "pattern_id": "p",
                "pattern_type": "t",
                "description": "d",
                "confidence": 0.5,
                # metadata deliberately absent
            },
        )
        envelope = publisher.publish.await_args.args[0]
        # When ``metadata`` is missing, ``payload.get("metadata", {})``
        # defaults to ``{}`` in the publisher; since ``{} | {}`` produces
        # an empty dict, the merged payload has only the required fields.
        assert envelope.payload["pattern_id"] == "p"
        assert envelope.payload["confidence"] == 0.5

    @pytest.mark.asyncio
    async def test_anomaly_detected_default_metrics(self) -> None:
        """``anomaly.detected`` without ``metrics`` must default to ``{}``."""
        publisher = AsyncMock()
        publish = _capture_tool_with_publisher(publisher)
        await publish(
            topic="anomaly.detected",
            payload={
                "anomaly_id": "a",
                "anomaly_type": "t",
                "severity": "low",
                "description": "d",
                # metrics absent
            },
        )
        envelope = publisher.publish.await_args.args[0]
        assert envelope.payload.get("metrics") == {}


class TestSyncPath:
    """When ``async_callback=False``, the tool awaits the dispatch before returning."""

    @pytest.mark.asyncio
    async def test_sync_publish_returns_published_status(self) -> None:
        """A sync call with a wired publisher must return ``{"status": "published"}``."""
        publisher = AsyncMock()
        publish = _capture_tool_with_publisher(publisher)
        result = await publish(
            topic="pattern.detected",
            payload={
                "pattern_id": "p",
                "pattern_type": "t",
                "description": "d",
                "confidence": 0.5,
            },
        )
        assert result == {"status": "published"}
        assert publisher.publish.await_count == 1


class TestAsyncCallback:
    """``async_callback=True`` queues the dispatch and returns a workflow_id."""

    @pytest.mark.asyncio
    async def test_async_callback_returns_workflow_id_and_queued_status(self) -> None:
        """An async call returns a workflow_id and the queued status without awaiting the dispatch."""
        publisher = AsyncMock()
        publish = _capture_tool_with_publisher(publisher)
        result = await publish(
            topic="pattern.detected",
            payload={
                "pattern_id": "p",
                "pattern_type": "t",
                "description": "d",
                "confidence": 0.5,
            },
            async_callback=True,
        )
        # The sync return is immediate: a workflow_id + queued status.
        assert result["status"] == "queued"
        assert "workflow_id" in result
        # workflow_id format is "pub_<12 hex chars>".
        assert result["workflow_id"].startswith("pub_")
        assert len(result["workflow_id"]) == 4 + 12

    @pytest.mark.asyncio
    async def test_async_callback_eventually_publishes(self) -> None:
        """When ``async_callback=True``, the dispatch happens in a background task.

        We yield to the event loop once and verify the background task
        ran. The publisher's ``publish`` is async; we mock it as AsyncMock
        so awaiting it inside the background task completes immediately.
        """
        publisher = AsyncMock()
        publish = _capture_tool_with_publisher(publisher)
        await publish(
            topic="pattern.detected",
            payload={
                "pattern_id": "p",
                "pattern_type": "t",
                "description": "d",
                "confidence": 0.5,
            },
            async_callback=True,
        )
        # The background task may not have completed yet — give it one
        # event loop tick. ``asyncio.sleep(0)`` yields and lets pending
        # tasks run.
        await asyncio.sleep(0)
        assert publisher.publish.await_count == 1
