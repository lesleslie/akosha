"""Tests for ``akosha.mcp.client`` — BodaiComponentMCPClient + DharaServiceRegistryClient.

Audit found this module at 0% coverage. The clients sit on the
critical path for FitnessAnalyzer's trace polling and Dhara
service-discovery; both have non-trivial shape coercion logic and
SSRF defenses that need pinning.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from akosha.mcp.client import BodaiComponentMCPClient, DharaServiceRegistryClient


# ---------------------------------------------------------------------------
# BodaiComponentMCPClient — constructor + SSRF guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scheme",
    ["file", "ftp", "gopher", "javascript", "data", ""],
)
def test_bodai_client_rejects_non_http_schemes(scheme: str) -> None:
    """SSRF guard: only http/https are permitted."""
    with pytest.raises(ValueError, match="not allowed"):
        BodaiComponentMCPClient(base_url=f"{scheme}://example.com")


def test_bodai_client_accepts_http_and_https() -> None:
    BodaiComponentMCPClient(base_url="http://example.com/mcp")
    BodaiComponentMCPClient(base_url="https://example.com/mcp")


def test_bodai_client_strips_trailing_slash() -> None:
    client = BodaiComponentMCPClient(base_url="http://example.com/mcp/")
    assert client.base_url == "http://example.com/mcp"


def test_bodai_client_tools_url_matches_base_url() -> None:
    client = BodaiComponentMCPClient(base_url="http://example.com/mcp")
    assert client.tools_url == "http://example.com/mcp"


def test_bodai_client_session_id_is_none_before_session() -> None:
    client = BodaiComponentMCPClient(base_url="http://example.com/mcp")
    assert client.session_id is None


def test_bodai_client_timeout_default_is_30_seconds() -> None:
    client = BodaiComponentMCPClient(base_url="http://example.com/mcp")
    assert client.timeout == 30.0


def test_bodai_client_custom_timeout_is_preserved() -> None:
    client = BodaiComponentMCPClient(base_url="http://example.com/mcp", timeout=5.0)
    assert client.timeout == 5.0


# ---------------------------------------------------------------------------
# query_local_traces — response-shape coercion (the audit-critical logic)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_local_traces_returns_list_response_directly() -> None:
    """Bare-list response is returned as-is."""
    client = BodaiComponentMCPClient(base_url="http://example.com/mcp")
    expected = [{"id": "t1"}, {"id": "t2"}]

    with patch.object(client, "call_tool", AsyncMock(return_value=expected)):
        result = await client.query_local_traces(task_class="code_generation")

    assert result == expected


@pytest.mark.asyncio
async def test_query_local_traces_extracts_traces_key_from_dict() -> None:
    """Dict response with ``traces`` key is unwrapped."""
    client = BodaiComponentMCPClient(base_url="http://example.com/mcp")
    inner = [{"id": "a"}]

    with patch.object(
        client, "call_tool", AsyncMock(return_value={"traces": inner})
    ):
        result = await client.query_local_traces(task_class="reasoning")

    assert result == inner


@pytest.mark.asyncio
async def test_query_local_traces_extracts_items_key_from_dict() -> None:
    """Dict response with ``items`` key (alternate shape) is unwrapped."""
    client = BodaiComponentMCPClient(base_url="http://example.com/mcp")
    inner = [{"id": "b"}]

    with patch.object(
        client, "call_tool", AsyncMock(return_value={"items": inner})
    ):
        result = await client.query_local_traces(task_class="reasoning")

    assert result == inner


@pytest.mark.asyncio
async def test_query_local_traces_extracts_result_key_from_dict() -> None:
    """Dict response with ``result`` key (MCP tool result shape) is unwrapped."""
    client = BodaiComponentMCPClient(base_url="http://example.com/mcp")
    inner = [{"id": "c"}]

    with patch.object(
        client, "call_tool", AsyncMock(return_value={"result": inner})
    ):
        result = await client.query_local_traces(task_class="reasoning")

    assert result == inner


@pytest.mark.asyncio
async def test_query_local_traces_returns_empty_for_unexpected_shape() -> None:
    """Unexpected response shape (e.g. str, dict without known keys) → []."""
    client = BodaiComponentMCPClient(base_url="http://example.com/mcp")

    with patch.object(client, "call_tool", AsyncMock(return_value="not a list")):
        assert await client.query_local_traces(task_class="reasoning") == []

    with patch.object(client, "call_tool", AsyncMock(return_value={"unknown_key": []})):
        assert await client.query_local_traces(task_class="reasoning") == []


@pytest.mark.asyncio
async def test_query_local_traces_passes_through_task_class_and_window() -> None:
    """Both arguments are forwarded to ``call_tool`` unchanged."""
    client = BodaiComponentMCPClient(base_url="http://example.com/mcp")
    mock_call = AsyncMock(return_value=[])

    with patch.object(client, "call_tool", mock_call):
        await client.query_local_traces(task_class="swarm", time_range_minutes=15)

    mock_call.assert_awaited_once_with(
        "query_local_traces",
        {"task_class": "swarm", "time_range_minutes": 15},
    )


@pytest.mark.asyncio
async def test_query_local_traces_default_window_is_60_minutes() -> None:
    client = BodaiComponentMCPClient(base_url="http://example.com/mcp")
    mock_call = AsyncMock(return_value=[])

    with patch.object(client, "call_tool", mock_call):
        await client.query_local_traces(task_class="documentation")

    mock_call.assert_awaited_once_with(
        "query_local_traces",
        {"task_class": "documentation", "time_range_minutes": 60},
    )


# ---------------------------------------------------------------------------
# call_tool — error path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_tool_raises_when_session_not_initialized() -> None:
    """Defensive guard: call_tool must not silently succeed without a session.

    We patch ``_ensure_session`` to a no-op so the guard's runtime
    check actually runs; otherwise the real session setup would
    create a session before the guard executes.
    """
    client = BodaiComponentMCPClient(base_url="http://example.com/mcp")
    # Bypass _ensure_session so the runtime guard is exercised.
    with (
        patch.object(client, "_ensure_session", AsyncMock()),
        pytest.raises(RuntimeError, match="not initialized"),
    ):
        await client.call_tool("query_local_traces", {})


# ---------------------------------------------------------------------------
# aclose — cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_is_safe_when_never_opened() -> None:
    """Calling ``aclose`` on a fresh client must be a safe no-op."""
    client = BodaiComponentMCPClient(base_url="http://example.com/mcp")
    await client.aclose()  # must not raise
    assert client._session is None
    assert client._transport_context is None


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
        await client.list_services(
            service_type="mcp", capability="routing", status="active"
        )

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
    assert fake_client.post.await_args.kwargs["json"]["arguments"]["prefix"] == "component_endpoint/"


@pytest.mark.asyncio
async def test_dhara_aclose_is_noop() -> None:
    """DharaServiceRegistryClient has no resources to release."""
    client = DharaServiceRegistryClient(base_url="http://dhara:8683")
    await client.aclose()  # must not raise
