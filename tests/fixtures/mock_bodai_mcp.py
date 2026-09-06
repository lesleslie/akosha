"""Mock Bodai ecosystem fixtures for end-to-end Akosha MCP tests.

Hosts two small Starlette ASGI apps (Session-Buddy MCP + OTel collector)
on ephemeral ports, seeded with canned data, so the Akosha lifespan can
be exercised end-to-end without a real Bodai component.

Usage:
    async with MockBodaiEcosystem(code_graphs=...) as eco:
        # Point Akosha at eco.session_buddy_url
        ...
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


class MockSessionBuddyMCP:
    """Starlette ASGI app that mimics Session-Buddy's MCP ``/tools/call`` surface.

    Returns canned responses for:
    - ``list_code_graphs`` → ``{"status": "success", "code_graphs": [...]}``
    - ``get_code_graph`` → ``{"status": "success", "repo_path": ..., "graph_data": ..., ...}``

    Records every request in ``request_count`` and ``last_query`` so tests
    can assert the mocks were actually polled.
    """

    def __init__(
        self,
        code_graphs: list[dict[str, Any]] | None = None,
        full_graphs: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.code_graphs = code_graphs or []
        # ``full_graphs`` is keyed by ``f"{repo_path}@{commit_hash}"``.
        self.full_graphs = full_graphs or {}
        self.request_count = 0
        self.last_query: dict[str, Any] | None = None

    async def __call__(
        self, scope: dict[str, Any], receive: Any, send: Any
    ) -> None:
        if scope["type"] != "http":
            return
        request = Request(scope, receive)
        if request.url.path != "/tools/call":
            response = JSONResponse({"error": "not found"}, status_code=404)
            await response(scope, receive, send)
            return
        self.request_count += 1
        try:
            body = await request.json()
        except Exception:
            response = JSONResponse(
                {"status": "error", "message": "invalid JSON body"}, status_code=400
            )
            await response(scope, receive, send)
            return
        self.last_query = body
        name = body.get("name")
        args = body.get("arguments") or {}
        if name == "list_code_graphs":
            response = JSONResponse(
                {"status": "success", "code_graphs": self.code_graphs}
            )
        elif name == "get_code_graph":
            key = f"{args.get('repo_path')}@{args.get('commit_hash')}"
            graph = self.full_graphs.get(key)
            if graph is None:
                response = JSONResponse(
                    {"status": "error", "message": f"unknown graph: {key}"},
                    status_code=200,  # JSON-RPC: error in body, not HTTP
                )
            else:
                response = JSONResponse({"status": "success", **graph})
        else:
            response = JSONResponse(
                {"status": "error", "message": f"unknown tool: {name}"},
                status_code=200,
            )
        await response(scope, receive, send)


class MockOtelCollector:
    """Starlette ASGI app that mimics an OTLP/HTTP collector's ``/v1/traces`` surface.

    Returns canned spans filtered by the ``since`` query parameter. Records
    every request in ``request_count`` and ``last_query``.

    Span shape (canned input → OTLP/HTTP JSON output):
        - Input spans are flat dicts with ``traceId``, ``spanId``, ``name``,
          ``startTimeUnixNano``, ``endTimeUnixNano``, ``attributes``.
        - Output wraps them in ``resourceSpans[].scopeSpans[].spans[]``.
    """

    def __init__(self, spans: list[dict[str, Any]] | None = None) -> None:
        self.spans = spans or []
        self.request_count = 0
        self.last_query: dict[str, str] | None = None

    async def __call__(
        self, scope: dict[str, Any], receive: Any, send: Any
    ) -> None:
        if scope["type"] != "http":
            return
        request = Request(scope, receive)
        if request.url.path != "/v1/traces":
            response = JSONResponse({"error": "not found"}, status_code=404)
            await response(scope, receive, send)
            return
        self.request_count += 1
        self.last_query = dict(request.query_params)
        since_raw = request.query_params.get("since")
        since = int(since_raw) if since_raw is not None else 0
        filtered = [
            s for s in self.spans if int(s.get("startTimeUnixNano", "0")) > since
        ]
        body = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": "akosha"}}
                        ]
                    },
                    "scopeSpans": [{"spans": filtered}],
                }
            ]
        }
        response = JSONResponse(body)
        await response(scope, receive, send)


__all__ = ["MockOtelCollector", "MockSessionBuddyMCP"]
