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

import asyncio
import logging
from contextlib import suppress
from typing import Any

import uvicorn
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
        if not request.url.path.endswith("/tools/call"):
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
        if not request.url.path.endswith("/v1/traces"):
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


class MockBodaiEcosystem:
    """Async context manager that hosts both mocks on ephemeral ports.

    Yields URLs for the Akosha lifespan to consume:
        .session_buddy_url → http://127.0.0.1:<port>/mcp
            (CodeGraphIngester appends /tools/call)
        .otel_endpoint     → http://127.0.0.1:<port>/v1/traces

    ``start()`` blocks until both ASGI servers are listening; ``stop()``
    cancels both servers cleanly. Safe to call ``stop()`` multiple times.
    """

    def __init__(
        self,
        code_graphs: list[dict[str, Any]] | None = None,
        full_graphs: dict[str, dict[str, Any]] | None = None,
        otel_spans: list[dict[str, Any]] | None = None,
    ) -> None:
        self._session_buddy = MockSessionBuddyMCP(
            code_graphs=code_graphs, full_graphs=full_graphs
        )
        self._otel = MockOtelCollector(spans=otel_spans)
        self._sb_server: uvicorn.Server | None = None
        self._otel_server: uvicorn.Server | None = None
        self._sb_task: asyncio.Task[None] | None = None
        self._otel_task: asyncio.Task[None] | None = None
        self._sb_port: int = 0
        self._otel_port: int = 0

    @property
    def session_buddy_url(self) -> str:
        """CodeGraphIngester uses this URL as ``{endpoint}/tools/call``."""
        return f"http://127.0.0.1:{self._sb_port}/mcp"

    @property
    def otel_endpoint(self) -> str:
        """OtelTraceIngester polls this URL as a GET endpoint."""
        return f"http://127.0.0.1:{self._otel_port}/v1/traces"

    @property
    def session_buddy_request_count(self) -> int:
        return self._session_buddy.request_count

    @property
    def otel_request_count(self) -> int:
        return self._otel.request_count

    async def start(self) -> None:
        """Start both ASGI servers on ephemeral ports. Idempotent."""
        if self._sb_server is not None:
            return
        sb_config = uvicorn.Config(
            self._session_buddy,
            host="127.0.0.1",
            port=0,
            log_level="warning",
            lifespan="off",
        )
        self._sb_server = uvicorn.Server(sb_config)
        self._sb_task = asyncio.create_task(self._sb_server.serve())
        # Wait for the server to bind and capture the ephemeral port
        for _ in range(100):
            if self._sb_server.started and self._sb_server.servers:
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("Session-Buddy mock failed to start within 5s")
        self._sb_port = self._sb_server.servers[0].sockets[0].getsockname()[1]

        otel_config = uvicorn.Config(
            self._otel,
            host="127.0.0.1",
            port=0,
            log_level="warning",
            lifespan="off",
        )
        self._otel_server = uvicorn.Server(otel_config)
        self._otel_task = asyncio.create_task(self._otel_server.serve())
        for _ in range(100):
            if self._otel_server.started and self._otel_server.servers:
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("OTel mock failed to start within 5s")
        self._otel_port = self._otel_server.servers[0].sockets[0].getsockname()[1]

    async def stop(self) -> None:
        """Stop both ASGI servers. Idempotent."""
        for server_attr, task_attr in (
            ("_sb_server", "_sb_task"),
            ("_otel_server", "_otel_task"),
        ):
            server = getattr(self, server_attr)
            task = getattr(self, task_attr)
            if server is None:
                continue
            server.should_exit = True
            if task is not None:
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except asyncio.TimeoutError:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                except Exception:
                    logger.warning("Mock server task raised on stop", exc_info=True)
            setattr(self, server_attr, None)
            setattr(self, task_attr, None)

    async def __aenter__(self) -> MockBodaiEcosystem:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.stop()


__all__ = ["MockBodaiEcosystem", "MockOtelCollector", "MockSessionBuddyMCP", "make_canned_span"]


def make_canned_span(
    span_id: str = "b7ad6b7169203331",
    name: str = "test.span",
    start_unix_nano: str = "1700000000000000000",
    task_class: str = "CODE_GENERATION",
) -> dict[str, Any]:
    """Return a single OTel span shaped like OTLP/HTTP JSON.

    ``start_unix_nano`` defaults to a 2023 timestamp; tests that need
    the span to pass a snapshot-poll collector's ``since`` filter
    must pass the current ``time.time_ns()`` explicitly.
    """
    return {
        "traceId": "0af7651916cd43dd8448eb211c80319c",
        "spanId": span_id,
        "name": name,
        "startTimeUnixNano": start_unix_nano,
        "endTimeUnixNano": str(int(start_unix_nano) + 1_000_000),
        "attributes": [
            {"key": "task.class", "value": {"stringValue": task_class}},
        ],
    }
