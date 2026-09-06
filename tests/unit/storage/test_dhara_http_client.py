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
from unittest.mock import AsyncMock, MagicMock, patch

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
        client._client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
        client._client.aclose = AsyncMock(return_value=None)

        result = await client.list_prefix("p/")

        assert result == []


class TestPut:
    @pytest.mark.asyncio
    async def test_dhara_http_client_put_returns_true_on_success(self) -> None:
        """httpx 200 -> ``True``."""
        client = DharaHttpClient(base_url="http://example.invalid")
        mock_response = _build_response({"content": [{"type": "text", "text": "ok"}]})
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
        client._client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
        client._client.aclose = AsyncMock(return_value=None)

        result = await client.put("k", {"v": 1})

        assert result is False


class TestLifecycle:
    """Cover the ``_ensure_client`` lazy init and ``aclose`` paths."""

    @pytest.mark.asyncio
    async def test_ensure_client_constructs_on_first_call(self) -> None:
        """First call to ``list_prefix`` constructs the httpx client."""
        client = DharaHttpClient(base_url="http://x", timeout_seconds=5.0)
        assert client._client is None  # lazy — not constructed yet
        sentinel = MagicMock()
        sentinel.post = AsyncMock(return_value=_build_response({"content": []}))
        sentinel.aclose = AsyncMock(return_value=None)
        with patch(
            "akosha.storage.dhara_http_client.httpx.AsyncClient", return_value=sentinel
        ) as factory:
            await client.list_prefix("p/")
            factory.assert_called_once_with(timeout=5.0)
        await client.aclose()

    @pytest.mark.asyncio
    async def test_ensure_client_reuses_existing_instance(self) -> None:
        """Second call reuses the existing client — no re-construction."""
        client = DharaHttpClient(base_url="http://x")
        sentinel = MagicMock()
        sentinel.post = AsyncMock(return_value=_build_response({"content": []}))
        sentinel.aclose = AsyncMock(return_value=None)
        client._client = sentinel

        with patch("akosha.storage.dhara_http_client.httpx.AsyncClient") as factory:
            await client.list_prefix("p/")
            await client.list_prefix("p/")
            factory.assert_not_called()  # never re-constructed
            assert sentinel.post.await_count == 2

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
        sentinel = MagicMock()
        sentinel.post = AsyncMock(return_value=_build_response({"content": []}))
        sentinel.aclose = AsyncMock(return_value=None)
        client._client = sentinel

        await client.aclose()

        sentinel.aclose.assert_awaited_once()
        assert client._client is None


class TestListPrefixResponseShapes:
    """Cover the ``_parse_mcp_content`` tolerance branches."""

    @pytest.mark.asyncio
    async def test_list_prefix_empty_content_returns_empty_list(self) -> None:
        """``{"content": []}`` → ``[]`` (no items to parse)."""
        client = DharaHttpClient(base_url="http://x")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.post = AsyncMock(return_value=_build_response({"content": []}))
        client._client.aclose = AsyncMock(return_value=None)

        assert await client.list_prefix("p/") == []

    @pytest.mark.asyncio
    async def test_list_prefix_response_missing_content_key(self) -> None:
        """``{}`` (no ``content`` key) → ``[]``."""
        client = DharaHttpClient(base_url="http://x")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.post = AsyncMock(return_value=_build_response({}))
        client._client.aclose = AsyncMock(return_value=None)

        assert await client.list_prefix("p/") == []

    @pytest.mark.asyncio
    async def test_list_prefix_response_text_malformed_json(self) -> None:
        """Malformed JSON inside ``content[0].text`` → ``[]`` (tolerant)."""
        client = DharaHttpClient(base_url="http://x")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.post = AsyncMock(
            return_value=_build_response({"content": [{"type": "text", "text": "{not json"}]})
        )
        client._client.aclose = AsyncMock(return_value=None)

        assert await client.list_prefix("p/") == []

    @pytest.mark.asyncio
    async def test_list_prefix_response_text_is_not_a_list(self) -> None:
        """``content[0].text`` parses to a dict (not a list) → ``[]``."""
        client = DharaHttpClient(base_url="http://x")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.post = AsyncMock(
            return_value=_build_response({"content": [{"type": "text", "text": '{"key": "k"}'}]})
        )
        client._client.aclose = AsyncMock(return_value=None)

        assert await client.list_prefix("p/") == []

    @pytest.mark.asyncio
    async def test_list_prefix_skips_items_missing_key_or_value(self) -> None:
        """List items without ``key`` or ``value`` are silently skipped."""
        client = DharaHttpClient(base_url="http://x")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.post = AsyncMock(
            return_value=_build_response(
                {
                    "content": [
                        {
                            "type": "text",
                            "text": '[{"key": "a", "value": {"x": 1}}, {"key": "b"}, {"value": {"y": 2}}, "not-a-dict"]',
                        }
                    ]
                }
            )
        )
        client._client.aclose = AsyncMock(return_value=None)

        result = await client.list_prefix("p/")
        # Only the well-formed item makes it through.
        assert result == [("a", {"x": 1})]

    @pytest.mark.asyncio
    async def test_list_prefix_response_top_level_not_dict(self) -> None:
        """Response body that is not a dict → ``[]`` (graceful)."""
        client = DharaHttpClient(base_url="http://x")
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.post = AsyncMock(
            return_value=_build_response("just a string")  # type: ignore[arg-type]
        )
        client._client.aclose = AsyncMock(return_value=None)

        assert await client.list_prefix("p/") == []


class TestHttpStatusErrors:
    """Cover non-2xx responses for both ``list_prefix`` and ``put``."""

    @pytest.mark.asyncio
    async def test_list_prefix_500_returns_empty(self) -> None:
        """HTTP 500 → ``[]`` (no exception leak)."""
        client = DharaHttpClient(base_url="http://x")
        resp = _build_response({"error": "internal"})
        resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("500", request=MagicMock(), response=resp)
        )
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.post = AsyncMock(return_value=resp)
        client._client.aclose = AsyncMock(return_value=None)

        assert await client.list_prefix("p/") == []

    @pytest.mark.asyncio
    async def test_put_500_returns_false(self) -> None:
        """HTTP 500 on put → ``False``."""
        client = DharaHttpClient(base_url="http://x")
        resp = _build_response({"error": "internal"})
        resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("500", request=MagicMock(), response=resp)
        )
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.post = AsyncMock(return_value=resp)
        client._client.aclose = AsyncMock(return_value=None)

        assert await client.put("k", {"v": 1}) is False
