"""Unit tests for the mock Bodai ecosystem fixture (ASGI apps in isolation)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tests.fixtures.mock_bodai_mcp import (
    MockBodaiEcosystem,
    MockOtelCollector,
    MockSessionBuddyMCP,
)


@pytest.fixture
def mock_session_buddy() -> MockSessionBuddyMCP:
    code_graphs = [
        {
            "id": "akosha@deadbeef",
            "repo_path": "/path/to/akosha",
            "commit_hash": "deadbeef",
            "indexed_at": "2026-09-06T00:00:00Z",
            "nodes_count": 42,
            "edges_count": 17,
        }
    ]
    full_graphs = {
        "/path/to/akosha@deadbeef": {
            "repo_path": "/path/to/akosha",
            "commit_hash": "deadbeef",
            "indexed_at": "2026-09-06T00:00:00Z",
            "nodes_count": 42,
            "edges_count": 17,
            "graph_data": {"nodes": [], "edges": []},
            "metadata": {"language": "python"},
        }
    }
    return MockSessionBuddyMCP(code_graphs=code_graphs, full_graphs=full_graphs)


@pytest.mark.asyncio
async def test_session_buddy_returns_canned_code_graphs(
    mock_session_buddy: MockSessionBuddyMCP,
) -> None:
    """A POST /mcp JSON-RPC tools/call with list_code_graphs returns the canned graphs."""
    transport = httpx.ASGITransport(app=mock_session_buddy)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "list_code_graphs",
                    "arguments": {"limit": 100},
                },
            },
        )
    assert response.status_code == 200
    rpc = response.json()
    assert rpc.get("jsonrpc") == "2.0"
    content = rpc["result"]["content"][0]
    body = json.loads(content["text"])
    assert body["status"] == "success"
    assert len(body["code_graphs"]) == 1
    assert body["code_graphs"][0]["id"] == "akosha@deadbeef"
    assert mock_session_buddy.request_count == 1


@pytest.mark.asyncio
async def test_session_buddy_get_code_graph_returns_full_payload(
    mock_session_buddy: MockSessionBuddyMCP,
) -> None:
    """A POST /mcp JSON-RPC tools/call with get_code_graph returns the canned full graph."""
    transport = httpx.ASGITransport(app=mock_session_buddy)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "get_code_graph",
                    "arguments": {
                        "repo_path": "/path/to/akosha",
                        "commit_hash": "deadbeef",
                    },
                },
            },
        )
    assert response.status_code == 200
    rpc = response.json()
    content = rpc["result"]["content"][0]
    body = json.loads(content["text"])
    assert body["status"] == "success"
    assert body["repo_path"] == "/path/to/akosha"
    assert body["graph_data"] == {"nodes": [], "edges": []}


@pytest.mark.asyncio
async def test_session_buddy_rejects_unknown_tool(
    mock_session_buddy: MockSessionBuddyMCP,
) -> None:
    """An unknown tool name returns isError=true, not 500."""
    transport = httpx.ASGITransport(app=mock_session_buddy)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "unknown_tool", "arguments": {}},
            },
        )
    assert response.status_code == 200  # JSON-RPC convention: error in body
    rpc = response.json()
    assert rpc["result"]["isError"] is True
    content = rpc["result"]["content"][0]
    body = json.loads(content["text"])
    assert body["status"] == "error"


@pytest.fixture
def mock_otel() -> MockOtelCollector:
    spans = [
        {
            "traceId": "0af7651916cd43dd8448eb211c80319c",
            "spanId": "b7ad6b7169203331",
            "name": "test.span",
            "startTimeUnixNano": "1700000000000000000",
            "endTimeUnixNano": "1700000000001000000",
            "attributes": [
                {"key": "task.class", "value": {"stringValue": "CODE_GENERATION"}},
            ],
        }
    ]
    return MockOtelCollector(spans=spans)


@pytest.mark.asyncio
async def test_otel_returns_all_spans_when_no_since(
    mock_otel: MockOtelCollector,
) -> None:
    """GET /v1/traces with no since parameter returns all canned spans."""
    transport = httpx.ASGITransport(app=mock_otel)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/traces")
    assert response.status_code == 200
    body = response.json()
    spans = body["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(spans) == 1


@pytest.mark.asyncio
async def test_otel_filters_spans_by_since(
    mock_otel: MockOtelCollector,
) -> None:
    """GET /v1/traces?since=<n> filters out spans with startTimeUnixNano <= n."""
    transport = httpx.ASGITransport(app=mock_otel)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/traces", params={"since": "1700000000000000000"})
    assert response.status_code == 200
    spans = response.json()["resourceSpans"][0]["scopeSpans"][0]["spans"]
    # The canned span has startTimeUnixNano == 1700000000000000000, which is NOT
    # greater than since, so it must be filtered out.
    assert spans == []


@pytest.mark.asyncio
async def test_ecosystem_start_yields_urls_and_stops_cleanly() -> None:
    """The fixture starts both mocks, yields reachable URLs, stops cleanly."""
    import httpx as _httpx

    code_graphs = [{"id": "akosha@deadbeef", "repo_path": "/a", "commit_hash": "deadbeef"}]
    spans = [
        {
            "traceId": "x",
            "spanId": "y",
            "name": "z",
            "startTimeUnixNano": "1",
            "endTimeUnixNano": "2",
            "attributes": [],
        }
    ]
    async with MockBodaiEcosystem(code_graphs=code_graphs, otel_spans=spans) as eco:
        # session_buddy_url ends in /mcp. The mock speaks streamable-HTTP:
        # POST /mcp carries a JSON-RPC envelope; tool calls dispatch via
        # method="tools/call" with params.name/params.arguments.
        assert eco.session_buddy_url.startswith("http://127.0.0.1:")
        assert eco.otel_endpoint.startswith("http://127.0.0.1:")
        async with _httpx.AsyncClient() as c:
            r = await c.post(
                eco.session_buddy_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "list_code_graphs",
                        "arguments": {},
                    },
                },
            )
        assert r.status_code == 200
        rpc = r.json()
        content = rpc["result"]["content"][0]
        body = json.loads(content["text"])
        assert body["code_graphs"][0]["id"] == "akosha@deadbeef"


@pytest.mark.asyncio
async def test_ecosystem_stop_is_idempotent() -> None:
    """Calling stop twice (or after exit) does not raise."""
    eco = MockBodaiEcosystem()
    # Exit the context cleanly
    async with eco:
        pass
    # A second exit is a no-op
    await eco.stop()
    await eco.stop()
