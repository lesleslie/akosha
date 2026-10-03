# Live MCP Server Smoke Test Design

**Status**: complete (2026-09-06)
**Source**: Wave 5 followup #5 (architectural)
**Wave**: post-Wave 5 (Wave 6 candidate)

## Context

The Wave 5 hardening closed the `mcp-surface-health-illusion` failure mode
by wiring `CodeGraphIngester` + kg population in the Akosha MCP lifespan.
The wiring is exercised by `tests/unit/test_wave5_lifespan_wiring.py`,
but those tests **mock** the Session-Buddy endpoint — they verify that the
ingester's polling loop runs, not that it actually pulls data from a
remote Session-Buddy and ingests it into `hot_store`.

Today there is no end-to-end test that proves:

- The Akosha lifespan starts a real HTTP polling loop
- The poll loop successfully retrieves data from a Session-Buddy-shaped endpoint
- The retrieved data lands in `hot_store` in the expected shape
- The `/health` endpoint reflects the new ingesters' running state

The Wave 5 followup #5 explicitly deferred this work pending a "mock
Session-Buddy MCP server". This spec designs that mock and the smoke test
that consumes it.

The same pattern applies to the Wave 6 OTel trace ingester (see
`2026-09-06-otel-trace-ingester-design.md`). This spec mocks **both**
ingester endpoints so the smoke test exercises both feed paths.

## Approach

A **pytest-managed mock ecosystem** fixture that starts two small ASGI
apps (Session-Buddy MCP and OTel collector) on ephemeral ports, seeded
with canned data. The Akosha MCP lifespan runs against the mock URLs.
After a fixed number of poll cycles, the test asserts that the seeded
data has landed in `hot_store` and the `/health` endpoint reflects both
ingesters running.

## Components

### `tests/fixtures/mock_bodai_mcp.py` (new)

```python
class MockSessionBuddyMCP:
    """FastMCP-shaped mock that returns canned responses for the
    CodeGraphIngester's `list_code_graphs` call."""

    def __init__(self, code_graphs: list[dict[str, Any]] | None = None) -> None:
        self.code_graphs = code_graphs or []
        self.request_count = 0

    async def handle_list_code_graphs(self) -> dict[str, Any]:
        self.request_count += 1
        return {"graphs": self.code_graphs, "next_cursor": None}


class MockOtelCollector:
    """OTLP/HTTP-shaped mock that returns canned spans for the
    OtelTraceIngester's GET /v1/traces poll."""

    def __init__(self, spans: list[dict[str, Any]] | None = None) -> None:
        self.spans = spans or []
        self.request_count = 0
        self.last_query: dict[str, str] | None = None

    async def handle_get_traces(self, query: dict[str, str]) -> dict[str, Any]:
        self.request_count += 1
        self.last_query = query
        # OTLP/HTTP shape: return only spans newer than the watermark
        since = query.get("since")
        if since is None:
            return {"resourceSpans": [_wrap_span(s) for s in self.spans]}
        return {
            "resourceSpans": [
                _wrap_span(s) for s in self.spans if int(s["start_time_unix_nano"]) > int(since)
            ]
        }


class MockBodaiEcosystem:
    """Test fixture that hosts both mocks on ephemeral ports."""

    def __init__(
        self,
        code_graphs: list[dict[str, Any]] | None = None,
        otel_spans: list[dict[str, Any]] | None = None,
    ) -> None: ...

    @property
    def session_buddy_url(self) -> str:
        """http://127.0.0.1:<port>/mcp — for AKOSHA to consume."""

    @property
    def otel_endpoint(self) -> str:
        """http://127.0.0.1:<port>/v1/traces — for AKOSHA to consume."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    @property
    def session_buddy_request_count(self) -> int: ...
    @property
    def otel_request_count(self) -> int: ...
```

The fixture uses `pytest-httpx` or a minimal `aiohttp` ASGI runner on
ephemeral ports (`127.0.0.1:0`). `start()` blocks until both servers are
listening; `stop()` cancels both ASGI apps cleanly.

### `tests/integration/test_live_mcp_smoke.py` (new)

```python
@pytest.mark.asyncio
@pytest.mark.slow  # 2 poll cycles * 60s default = 120s; CI uses AKOSHA_OTEL_POLL_SECONDS=5
async def test_live_mcp_smoke_pulls_code_graphs_and_otel_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. Seed canned data
    code_graphs = [_make_code_graph(repo="akosha", nodes=42)]
    spans = [_make_otel_span(system="akosha", task_class="CODE_GENERATION")]

    async with MockBodaiEcosystem(code_graphs=code_graphs, otel_spans=spans) as eco:
        # 2. Configure Akosha to consume the mock URLs
        monkeypatch.setenv("SESSION_BUDDY_MCP_URL", eco.session_buddy_url)
        monkeypatch.setenv("AKOSHA_OTLP_ENDPOINT", eco.otel_endpoint)
        # Faster poll cadence so the test runs in seconds, not minutes
        monkeypatch.setenv("AKOSHA_CODE_GRAPH_POLL_SECONDS", "5")
        monkeypatch.setenv("AKOSHA_OTEL_POLL_SECONDS", "5")

        # 3. Run the Akosha lifespan
        app = create_app()
        async with app.lifespan(app):
            # 4. Wait two poll cycles (10s at 5s interval)
            await asyncio.sleep(12)

            # 5. Assert the mocks were actually polled (not just registered)
            assert eco.session_buddy_request_count >= 2
            assert eco.otel_request_count >= 2

            # 6. Assert the data landed in hot_store
            # (use the lifespan-published shared_hot_store singleton)
            hot_store = get_shared_hot_store()
            assert hot_store is not None
            graphs = await hot_store.list_code_graphs()
            assert any(g["repo_path"] == "akosha" for g in graphs)
            traces = await hot_store.query_traces(system_id="akosha")
            assert any(
                t["metadata"]["attributes"]["task_class"] == "CODE_GENERATION" for t in traces
            )

            # 7. Assert /health reflects both ingesters running
            response = await app.routes["/health"]["handler"](None)
            body = json.loads(response.body)
            assert body["checks"]["code_graphs_feed"]["ok"] is True
            assert body["checks"]["knowledge_graph_feed"]["ok"] is True
            assert body["checks"]["local_traces_feed"]["ok"] is True
```

## Data flow

```
┌─────────────────────────────────────────────────────────────────┐
│  MockBodaiEcosystem (test fixture, in-process)                  │
│  ┌─────────────────────┐  ┌─────────────────────┐              │
│  │ MockSessionBuddyMCP │  │ MockOtelCollector   │              │
│  │ 127.0.0.1:R1/mcp    │  │ 127.0.0.1:R2/v1/traces              │
│  │ GET list_code_graphs│  │ GET spans?since=... │              │
│  └─────────┬───────────┘  └─────────┬───────────┘              │
└────────────┼──────────────────────┼───────────────────────────┘
             │                       │
             │ HTTP                  │ HTTP
             ▼                       ▼
┌─────────────────────────────────────────────────────────────────┐
│  Akosha MCP server (create_app() under test)                    │
│  Lifespan owns:                                                 │
│    • CodeGraphIngester  → polls MockSessionBuddyMCP             │
│    • OtelTraceIngester  → polls MockOtelCollector               │
│    • _kg_refresh_loop   → feeds hot_store into KnowledgeGraph   │
│  Shared singleton: hot_store (via set_shared_hot_store)         │
└────────────┬─────────────────────────────────────────────────────┘
             │
             │ hot_store.list_code_graphs() / .query_traces()
             ▼
        Test assertions
```

## Error handling

The fixture is **fail-fast on startup**:

- If port binding fails, raise `RuntimeError` immediately
- If the ASGI app fails to start within 5 seconds, raise

The fixture is **lenient on shutdown**:

- `stop()` swallows cancellation errors (logs + continues)
- Resource leaks (open sockets) are acceptable because pytest reaps the
  interpreter after the test

## Testing

### Unit tests for the fixture itself

- `tests/unit/test_mock_bodai_ecosystem.py` (new):
  - `start()` returns URLs with valid `127.0.0.1:R{non-zero}/` shape
  - `MockSessionBuddyMCP.handle_list_code_graphs` returns canned data
  - `MockOtelCollector.handle_get_traces` filters by `since` parameter
  - `stop()` is idempotent (safe to call twice)

### Integration test (the smoke test itself)

- `tests/integration/test_live_mcp_smoke.py` (new, 1 test, `@pytest.mark.slow`)

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `AKOSHA_CODE_GRAPH_POLL_SECONDS` | `60` | Already exists; smoke test overrides to `5` for speed |
| `AKOSHA_OTEL_POLL_SECONDS` | `60` | New in Wave 6; smoke test overrides to `5` for speed |
| `SESSION_BUDDY_MCP_URL` | `http://localhost:8678/mcp` | Already exists; smoke test overrides to mock URL |

## CI integration

- The integration test is marked `@pytest.mark.slow`. Default pytest
  invocations (`pytest`) skip it via `-m "not slow"`.
- A new CI workflow step runs `pytest -m slow tests/integration/test_live_mcp_smoke.py`
  on every push.
- The test takes ~15s wall-clock at 5s poll cadence.
- No new CI infrastructure (no sidecar containers, no docker-compose).

## Out of scope

- Cross-process ingester coordination (multi-Akosha deployments).
- Real OTLP collector compatibility (the mock returns canned JSON, not
  protocol-conformant OTLP Protobuf).
- Session-Buddy's full MCP API surface (only `list_code_graphs` is mocked).
- Performance benchmarking (no throughput or latency assertions).
