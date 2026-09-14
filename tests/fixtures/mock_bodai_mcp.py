"""Mock Bodai ecosystem fixtures for end-to-end Akosha MCP tests.

Hosts two small Starlette ASGI apps (Session-Buddy MCP server + OTel
collector) on ephemeral ports, seeded with canned data, so the Akosha
lifespan can be exercised end-to-end without a real Bodai component.

The MCP mock speaks the streamable-HTTP transport that
:class:`mcp_common.clients.CommonMCPClient` produces: JSON-RPC envelopes
on POST /mcp, an SSE stream on GET /mcp, and 204 No Content on DELETE
/mcp. Implements the minimal subset needed by REQ-009's cross-repo
smoke test.

Usage:
    async with MockBodaiEcosystem(code_graphs=...) as eco:
        # Point Akosha at eco.session_buddy_url
        ...
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import suppress
from typing import Any

import uvicorn
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


class MockSessionBuddyMCP:
    """Starlette ASGI app that mimics a Bodai MCP server's streamable-HTTP surface.

    Implements the minimal subset of MCP server behavior needed by
    REQ-009's cross-repo smoke test so ``CommonMCPClient.call_tool()``
    reaches canned data without a real Bodai component.

    Endpoints
    ---------
    - ``POST /mcp``:
        JSON-RPC envelope methods ``initialize`` / ``tools/list`` /
        ``tools/call`` / ``ping`` and ``notifications/initialized``.
        ``initialize`` returns 200 + ``mcp-session-id`` header.
        Notifications return 202 Accepted with empty body (no JSON
        envelope — per the MCP spec, the server MUST NOT send a
        response to notifications).
    - ``GET /mcp``:
        text/event-stream; one priming comment line, then immediate
        close. The ``streamable_http_client`` accepts this as a normal
        end-of-stream and reconnects per its own retry policy.
    - ``DELETE /mcp``:
        204 No Content (session termination).
    - Other paths / methods: 404 / 405.

    Records every JSON-RPC request that arrives over POST in
    ``request_count`` + ``last_query`` so tests can assert the mock was
    actually polled (initialize + initialized count as 1 each; tool
    calls each increment by 1).
    """

    def __init__(
        self,
        code_graphs: list[dict[str, Any]] | None = None,
        full_graphs: dict[str, dict[str, Any]] | None = None,
        server_name: str = "mock-session-buddy-mcp",
        server_version: str = "0.0.1-test",
    ) -> None:
        self.code_graphs = code_graphs or []
        # ``full_graphs`` is keyed by ``f"{repo_path}@{commit_hash}"``.
        self.full_graphs = full_graphs or {}
        self.server_name = server_name
        self.server_version = server_version
        # Stable per-instance session ID; clients see this once on the
        # initialize response and reuse for subsequent calls. There is
        # one session per MockSessionBuddyMCP instance.
        self.session_id: str = uuid.uuid4().hex
        self.request_count = 0
        self.last_query: dict[str, Any] | None = None

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        request = Request(scope, receive)
        # The CommonMCPClient passes the full /mcp URL as base_url. Use
        # exact match (not endswith) so a future path like
        # ``/admin-mcp`` doesn't accidentally satisfy this guard.
        if request.url.path != "/mcp":
            response = JSONResponse({"error": "not found"}, status_code=404)
            await response(scope, receive, send)
            return

        http_method = scope["method"]
        if http_method == "DELETE":
            await self._send_raw(send, 204, [(b"content-length", b"0")], b"")
            return

        if http_method == "GET":
            # SSE preamble: status 200 + headers, then a single comment
            # line terminated by an empty line. The streamable_http
            # client's sse_within_origin iterator treats EOF as a normal
            # close and stops.
            await self._send_raw(
                send,
                200,
                [
                    (b"content-type", b"text/event-stream"),
                    (b"cache-control", b"no-cache"),
                    (b"mcp-session-id", self.session_id.encode()),
                    (b"connection", b"close"),
                ],
                b": keep-alive\n\n",
            )
            return

        if http_method != "POST":
            response = JSONResponse({"error": "method not allowed"}, status_code=405)
            await response(scope, receive, send)
            return

        # POST /mcp path: JSON-RPC envelope.
        try:
            body = await request.json()
        except Exception:
            response = JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "invalid JSON"},
                },
                status_code=400,
            )
            await response(scope, receive, send)
            return

        self.request_count += 1
        self.last_query = body
        jsonrpc_id = body.get("id")
        method_name = body.get("method") or ""
        params = body.get("params") or {}

        # Notifications MUST NOT receive a JSON-RPC response. Per spec,
        # the server returns 202 Accepted with an empty body so the
        # client knows the message was received but no reply is coming.
        if method_name.startswith("notifications/"):
            await self._send_raw(send, 202, [(b"content-length", b"0")], b"")
            return

        if method_name == "initialize":
            response = JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": jsonrpc_id,
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {
                            "name": self.server_name,
                            "version": self.server_version,
                        },
                    },
                },
                status_code=200,
                headers={"mcp-session-id": self.session_id},
            )
        elif method_name == "tools/list":
            response = JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": jsonrpc_id,
                    "result": {
                        "tools": [
                            {
                                "name": "list_code_graphs",
                                "description": (
                                    "Return the set of registered "
                                    "code-graph identities."
                                ),
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {},
                                    "additionalProperties": False,
                                },
                            },
                            {
                                "name": "get_code_graph",
                                "description": (
                                    "Return full graph data for a "
                                    "repo_path+commit_hash key."
                                ),
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "repo_path": {"type": "string"},
                                        "commit_hash": {"type": "string"},
                                    },
                                    "required": ["repo_path", "commit_hash"],
                                    "additionalProperties": False,
                                },
                            },
                        ],
                    },
                },
                status_code=200,
            )
        elif method_name == "tools/call":
            response = self._handle_tool_call(jsonrpc_id, params)
        elif method_name == "ping":
            response = JSONResponse(
                {"jsonrpc": "2.0", "id": jsonrpc_id, "result": {}},
                status_code=200,
            )
        else:
            response = JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": jsonrpc_id,
                    "error": {
                        "code": -32601,
                        "message": f"Method not found: {method_name}",
                    },
                },
                status_code=200,
            )
        await response(scope, receive, send)

    @staticmethod
    async def _send_raw(
        send: Any,
        status: int,
        headers: list[tuple[bytes, bytes]],
        body: bytes,
    ) -> None:
        """Send a response via raw ASGI send (used for non-JSON or
        no-content responses like the SSE preamble and 204 deletes).
        """
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": headers,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body,
                "more_body": False,
            }
        )

    def _handle_tool_call(
        self,
        jsonrpc_id: Any,
        params: dict[str, Any],
    ) -> JSONResponse:
        """Dispatch a JSON-RPC ``tools/call`` to canned-data handlers.

        Tool results are wrapped per the MCP spec:
        ``{"content": [{"type": "text", "text": "<json>"}], "isError": bool}``.
        The ``text`` field is a JSON-encoded string of the legacy
        ``{"status": ..., "...": ...}`` payload that older callers
        (e.g. ``CodeGraphIngester._extract_status_payload``) decode from
        the envelope.
        """
        tool_name = params.get("name")
        tool_args = params.get("arguments") or {}

        if tool_name == "list_code_graphs":
            text = json.dumps({"status": "success", "code_graphs": self.code_graphs})
            return self._tool_result(jsonrpc_id, text, is_error=False)

        if tool_name == "get_code_graph":
            key = f"{tool_args.get('repo_path')}@{tool_args.get('commit_hash')}"
            graph = self.full_graphs.get(key)
            if graph is None:
                text = json.dumps(
                    {"status": "error", "message": f"unknown graph: {key}"}
                )
                return self._tool_result(jsonrpc_id, text, is_error=True)
            text = json.dumps({"status": "success", **graph})
            return self._tool_result(jsonrpc_id, text, is_error=False)

        # Unknown tool: still return a 200 + isError=true envelope so the
        # client surfaces the error rather than retrying on transport.
        text = json.dumps(
            {"status": "error", "message": f"unknown tool: {tool_name}"}
        )
        return self._tool_result(jsonrpc_id, text, is_error=True)

    @staticmethod
    def _tool_result(
        jsonrpc_id: Any,
        text: str,
        *,
        is_error: bool,
    ) -> JSONResponse:
        """Wrap a canned tool result in the MCP-spec content envelope."""
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": jsonrpc_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "isError": is_error,
                },
            },
            status_code=200,
        )


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

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        request = Request(scope, receive)
        # OtelTraceIngester polls ``self.otlp_endpoint`` which is set to
        # ``http://127.0.0.1:<port>/v1/traces`` by MockBodaiEcosystem.
        # Exact match so a future path like ``/admin-v1/traces`` can't
        # accidentally satisfy this guard.
        if request.url.path != "/v1/traces":
            response = JSONResponse({"error": "not found"}, status_code=404)
            await response(scope, receive, send)
            return
        self.request_count += 1
        self.last_query = dict(request.query_params)
        since_raw = request.query_params.get("since")
        since = int(since_raw) if since_raw is not None else 0
        filtered = [s for s in self.spans if int(s.get("startTimeUnixNano", "0")) > since]
        body = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [{"key": "service.name", "value": {"stringValue": "akosha"}}]
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
        self._session_buddy = MockSessionBuddyMCP(code_graphs=code_graphs, full_graphs=full_graphs)
        self._otel = MockOtelCollector(spans=otel_spans)
        self._sb_server: uvicorn.Server | None = None
        self._otel_server: uvicorn.Server | None = None
        self._sb_task: asyncio.Task[None] | None = None
        self._otel_task: asyncio.Task[None] | None = None
        self._sb_port: int = 0
        self._otel_port: int = 0

    @property
    def session_buddy_url(self) -> str:
        """Full MCP streamable-HTTP URL (consumer passes it directly to
        ``CommonMCPClient(base_url=..., ...)`` — no path appending).
        """
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
