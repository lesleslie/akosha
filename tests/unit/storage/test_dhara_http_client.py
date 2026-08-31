"""Tests for ``akosha.storage.dhara_http_client``.

Followup 4 of docs/plans/2026-08-29-akosha-websocket-search.md. The
client wraps Dhara's ``POST /tools/call`` MCP-style endpoint so the
``WebSocketInvocationsSubscriber`` can poll
``websocket_tool_invocation/v1/*`` without re-implementing httpx plumbing.

These tests cover:
- Successful ``list_prefix`` parsing of MCP-format content.
- Graceful empty ``[]`` on transport errors (no exception leak).
- Successful ``put`` returning True.
- Graceful False on transport errors.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx2 as httpx
import pytest

from akosha.storage.dhara_http_client import DharaHttpClient


def _build_response(json_body: dict[str, Any]) -> MagicMock:
    """Build a mock httpx Response with the given JSON body."""
    resp = MagicMock(spec=httpx.Response)
    resp.json = MagicMock(return_value=json_body)
    resp.raise_for_status = MagicMock(return_value=None)
    return resp


class TestListPrefix:
    @pytest.mark.asyncio
    async def test_dhara_http_client_list_prefix_parses_mcp_content(self) -> None:
        """MCP-format response (content[0].text = JSON str) -> [(key, value)]."""
        client = DharaHttpClient(base_url="http://example.invalid")
        mock_response = _build_response(
            {
                "content": [
                    {
                        "type": "text",
                        "text": '[{"key": "a", "value": {"x": 1}}]',
                    }
                ]
            }
        )
        # Patch the lazy httpx client.
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.post = AsyncMock(return_value=mock_response)
        client._client.aclose = AsyncMock(return_value=None)

        result = await client.list_prefix("p/")

        assert result == [("a", {"x": 1})]
        client._client.post.assert_awaited_once()
        # Verify the POST URL + payload shape matches the Dhara MCP pattern.
        post_args = client._client.post.await_args
        assert post_args.args[0] == "http://example.invalid/tools/call"
        assert post_args.kwargs["json"]["name"] == "list_prefix"
        assert post_args.kwargs["json"]["arguments"]["prefix"] == "p/"

    @pytest.mark.asyncio
    async def test_dhara_http_client_list_prefix_returns_empty_on_error(
        self,
    ) -> None:
        """httpx.ConnectError on the post -> ``[]`` and no exception leak."""
        client = DharaHttpClient(base_url="http://example.invalid")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.post = AsyncMock(
            side_effect=httpx.ConnectError("connection refused")
        )
        client._client.aclose = AsyncMock(return_value=None)

        result = await client.list_prefix("p/")

        assert result == []


class TestPut:
    @pytest.mark.asyncio
    async def test_dhara_http_client_put_returns_true_on_success(self) -> None:
        """httpx 200 -> ``True``."""
        client = DharaHttpClient(base_url="http://example.invalid")
        mock_response = _build_response(
            {"content": [{"type": "text", "text": "ok"}]}
        )
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.post = AsyncMock(return_value=mock_response)
        client._client.aclose = AsyncMock(return_value=None)

        result = await client.put("k", {"v": 1})

        assert result is True
        post_args = client._client.post.await_args
        assert post_args.kwargs["json"]["name"] == "put"
        assert post_args.kwargs["json"]["arguments"]["key"] == "k"
        assert post_args.kwargs["json"]["arguments"]["value"] == {"v": 1}

    @pytest.mark.asyncio
    async def test_dhara_http_client_put_returns_false_on_error(self) -> None:
        """httpx.ConnectError on the post -> ``False``."""
        client = DharaHttpClient(base_url="http://example.invalid")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.post = AsyncMock(
            side_effect=httpx.ConnectError("connection refused")
        )
        client._client.aclose = AsyncMock(return_value=None)

        result = await client.put("k", {"v": 1})

        assert result is False
