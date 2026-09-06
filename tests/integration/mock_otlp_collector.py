"""Minimal OTLP/HTTP-shaped mock for integration testing."""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse


class MockOtelCollector:
    """Returns canned OTel spans from GET /v1/traces?since=<unix_nano>."""

    def __init__(self, spans: list[dict[str, Any]] | None = None) -> None:
        self.spans = spans or []
        self.request_count = 0
        self.last_query: dict[str, str] | None = None

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
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


def make_canned_span(
    span_id: str = "b7ad6b7169203331",
    name: str = "test.span",
    start_unix_nano: str = "1700000000000000000",
    task_class: str = "CODE_GENERATION",
) -> dict[str, Any]:
    """Return a single OTel span shaped like OTLP/HTTP JSON."""
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
