"""Tests for :func:`akosha.mcp.client.query_local_traces`.

The helper is the akosha-side chokepoint for the fleet-wide
``query_local_traces`` contract: it keeps the ergonomic relative window
(``time_range_minutes``) and converts it to the absolute ISO8601 window the
tool now takes. The conversion is the part that can silently drift, so it is
pinned here — including the exact argument SET, because the removed
``time_range_minutes`` field would be rejected by the server at runtime.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from akosha.mcp.client import query_local_traces


def _envelope(payload: Any) -> dict[str, Any]:
    """Wrap a payload in the MCP content envelope the client unwraps."""
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


class TestQueryLocalTraces:
    """The relative window is converted to an absolute one on the wire."""

    @pytest.mark.asyncio
    async def test_converts_relative_window_to_absolute(self) -> None:
        client = AsyncMock()
        client.call_tool = AsyncMock(return_value=_envelope([]))

        await query_local_traces(client, "code_generation", time_range_minutes=60)

        client.call_tool.assert_awaited_once()
        name, arguments = client.call_tool.await_args.args
        assert name == "query_local_traces"
        assert set(arguments) == {"task_class", "start_time", "end_time"}
        start = datetime.fromisoformat(arguments["start_time"])
        end = datetime.fromisoformat(arguments["end_time"])
        assert end - start == timedelta(minutes=60)
        assert start.tzinfo is not None
        assert end.tzinfo is not None

    @pytest.mark.asyncio
    async def test_removed_time_range_minutes_is_not_forwarded(self) -> None:
        """The removed field would be rejected by the server at runtime."""
        client = AsyncMock()
        client.call_tool = AsyncMock(return_value=_envelope([]))

        await query_local_traces(client, "code_generation")

        _, arguments = client.call_tool.await_args.args
        assert "time_range_minutes" not in arguments

    @pytest.mark.asyncio
    async def test_returns_parsed_payload(self) -> None:
        client = AsyncMock()
        client.call_tool = AsyncMock(return_value=_envelope([{"id": 1}]))

        assert await query_local_traces(client, "code_generation") == [{"id": 1}]

    @pytest.mark.asyncio
    async def test_unwraps_dict_payload_shapes(self) -> None:
        """The response-shape coercion (list OR dict with items) is Bodai-specific."""
        client = AsyncMock()
        client.call_tool = AsyncMock(return_value=_envelope({"items": [{"id": 2}]}))

        assert await query_local_traces(client, "code_generation") == [{"id": 2}]
