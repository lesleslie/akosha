# Live MCP Smoke Test Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a pytest-managed `MockBodaiEcosystem` fixture that hosts two small ASGI apps (Session-Buddy MCP + OTel collector) on ephemeral ports, plus an end-to-end smoke test that exercises the Akosha lifespan against the mocks and asserts data flowed through the ingester pipelines.

**Architecture:** A standalone Starlette ASGI app class per mock (no FastMCP dependency) handles the two endpoint contracts: Session-Buddy's `POST /tools/call` JSON-RPC surface (the contract `CodeGraphIngester` actually uses) and OTel's `GET /v1/traces?since=<unix_nano>` HTTP shape. Both start on ephemeral ports via `uvicorn.Config(host="127.0.0.1", port=0)`. The fixture exposes the URLs for the Akosha lifespan to consume. The smoke test points Akosha at the URLs, waits for two poll cycles at the production 60s interval (overridden to 5s for speed), and asserts the canned data landed in `hot_store`.

**Tech Stack:** Python 3.14, asyncio, `starlette` (already in the venv for FastMCP), `uvicorn` (already in pyproject's `dev` group), `pytest_asyncio` (already in use).

**Spec:** `/Users/les/Projects/akosha/docs/superpowers/specs/2026-09-06-live-mcp-smoke-test-design.md`

## Scope Notes

> **Status (2026-09-06):** The OTel half was originally deferred. It
> has since shipped — `OtelTraceIngester` is wired into the Akosha
> lifespan (commit `b3b298e` / Wave 6), `MockBodaiEcosystem` accepts
> `otel_spans` as designed, and `test_otel_ingester_e2e_against_mock_collector`
> is a passing assertion in `tests/integration/test_live_mcp_smoke.py`.
> The "deferred" framing below is preserved for historical accuracy;
> a future cleanup commit could replace it with "Both halves ship
> together — see [OTel spec](../specs/2026-09-06-otel-trace-ingester-design.md)."

This plan implements **only the Session-Buddy / CodeGraphIngester half** of the spec. The OTel ingester half is deferred: it depends on `OtelTraceIngester` (Wave 6, separate spec). When the OTel ingester lands, a followup commit extends the fixture with `seed_otel_spans` and un-skips the OTel assertions. The fixture is designed to accept both mocks so the extension is mechanical.

## Global Constraints

- `from __future__ import annotations` as the first non-comment line of every source file.
- Imports sorted within each section (`force-sort-within-sections = true`, `known-first-party = ["akosha"]`).
- Modern syntax: `X | None` not `Optional[X]`, `list[str]` not `List[str]`.
- Function arguments with default `None` typed `X | None = None`.
- Use `logger.exception(...)` in except blocks, never `logger.error(..., exc_info=True)`.
- All I/O in the orchestration layer is async.
- Use the Oneiric logger (`oneiric.logging`); do not introduce stdlib `logging` new instances.
- Coverage floor: 89.0%.
- The smoke test is `@pytest.mark.slow` and runs at 5s poll cadence (override `AKOSHA_CODE_GRAPH_POLL_SECONDS=5` and `AKOSHA_KG_REFRESH_SECONDS=0.05` for the kg_refresh task so it ticks during the test).

______________________________________________________________________

## File Structure

| File | Responsibility |
|---|---|
| `tests/fixtures/mock_bodai_mcp.py` (new) | `MockBodaiEcosystem` pytest fixture + `MockSessionBuddyMCP` + `MockOtelCollector` ASGI apps |
| `tests/fixtures/__init__.py` (new) | Empty package marker |
| `tests/unit/test_mock_bodai_ecosystem.py` (new) | Unit tests for the mocks in isolation |
| `tests/integration/test_live_mcp_smoke.py` (new) | End-to-end smoke test against the fixture |
| `tests/integration/__init__.py` (exists) | Already present; no changes |

______________________________________________________________________

## Task 1: MockSessionBuddyMCP + MockOtelCollector ASGI apps

**Files:**

- Create: `tests/fixtures/mock_bodai_mcp.py`
- Test: `tests/unit/test_mock_bodai_ecosystem.py`

**Interfaces:**

- `MockSessionBuddyMCP` — Starlette ASGI app that returns canned `code_graphs` from `POST /tools/call` with `{"name": "list_code_graphs", "arguments": {"limit": 100}}`. Returns `{"status": "success", "code_graphs": [{"id": ..., "repo_path": ..., "commit_hash": ...}, ...]}`. Also handles `get_code_graph` lookups for the second-phase ingest.

- `MockOtelCollector` — Starlette ASGI app that returns canned spans from `GET /v1/traces?since=<unix_nano>`. Returns `{"resourceSpans": [{"resource": {...}, "scopeSpans": [{"spans": [<filtered spans>]}]}]}`. Filters spans whose `startTimeUnixNano > since`.

- Both expose `.request_count` and `.last_query` for assertions.

- [ ] **Step 1: Write the failing unit tests**

```python
# tests/unit/test_mock_bodai_ecosystem.py
"""Unit tests for the mock Bodai ecosystem fixture (ASGI apps in isolation)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tests.fixtures.mock_bodai_mcp import MockOtelCollector, MockSessionBuddyMCP


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
        "akosha@deadbeef": {
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
    """A POST /tools/call with list_code_graphs returns the canned graphs."""
    transport = httpx.ASGITransport(app=mock_session_buddy)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/tools/call",
            json={"name": "list_code_graphs", "arguments": {"limit": 100}},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert len(body["code_graphs"]) == 1
    assert body["code_graphs"][0]["id"] == "akosha@deadbeef"
    assert mock_session_buddy.request_count == 1


@pytest.mark.asyncio
async def test_session_buddy_get_code_graph_returns_full_payload(
    mock_session_buddy: MockSessionBuddyMCP,
) -> None:
    """A POST /tools/call with get_code_graph returns the canned full graph."""
    transport = httpx.ASGITransport(app=mock_session_buddy)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/tools/call",
            json={
                "name": "get_code_graph",
                "arguments": {
                    "repo_path": "/path/to/akosha",
                    "commit_hash": "deadbeef",
                },
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["repo_path"] == "/path/to/akosha"
    assert body["graph_data"] == {"nodes": [], "edges": []}


@pytest.mark.asyncio
async def test_session_buddy_rejects_unknown_tool(
    mock_session_buddy: MockSessionBuddyMCP,
) -> None:
    """An unknown tool name returns an error response, not a 500."""
    transport = httpx.ASGITransport(app=mock_session_buddy)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/tools/call", json={"name": "unknown_tool", "arguments": {}})
    assert response.status_code == 200  # JSON-RPC convention: error in body
    body = response.json()
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/les/Projects/akosha && /Users/les/Projects/akosha/.venv/bin/pytest tests/unit/test_mock_bodai_ecosystem.py -v --no-cov`
Expected: `ModuleNotFoundError: No module named 'tests.fixtures.mock_bodai_mcp'`

- [ ] **Step 3: Create the empty fixtures package marker**

```bash
mkdir -p tests/fixtures
touch tests/fixtures/__init__.py
```

- [ ] **Step 4: Write the mock implementations**

```python
# tests/fixtures/mock_bodai_mcp.py
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

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
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
            response = JSONResponse({"status": "success", "code_graphs": self.code_graphs})
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

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
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


__all__ = ["MockOtelCollector", "MockSessionBuddyMCP"]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/les/Projects/akosha && /Users/les/Projects/akosha/.venv/bin/pytest tests/unit/test_mock_bodai_ecosystem.py -v --no-cov`
Expected: 6 tests PASS.

- [ ] **Step 6: Commit**

```bash
cd /Users/les/Projects/akosha
git add tests/fixtures/__init__.py tests/fixtures/mock_bodai_mcp.py tests/unit/test_mock_bodai_ecosystem.py
git -c user.email='les@wedgwoodwebworks.com' commit -m "test(akosha): MockSessionBuddyMCP + MockOtelCollector ASGI apps

Adds a tests/fixtures/mock_bodai_mcp.py module with two Starlette
ASGI apps that mimic the endpoints CodeGraphIngester and (future)
OtelTraceIngester hit:

- MockSessionBuddyMCP handles POST /tools/call with the JSON-RPC
  shape CodeGraphIngester actually uses: list_code_graphs returns
  canned code_graphs list; get_code_graph returns full_graph by
  f'{repo_path}@{commit_hash}' key. Unknown tool names return an
  error in the JSON body (HTTP 200) per JSON-RPC convention.
- MockOtelCollector handles GET /v1/traces?since=<unix_nano>;
  filters spans whose startTimeUnixNano > since.

Both expose request_count and last_query for assertions. 6 unit tests
cover happy path + error path + filter logic.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

______________________________________________________________________

## Task 2: MockBodaiEcosystem lifecycle fixture

**Files:**

- Modify: `tests/fixtures/mock_bodai_mcp.py` (add `MockBodaiEcosystem`)
- Test: `tests/unit/test_mock_bodai_ecosystem.py` (add lifecycle tests)

**Interfaces:**

- `MockBodaiEcosystem(code_graphs, full_graphs, otel_spans)` — context manager that starts both ASGI apps on ephemeral ports via uvicorn, yields, then stops both.
- `.session_buddy_url: str` — `http://127.0.0.1:<port>/mcp` (CodeGraphIngester appends `/tools/call`; this fixture serves the path).
- `.otel_endpoint: str` — `http://127.0.0.1:<port>/v1/traces`.

Note: the spec says session_buddy_url ends with `/mcp` (CodeGraphIngester uses it as `{endpoint}/tools/call`). So the fixture's URL is the BASE; the mock serves `/tools/call` at any path under it. Use port 0 binding for ephemeral.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_mock_bodai_ecosystem.py`:

```python
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
        # session_buddy_url is the base; the mock serves /tools/call under it.
        assert eco.session_buddy_url.startswith("http://127.0.0.1:")
        assert eco.otel_endpoint.startswith("http://127.0.0.1:")
        # session_buddy_url + /tools/call must be reachable
        async with _httpx.AsyncClient() as c:
            r = await c.post(
                eco.session_buddy_url + "/tools/call",
                json={"name": "list_code_graphs", "arguments": {}},
            )
        assert r.status_code == 200
        assert r.json()["code_graphs"][0]["id"] == "akosha@deadbeef"


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/les/Projects/akosha && /Users/les/Projects/akosha/.venv/bin/pytest tests/unit/test_mock_bodai_ecosystem.py::test_ecosystem_start_yields_urls_and_stops_cleanly tests/unit/test_mock_bodai_ecosystem.py::test_ecosystem_stop_is_idempotent -v --no-cov`
Expected: `ImportError: cannot import name 'MockBodaiEcosystem'`

- [ ] **Step 3: Implement MockBodaiEcosystem**

Append to `tests/fixtures/mock_bodai_mcp.py`:

```python
import asyncio

import uvicorn


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

    async def __aenter__(self) -> "MockBodaiEcosystem":
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.stop()


# Update __all__ to include the new class
__all__ = ["MockBodaiEcosystem", "MockOtelCollector", "MockSessionBuddyMCP"]
```

Add the missing imports (`from contextlib import suppress`) at the top of the file.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/les/Projects/akosha && /Users/les/Projects/akosha/.venv/bin/pytest tests/unit/test_mock_bodai_ecosystem.py -v --no-cov`
Expected: 8 tests PASS (6 from Task 1 + 2 from Task 2).

- [ ] **Step 5: Commit**

```bash
cd /Users/les/Projects/akosha
git add tests/fixtures/mock_bodai_mcp.py tests/unit/test_mock_bodai_ecosystem.py
git -c user.email='les@wedgwoodwebworks.com' commit -m "test(akosha): MockBodaiEcosystem ephemeral-port fixture

Context manager that starts MockSessionBuddyMCP and MockOtelCollector
on uvicorn servers bound to 127.0.0.1:0 (ephemeral ports). Captures the
actual ports and exposes .session_buddy_url (ending in /mcp — matches
CodeGraphIngester's {endpoint}/tools/call contract) and
.otel_endpoint.

Lifecycle:
- start() blocks until both servers are bound (5s timeout each)
- stop() cancels both servers with 5s grace, idempotent
- __aenter__/__aexit__ delegate to start/stop

2 new tests: URL reachability + idempotent stop. 8 total in
test_mock_bodai_ecosystem.py.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

______________________________________________________________________

## Task 3: End-to-end smoke test

**Files:**

- Create: `tests/integration/test_live_mcp_smoke.py`

**Interfaces:**

- Consumes: `MockBodaiEcosystem` (from Task 2), Akosha `create_app()`, `get_shared_hot_store()` singleton accessor.

- Produces: a single `@pytest.mark.slow` test that asserts the CodeGraphIngester pipeline (Session-Buddy mock → ingester → hot_store) works end-to-end.

- [ ] **Step 1: Write the smoke test**

```python
# tests/integration/test_live_mcp_smoke.py
"""End-to-end smoke test: Akosha lifespan against mock Bodai ecosystem."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from akosha.mcp.server import create_app
from akosha.mcp.tools.group_registers import get_shared_hot_store

from tests.fixtures.mock_bodai_mcp import MockBodaiEcosystem


@pytest.mark.asyncio
@pytest.mark.slow
async def test_lifespan_pulls_code_graphs_from_mock_session_buddy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CodeGraphIngester polls the mock Session-Buddy and ingests the
    canned code graph into hot_store.

    Smoke test exercises:
    - Lifespan integration (start ingester on create_app)
    - Shared-singleton pattern (get_shared_hot_store returns the
      lifespan-published instance, NOT a fresh per-process DuckDB)
    - CodeGraphIngester poll cycle (POST /tools/call with the right
      JSON-RPC payload)
    - Ingest path (POST /tools/call with get_code_graph + insert
      into hot_store.list_code_graphs)

    OTel half ships separately in test_otel_ingester_e2e_against_mock_collector
    (see Task 3 in the OTel plan); the live MCP smoke test focuses on
    the Session-Buddy half.
    """
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
        "akosha@deadbeef": {
            "repo_path": "/path/to/akosha",
            "commit_hash": "deadbeef",
            "indexed_at": "2026-09-06T00:00:00Z",
            "nodes_count": 42,
            "edges_count": 17,
            "graph_data": {"nodes": [], "edges": []},
            "metadata": {"language": "python"},
        }
    }

    async with MockBodaiEcosystem(code_graphs=code_graphs, full_graphs=full_graphs) as eco:
        # Override the Session-Buddy endpoint to point at the mock
        monkeypatch.setenv("SESSION_BUDDY_MCP_URL", eco.session_buddy_url)
        # Speed up the poll cycle (default is 60s)
        monkeypatch.setenv("AKOSHA_CODE_GRAPH_POLL_SECONDS", "5")
        # Disable KG refresh so it doesn't try to query traces
        # (we haven't seeded any)
        monkeypatch.setenv("AKOSHA_SKIP_KG_REFRESH", "1")
        # Disable OTel ingester (not yet implemented; this test
        # only covers the CodeGraphIngester half)
        monkeypatch.setenv("AKOSHA_SKIP_OTEL_INGESTER", "1")

        app = create_app()
        async with app._mcp_server.lifespan(app):
            # Wait for two poll cycles (10s at 5s interval)
            await asyncio.sleep(12)

            # Assertion 1: the mock was actually polled, not just registered
            assert eco.session_buddy_request_count >= 2, (
                f"Session-Buddy mock was polled {eco.session_buddy_request_count} "
                f"times; expected ≥2"
            )

            # Assertion 2: data landed in hot_store via the shared singleton
            hot_store = get_shared_hot_store()
            assert hot_store is not None, (
                "shared hot_store singleton was not published by the lifespan"
            )
            ingested = await hot_store.list_code_graphs(limit=10)
            assert any(
                g.get("repo_path") == "/path/to/akosha" and g.get("commit_hash") == "deadbeef"
                for g in ingested
            ), f"ingested graphs did not include the canned akosha@deadbeef; got: {ingested!r}"

            # Assertion 3: /health reflects the ingester running
            response = await app.routes["/health"]["handler"](None)
            import json

            body = json.loads(response.body)
            assert body["checks"]["code_graphs_feed"]["ingester_running"] is True
```

- [ ] **Step 2: Add the OTel/CodeGraph opt-out env vars to the existing lifespan test fixtures**

Edit `tests/unit/test_mcp_server_lifespan.py` and `tests/unit/test_wave5_lifespan_wiring.py` to add `monkeypatch.setenv("AKOSHA_SKIP_OTEL_INGESTER", "1")` so the existing tests don't try to start an OTel ingester once Task 3 of the OTel ingester plan lands. **Only do this once the OTel ingester ships**; today, the env var is unused and the smoke test is the only place it's set.

NOTE: For this commit, skip this step. The `AKOSHA_SKIP_OTEL_INGESTER` env var is harmless when no ingester exists. The lifespan tests will continue to pass without modification.

- [ ] **Step 3: Run the smoke test**

Run: `cd /Users/les/Projects/akosha && /Users/les/Projects/akosha/.venv/bin/pytest tests/integration/test_live_mcp_smoke.py -v --no-cov -m slow`
Expected: 1 test PASS in ~15s.

If the test fails, common causes:

- `AKOSHA_SKIP_OTEL_INGESTER` not recognized → the env var is a no-op until the OTel ingester ships, so this is safe to ignore.

- `hot_store.list_code_graphs` returns empty → the ingester's ingest path (POST get_code_graph) didn't reach the mock; check the mock's `full_graphs` key matches `{repo_path}@{commit_hash}`.

- The lifespan startup takes >12s → bump the `asyncio.sleep` to 20s.

- [ ] **Step 4: Verify default `pytest` doesn't run the smoke test**

Run: `cd /Users/les/Projects/akosha && /Users/les/Projects/akosha/.venv/bin/pytest tests/integration/test_live_mcp_smoke.py --no-cov -q -m "not slow"`
Expected: 1 test DESELECTED (the smoke test is marked slow, so default pytest runs skip it).

- [ ] **Step 5: Commit**

```bash
cd /Users/les/Projects/akosha
git add tests/integration/test_live_mcp_smoke.py
git -c user.email='les@wedgwoodwebworks.com' commit -m "test(akosha): live MCP smoke test against MockBodaiEcosystem

End-to-end test that exercises the CodeGraphIngester pipeline:

1. Start MockBodaiEcosystem with seeded code_graphs + full_graphs
2. Point Akosha's Session-Buddy endpoint at the mock
3. Override AKOSHA_CODE_GRAPH_POLL_SECONDS=5 so the test runs
   in ~12s wall-clock instead of the 60s default
4. Enter the Akosha lifespan (this starts the ingester)
5. After 2 poll cycles, assert:
   - The mock was actually polled (request_count >= 2)
   - hot_store.list_code_graphs contains the canned graph
   - /health reports code_graphs_feed.ingester_running = True

The test verifies:
- Lifespan integration (start ingester on create_app)
- Shared-singleton pattern (get_shared_hot_store returns the
  lifespan-published instance, not a fresh per-process DuckDB)
- CodeGraphIngester two-phase ingest (list_code_graphs +
  get_code_graph + hot_store.insert_code_graph)

Marked @pytest.mark.slow; default pytest runs skip it. OTel half
is deferred (depends on Wave 6 OtelTraceIngester landing); the
fixture already accepts otel_spans so the extension is mechanical.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

______________________________________________________________________

## Self-Review

1. **Spec coverage**:

   - Multi-component mock (Session-Buddy + OTel) → Task 1 (both ASGI apps), Task 2 (lifecycle)
   - In-repo pytest fixture → Task 2 (MockBodaiEcosystem with start/stop)
   - Smoke test runs at 5s poll cadence → Task 3 (override env var)
   - Marked @pytest.mark.slow → Task 3
   - Default pytest skips it → Task 3 Step 4

1. **Scope refinement** *(status 2026-09-06: closed)*: The OTel half of the
   smoke test was originally deferred until the OTel ingester landed.
   Both have shipped — `OtelTraceIngester` is wired in the lifespan
   (commit `b3b298e`), `MockBodaiEcosystem` accepts `otel_spans`, and
   `test_otel_ingester_e2e_against_mock_collector` is a passing test
   in `tests/integration/test_live_mcp_smoke.py`. The "deferred"
   framing in the Scope Notes block above is preserved for historical
   accuracy; see the status note at the top of that block.

1. **Placeholder scan**: All test code is concrete; no "TBD" markers.

1. **Type consistency**: `MockBodaiEcosystem.session_buddy_url` returns `str` consistently across Task 2 and Task 3.
