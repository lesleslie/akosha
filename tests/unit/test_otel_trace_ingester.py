"""Unit tests for OtelTraceIngester."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

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
