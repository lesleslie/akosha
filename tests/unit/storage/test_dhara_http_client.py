"""Tests for ``akosha.storage.dhara_http_client``.

Followup 4 of docs/plans/2026-08-29-akosha-websocket-search.md. The
client wraps Dhara's MCP ``list_prefix`` and ``put`` tools via
``mcp_common.clients.CommonMCPClient`` so the
``WebSocketInvocationsSubscriber`` can poll
``websocket_tool_invocation/v1/*`` without re-implementing HTTP plumbing.

These tests cover:
- Successful ``list_prefix`` parsing of MCP-format content.
- Graceful empty ``[]`` on transport errors (no exception leak).
- Successful ``put`` returning True.
- Graceful False on transport errors.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from akosha.storage.dhara_http_client import DharaHttpClient


def _wrap(payload: Any) -> dict[str, Any]:
    """Wrap an unwrapped tool payload in the MCP ``CallToolResult`` envelope."""
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def _make_call_tool_stub(payload: Any = None, side_effect: Any = None) -> MagicMock:
    """Build a stub with ``call_tool`` and ``aclose`` set up.

    Pass ``payload`` for a successful return, or ``side_effect`` for an
    exception to raise from the call.
    """
    stub = MagicMock()
    if side_effect is not None:
        stub.call_tool = AsyncMock(side_effect=side_effect)
    else:
        stub.call_tool = AsyncMock(return_value=_wrap(payload))
    stub.aclose = AsyncMock(return_value=None)
    return stub


class TestListPrefix:
    @pytest.mark.asyncio
    async def test_dhara_http_client_list_prefix_parses_mcp_content(self) -> None:
        """MCP-format response unwrapped to ``[(key, value)]``."""
        client = DharaHttpClient(base_url="http://example.invalid")
        client._client = _make_call_tool_stub([{"key": "a", "value": {"x": 1}}])

        result = await client.list_prefix("p/")

        assert result == [("a", {"x": 1})]
        call_args = client._client.call_tool.await_args
        assert call_args.args[0] == "list_prefix"
        assert call_args.args[1] == {"prefix": "p/"}

    @pytest.mark.asyncio
    async def test_dhara_http_client_list_prefix_returns_empty_on_error(
        self,
    ) -> None:
        """Exception on call_tool -> ``[]`` and no exception leak."""
        client = DharaHttpClient(base_url="http://example.invalid")
        client._client = _make_call_tool_stub(side_effect=Exception("connection refused"))

        result = await client.list_prefix("p/")

        assert result == []


class TestPut:
    @pytest.mark.asyncio
    async def test_dhara_http_client_put_returns_true_on_success(self) -> None:
        """Successful ``put`` call_tool -> ``True``."""
        client = DharaHttpClient(base_url="http://example.invalid")
        client._client = _make_call_tool_stub({"status": "ok"})

        result = await client.put("k", {"v": 1})

        assert result is True
        call_args = client._client.call_tool.await_args
        assert call_args.args[0] == "put"
        assert call_args.args[1] == {"key": "k", "value": {"v": 1}}

    @pytest.mark.asyncio
    async def test_dhara_http_client_put_returns_false_on_error(self) -> None:
        """Exception on call_tool -> ``False``."""
        client = DharaHttpClient(base_url="http://example.invalid")
        client._client = _make_call_tool_stub(side_effect=Exception("connection refused"))

        result = await client.put("k", {"v": 1})

        assert result is False


class TestLifecycle:
    """Cover the ``_ensure_client`` lazy init and ``aclose`` paths."""

    @pytest.mark.asyncio
    async def test_ensure_client_constructs_on_first_call(self) -> None:
        """First call to ``list_prefix`` constructs the MCP client."""
        client = DharaHttpClient(base_url="http://x", timeout_seconds=5.0)
        assert client._client is None  # lazy — not constructed yet
        sentinel = _make_call_tool_stub([])
        with patch(
            "akosha.storage.dhara_http_client.CommonMCPClient", return_value=sentinel
        ) as factory:
            await client.list_prefix("p/")
            factory.assert_called_once_with(
                base_url="http://x", timeout=5.0,
            )
        await client.aclose()

    @pytest.mark.asyncio
    async def test_ensure_client_reuses_existing_instance(self) -> None:
        """Second call reuses the existing client — no re-construction."""
        client = DharaHttpClient(base_url="http://x")
        sentinel = _make_call_tool_stub([])
        client._client = sentinel

        with patch("akosha.storage.dhara_http_client.CommonMCPClient") as factory:
            await client.list_prefix("p/")
            await client.list_prefix("p/")
            factory.assert_not_called()  # never re-constructed
            assert sentinel.call_tool.await_count == 2

    @pytest.mark.asyncio
    async def test_aclose_is_safe_when_client_never_opened(self) -> None:
        """``aclose()`` before any request must not raise."""
        client = DharaHttpClient(base_url="http://x")
        assert client._client is None
        await client.aclose()  # no-op
        assert client._client is None

    @pytest.mark.asyncio
    async def test_aclose_calls_client_close_and_clears_reference(self) -> None:
        """``aclose()`` calls the underlying client's ``aclose`` and clears the reference."""
        client = DharaHttpClient(base_url="http://x")
        sentinel = _make_call_tool_stub([])
        client._client = sentinel

        await client.aclose()

        sentinel.aclose.assert_awaited_once()
        assert client._client is None


class TestListPrefixResponseShapes:
    """Cover the ``_parse_list_payload`` tolerance branches."""

    @pytest.mark.asyncio
    async def test_list_prefix_empty_content_returns_empty_list(self) -> None:
        """Empty ``content`` (no payload text) → ``[]``."""
        client = DharaHttpClient(base_url="http://x")
        # Empty envelope — extract_mcp_payload returns None
        client._client = _make_call_tool_stub(None)
        # Force the envelope to have content but with empty list
        client._client.call_tool = AsyncMock(return_value={"content": []})

        assert await client.list_prefix("p/") == []

    @pytest.mark.asyncio
    async def test_list_prefix_response_malformed_json(self) -> None:
        """Malformed JSON inside ``content[0].text`` → ``[]`` (tolerant)."""
        client = DharaHttpClient(base_url="http://x")
        client._client = MagicMock()
        client._client.call_tool = AsyncMock(
            return_value={"content": [{"type": "text", "text": "{not json"}]}
        )
        client._client.aclose = AsyncMock(return_value=None)

        assert await client.list_prefix("p/") == []

    @pytest.mark.asyncio
    async def test_list_prefix_response_text_is_not_a_list(self) -> None:
        """``content[0].text`` parses to a dict (not a list) → ``[]``."""
        client = DharaHttpClient(base_url="http://x")
        client._client = _make_call_tool_stub({"key": "k"})

        assert await client.list_prefix("p/") == []

    @pytest.mark.asyncio
    async def test_list_prefix_skips_items_missing_key_or_value(self) -> None:
        """List items without ``key`` or ``value`` are silently skipped."""
        client = DharaHttpClient(base_url="http://x")
        client._client = _make_call_tool_stub(
            [
                {"key": "a", "value": {"x": 1}},
                {"key": "b"},
                {"value": {"y": 2}},
                "not-a-dict",
            ]
        )

        result = await client.list_prefix("p/")
        # Only the well-formed item makes it through.
        assert result == [("a", {"x": 1})]

    @pytest.mark.asyncio
    async def test_list_prefix_accepts_two_tuple_shape(self) -> None:
        """``[key, value]`` tuple shape is also accepted."""
        client = DharaHttpClient(base_url="http://x")
        client._client = _make_call_tool_stub(
            [["a", {"x": 1}], ["b", {"y": 2}]]
        )

        result = await client.list_prefix("p/")
        assert result == [("a", {"x": 1}), ("b", {"y": 2})]


class TestHttpStatusErrors:
    """Cover error responses for both ``list_prefix`` and ``put``."""

    @pytest.mark.asyncio
    async def test_list_prefix_5xx_returns_empty(self) -> None:
        """5xx from call_tool → ``[]`` (no exception leak)."""
        from mcp_common.clients.common_mcp_client import MCPClientHTTPError

        client = DharaHttpClient(base_url="http://x")
        client._client = _make_call_tool_stub(
            side_effect=MCPClientHTTPError("HTTP 500", status_code=500)
        )

        assert await client.list_prefix("p/") == []

    @pytest.mark.asyncio
    async def test_put_5xx_returns_false(self) -> None:
        """5xx from put call_tool → ``False``."""
        from mcp_common.clients.common_mcp_client import MCPClientHTTPError

        client = DharaHttpClient(base_url="http://x")
        client._client = _make_call_tool_stub(
            side_effect=MCPClientHTTPError("HTTP 500", status_code=500)
        )

        assert await client.put("k", {"v": 1}) is False
