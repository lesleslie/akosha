"""Tests for ``akosha.mcp.client`` — DharaServiceRegistryClient.

The previous ``BodaiComponentMCPClient`` class that lived here was extracted
to ``mcp_common.clients.common_mcp_client.CommonMCPClient`` as part of the
``docs/plans/2026-09-14-common-mcp-client-transport-unification.md`` plan.
Canonical CommonMCPClient tests now live at
``mcp-common/tests/unit/clients/test_common_mcp_client.py``.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from akosha.mcp.client import DharaServiceRegistryClient


# ---------------------------------------------------------------------------
# DharaServiceRegistryClient
# ---------------------------------------------------------------------------


def _dhara_mcp_response(payload: Any) -> dict[str, Any]:
    """Build the MCP tool-response envelope around ``payload``."""
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def test_dhara_registry_client_strips_trailing_slash() -> None:
    c = DharaServiceRegistryClient(base_url="http://dhara:8683/")
    assert c.base_url == "http://dhara:8683"


def test_dhara_registry_client_default_timeout_is_10_seconds() -> None:
    c = DharaServiceRegistryClient(base_url="http://dhara:8683")
    assert c.timeout == 10.0


@pytest.mark.asyncio
async def test_dhara_list_services_returns_unwrapped_list() -> None:
    """MCP response envelope (content/text) is unwrapped to the inner list."""
    inner = [{"name": "akosha", "url": "http://akosha:8682/mcp"}]
    fake_response = MagicMock()
    fake_response.json.return_value = _dhara_mcp_response(inner)
    fake_response.raise_for_status = MagicMock()

    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value=fake_response)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx2.AsyncClient", return_value=fake_client):
        client = DharaServiceRegistryClient(base_url="http://dhara:8683")
        result = await client.list_services()

    assert result == inner


@pytest.mark.asyncio
async def test_dhara_list_services_filters_by_service_type_capability_status() -> None:
    """All three optional filters land in the request arguments."""
    fake_response = MagicMock()
    fake_response.json.return_value = _dhara_mcp_response([])
    fake_response.raise_for_status = MagicMock()

    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value=fake_response)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx2.AsyncClient", return_value=fake_client):
        client = DharaServiceRegistryClient(base_url="http://dhara:8683")
        await client.list_services(service_type="mcp", capability="routing", status="active")

    call_payload = fake_client.post.await_args.kwargs["json"]
    assert call_payload["arguments"]["service_type"] == "mcp"
    assert call_payload["arguments"]["capability"] == "routing"
    assert call_payload["arguments"]["status"] == "active"


@pytest.mark.asyncio
async def test_dhara_get_returns_dict_when_value_is_a_dict() -> None:
    inner = {"url": "http://akosha:8682/mcp"}
    fake_response = MagicMock()
    fake_response.json.return_value = _dhara_mcp_response(inner)
    fake_response.raise_for_status = MagicMock()

    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value=fake_response)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx2.AsyncClient", return_value=fake_client):
        client = DharaServiceRegistryClient(base_url="http://dhara:8683")
        result = await client.get("component_endpoint/akosha")

    assert result == inner


@pytest.mark.asyncio
async def test_dhara_get_returns_none_when_key_missing() -> None:
    """Dhara encodes 'key not found' as JSON null → client must return None."""
    fake_response = MagicMock()
    fake_response.json.return_value = _dhara_mcp_response(None)
    fake_response.raise_for_status = MagicMock()

    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value=fake_response)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx2.AsyncClient", return_value=fake_client):
        client = DharaServiceRegistryClient(base_url="http://dhara:8683")
        result = await client.get("component_endpoint/nonexistent")

    assert result is None


@pytest.mark.asyncio
async def test_dhara_get_wraps_string_value_in_url_dict() -> None:
    """Some Dhara servers return the URL string directly → wrap in {url: ...}."""
    fake_response = MagicMock()
    fake_response.json.return_value = _dhara_mcp_response("http://akosha:8682/mcp")
    fake_response.raise_for_status = MagicMock()

    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value=fake_response)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx2.AsyncClient", return_value=fake_client):
        client = DharaServiceRegistryClient(base_url="http://dhara:8683")
        result = await client.get("component_endpoint/akosha")

    assert result == {"url": "http://akosha:8682/mcp"}


@pytest.mark.asyncio
async def test_dhara_list_prefix_returns_unwrapped_list() -> None:
    inner = [
        {"key": "component_endpoint/akosha", "value": {"url": "http://akosha:8682/mcp"}},
        {"key": "component_endpoint/mahavishnu", "value": {"url": "http://mahavishnu:8680/mcp"}},
    ]
    fake_response = MagicMock()
    fake_response.json.return_value = _dhara_mcp_response(inner)
    fake_response.raise_for_status = MagicMock()

    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value=fake_response)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx2.AsyncClient", return_value=fake_client):
        client = DharaServiceRegistryClient(base_url="http://dhara:8683")
        result = await client.list_prefix("component_endpoint/")

    assert result == inner
    # The prefix is forwarded to Dhara as the request argument.
    assert (
        fake_client.post.await_args.kwargs["json"]["arguments"]["prefix"] == "component_endpoint/"
    )


@pytest.mark.asyncio
async def test_dhara_aclose_is_noop() -> None:
    """DharaServiceRegistryClient has no resources to release."""
    client = DharaServiceRegistryClient(base_url="http://dhara:8683")
    await client.aclose()  # must not raise
