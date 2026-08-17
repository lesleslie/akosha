# Akosha Memory Architecture

> **Status**: Living document. Updated whenever the storage schema, MCP surface, or integration contracts change.
> **Audience**: Bodai ecosystem contributors, Claude Code users, and downstream consumers (Session-Buddy, Dhara, Mahavishnu, Crackerjack).
> **Source of truth**: The runtime schema in `akosha/storage/models.py` and `akosha/storage/hot_store.py`, and the MCP tool implementations in `akosha/mcp/tools/`.

Akosha is the **Seer / intelligence** component of the Bodai ecosystem. It
owns a tiered memory store (hot / warm / cold), a derived analytics
layer (time-series, anomaly, correlation, changepoint), a knowledge
graph of cross-system entities, a fitness analyzer that feeds the
routing loop in Mahavishnu, and a unified MCP surface that every other
component queries but rarely writes to.

This document describes what Akosha stores, who reads and writes it,
and the integration contracts the rest of the ecosystem depends on.
The three contract bugs captured below were the trigger for writing it
— they all stemmed from undocumented expectations about how the tier
schema, the ingestion pipelines, and the EventBridge publisher line up.

______________________________________________________________________

## Table of Contents

1. [Storage Inventory](#1-storage-inventory)
1. [MCP Write Surface](#2-mcp-write-surface)
1. [MCP Read Surface](#3-mcp-read-surface)
1. [Cross-Component Visibility](#4-cross-component-visibility)
1. [Integration Contract](#5-integration-contract)
1. [Sample Queries](#6-sample-queries)
1. [Diagrams](#7-diagrams)
1. [Operational Notes](#8-operational-notes)

______________________________________________________________________

## 1. Storage Inventory

Akosha persists state across **three storage tiers** plus an in-process
analytics layer. The single anchor point for cross-tier joins is
**`conversation_id`** — every record carries one and downstream tools
filter on it. Each tier is a different DuckDB instance (plus an
optional pgvector collection) with a different write cost and recall
profile.

| Tier | Engine | Retention | Vector format | What it holds | Owner |
|------|--------|-----------|---------------|---------------|-------|
| **Hot** (`HotStore`) | DuckDB in-memory (`:memory:`) **or** pgvector via `PgvectorHotStore` when `AKOSHA__STORAGE__HOT__BACKEND=pgvector` | ~7 days (age cutoff default in `AgingService.migrate_hot_to_warm`) | `FLOAT[384]` with HNSW index (`m=16, ef_construction=200`) | Full content + raw embedding + code graphs | `akosha/storage/hot_store.py`, `akosha/storage/pgvector_hot_store.py` |
| **Warm** (`WarmStore`) | DuckDB on-disk at `${AKOSHA_DATA_PATH}/warm/warm.db` (resolved by `StoragePathResolver`) | 7–90 days | `INT8[384]` (75% size reduction) + 3-sentence extractive summary | Compressed embeddings + summaries | `akosha/storage/warm_store.py` |
| **Cold** (`ColdStore`) | Parquet (snappy) → S3/R2 via Oneiric adapter (TODO; current code logs and unlinks) | 90+ days | MinHash fingerprint + single-sentence summary | Archived parquet batches | `akosha/storage/cold_store.py` |
| **Code graphs** | DuckDB table inside `HotStore` (`code_graphs`) | Refreshed per commit | JSON graph blob | Per-repo AST snapshots | `HotStore.initialize_code_graphs_table` / `store_code_graph` |
| **Analytics buffer** (`FitnessAnalyzer`) | Bounded in-process `deque(maxlen=1000)` | Until flushed | n/a | Per-(task_class, selector) `FitnessSignal` ready to write to Dhara | `akosha/processing/fitness_analyzer.py` |
| **Knowledge graph** (`KnowledgeGraphBuilder`) | In-process `dict` + `list` (no persistence yet) | Process lifetime | n/a | `GraphEntity` + `GraphEdge` extracted from conversations | `akosha/processing/knowledge_graph.py` |

The `HotStore` is created via the `create_hot_store()` factory in
`akosha/storage/__init__.py`, which reads
`AKOSHA__STORAGE__HOT__BACKEND` (`duckdb-memory` / `duckdb-ssd` /
`pgvector`) and `AKOSHA__STORAGE__HOT__PG_URL`. Both backends expose the
same interface (`initialize`, `insert`, `search_similar`, `query_traces`,
`store_code_graph`, `get_code_graph`, `list_code_graphs`), so the rest
of Akosha does not branch on backend.

### Schema map

The diagram below shows the tier model and the in-flight aging
relationship. Green nodes are the authoritative read targets; yellow
nodes are derived/compressed; the red node is the (still-stubbed)
cold exporter.

```mermaid
erDiagram
    %% Hot tier (DuckDB in-memory or pgvector)
    HotStore ||--o{ conversations : "holds"
    HotStore ||--o{ code_graphs : "holds"
    conversations {
        string system_id
        string conversation_id PK
        text content
        float embedding "FLOAT[384] HNSW indexed"
        timestamp timestamp
        json metadata
        string content_hash
        timestamp uploaded_at
    }
    code_graphs {
        string repo_path
        string commit_hash
        int nodes_count
        json graph_data
        json metadata
        timestamp ingested_at
    }

    %% Aging service moves hot -> warm
    AgingService }|..|{ HotStore : "reads & deletes"
    AgingService }|..|{ WarmStore : "writes"

    %% Warm tier (DuckDB on-disk)
    WarmStore ||--o{ warm_conversations : "holds"
    warm_conversations {
        string system_id
        string conversation_id PK
        int embedding "INT8[384] quantized"
        text summary "3-sentence extractive"
        timestamp timestamp
        json metadata
        timestamp uploaded_at
    }

    %% Cold tier (Parquet to S3/R2; S3 upload is TODO)
    WarmStore ||..o{ ColdStore : "promotes via future warm->cold service"
    ColdStore ||--o{ parquet_batches : "exports"
    parquet_batches {
        string system_id
        string conversation_id
        binary fingerprint "MinHash"
        string ultra_summary
        timestamp timestamp
        string daily_metrics "JSON string"
    }

    %% Fitness analyzer (in-process, writes to Dhara externally)
    FitnessAnalyzer ||--o{ buffer_entries : "holds (deque maxlen=1000)"
    buffer_entries {
        string task_class
        string selector
        float signal_score
        float failure_rate
        float p99_latency_ms
        int samples
        int attempt
    }

    %% Knowledge graph (in-process, no persistence)
    KnowledgeGraphBuilder ||--o{ GraphEntity : "holds"
    KnowledgeGraphBuilder ||--o{ GraphEdge : "holds"
    GraphEntity {
        string entity_id PK
        string entity_type "user|project|system|concept|error"
        json properties
        string source_system
    }
    GraphEdge {
        string source_id FK
        string target_id FK
        string edge_type "worked_on|contains|related_to|fixed|similar_to|mentioned"
        float weight
        json properties
        timestamp timestamp
        string source_system
    }

    %% External writes — FitnessAnalyzer pushes to Dhara KV
    FitnessAnalyzer }|..|{ Dhara : "writes routing_fitness/{tc}/{selector}"

    style HotStore fill:#dfd,stroke:#383
    style conversations fill:#dfd,stroke:#383
    style code_graphs fill:#ffd,stroke:#993
    style warm_conversations fill:#ffd,stroke:#993
    style parquet_batches fill:#fdd,stroke:#933
    style buffer_entries fill:#dde,stroke:#338
    style GraphEntity fill:#eee,stroke:#666
    style GraphEdge fill:#eee,stroke:#666
```

### Per-tier ownership map

| Tier / object | Read by | Written by | Retention / aging |
|---------------|---------|------------|-------------------|
| `HotStore.conversations` | `search_all_systems`, `query_local_traces`, `find_similar_repositories`, `get_cross_repo_function_usage` (via code graphs), PyCharm search fallback | `store_memory`, `batch_store_memories`, `IngestionWorker._process_upload`, `system_endpoint/` uploads | Aging pushes ≥7d records to warm; rows deleted from hot after warm insert |
| `HotStore.code_graphs` | `list_ingested_code_graphs`, `get_code_graph_details`, `find_similar_repositories`, `get_cross_repo_function_usage`, `search_code_patterns`, `get_code_problems`, `find_function_usage`, `analyze_imports` | Mahavishnu indexer via `IngestionWorker` (JSON payload pushed into `store_code_graph`) | Refreshed per commit; `INSERT OR REPLACE` keyed on `(repo_path, commit_hash)` |
| `WarmStore.conversations` | (planned) warm-tier semantic recall; currently read indirectly via `get_migration_stats` | `AgingService._migrate_batch` only | 7–90 days; no eviction yet (warm→cold not implemented) |
| `ColdStore` parquet batches | (planned) bulk recall via DuckDB external scan of S3 bucket | (planned) warm→cold exporter; current `export_batch` writes a temp Parquet then logs the S3 key — upload is TODO | 90+ days; partitioned by `system_id/YYYY/MM/DD` |
| `FitnessAnalyzer` buffer | `run_fitness_analysis` (returns snapshot), `_flush_buffer` (writes to Dhara) | `_analyze_and_persist` (every `poll_interval` seconds; default 60) | Maxlen 1000 entries; DLQ after 3 consecutive Dhara write failures |
| `KnowledgeGraphBuilder.entities` / `.edges` | `query_knowledge_graph`, `find_path`, `get_graph_statistics` | `KnowledgeGraphBuilder.add_to_graph` (called after `extract_entities` + `extract_relationships` per conversation) | Process lifetime; no disk persistence yet |

### Path resolution

`StoragePathResolver` (`akosha/storage/path_resolver.py`) maps the
deployment environment to concrete paths. Environment detection priority:

1. `AKOSHA_ENV` if set (`container` / `local` / `development` / `test`).
1. `/.dockerenv` or `/proc/1/cgroup` → `container`.
1. `pytest` in `sys.modules` or `PYTEST_CURRENT_TEST` → `test`.
1. Otherwise `local`.

The resolver returns:

- `container`: `AKOSHA_DATA_PATH` or `/data/akosha`.
- `local`: `$XDG_DATA_HOME/akosha` (Linux), `~/Library/Application Support/akosha` (macOS), `%LOCALAPPDATA%\akosha` (Windows).
- `development`: `<project>/.akosha/data`.
- `test`: `/tmp/akosha/test`.

Within that base path, the warm store lives at `warm/warm.db`, the
hot WAL at `wal/`, the cold cache at `cold/cache/`, config at
`config/`, and the model cache at `cache/`. Any of these can be
overridden by env vars (`AKOSHA_WARM_PATH`, `AKOSHA_WAL_PATH`,
`AKOSHA_DATA_PATH`).

______________________________________________________________________

## 2. MCP Write Surface

Akosha is **mostly read-only** for other components. The write surface
is small, gated by profile, and either pushes data into the hot tier or
publishes analytics events onto the Bodai EventBridge.

| Tool | Tier | Caller (typical) | What it writes |
|------|------|------------------|----------------|
| `store_memory` | Hot | Session-Buddy HTTP push (push-based ingestion) | One row in `HotStore.conversations` via `HotRecord` |
| `batch_store_memories` | Hot | Session-Buddy bulk sync | Up to 1000 rows in `HotStore.conversations` per call |
| `publish_to_eventbridge` | Events | Akosha analytics paths (`/vishnu`, Conscious Agent) | One `EventEnvelope` on the Bodai EventBridge; topics: `pattern.detected`, `anomaly.detected`, `insight.generated`, `aggregation.completed` |
| `run_fitness_analysis` (side effect) | Fitness | Manual trigger / dashboards | One flush of the `FitnessAnalyzer` buffer to Dhara at `routing_fitness/{task_class}/{selector}` |
| `_register_component_to_dhara` (lifespan side effect) | Discovery | Akosha MCP server startup | `component_endpoint/akosha` key in Dhara KV; 5-minute heartbeat |
| `IngestionWorker._process_upload` (non-MCP side effect) | Hot | S3/R2 poller (cloud mode only) | Multiple rows + a `code_graphs` row from one uploaded Session-Buddy snapshot |
| `_populate_component_endpoints_from_dhara` (startup side effect) | Discovery | Akosha MCP server startup | Populates `FitnessAnalyzer._component_endpoints` from `component_endpoint/{name}` keys |

### Gating and disable behavior

`publish_to_eventbridge` is gated three ways:

1. **`enabled`** parameter passed at registration time (legacy, captured once).
1. **`enabled_fn` callable** invoked on every tool invocation — the production wiring reads `AkoshaConfig().eventbridge.enabled` per call so operators can flip `AKOSHA_EVENTBRIDGE_ENABLED` without restarting the MCP server.
1. **Publisher presence** — when the module-level `_publisher` in `akosha/observability/eventbridge_publisher.py` is `None` (no Oneiric dispatcher wired), the tool returns `{"status": "no_publisher", "warning": ...}` instead of failing.

When disabled, every call returns `{"status": "disabled"}`. When no
publisher is wired but `enabled=true`, the tool warns but does not
error — operators must attach an Oneiric `EventBridge` instance via
`set_eventbridge_publisher()` to actually emit envelopes.

`run_fitness_analysis` returns `{"status": "no_data"}` when no traces
were collected in the cycle (no rows written to Dhara) and
`{"status": "error", "error": "..."}` when the analyzer wasn't
initialized.

### Profile gating

All non-health tools are gated by `AKOSHA_TOOL_PROFILE`
(`akosha/mcp/tools/profiles.py`):

- `MINIMAL`: `get_liveness`, `get_readiness`, `health_check_service`, `health_check_all`, `wait_for_dependency`, `wait_for_all_dependencies` (always loaded via `register_health_tools_akosha`).
- `STANDARD`: minimal + `register_akosha_tools` (embeddings, search, analytics, graph).
- `FULL`: standard + `register_session_buddy_tools`, `register_pycharm_tools`, `register_otel_query_tools`, `register_fitness_tools`, `register_eventbridge_tools`.

The `discover_tools(query=...)` meta-tool is always registered. It
returns `loaded_tools` vs `not_loaded_tools` for the active profile
plus a `hint` to set `AKOSHA_TOOL_PROFILE=full`.

### Phase 0 registration flow

On startup the lifespan in `akosha/mcp/server.py` writes
`component_endpoint/akosha` to Dhara so the other components'
`FitnessAnalyzer` instances can discover Akosha as a polling target.
This used to be implemented via a private-attribute poke
(`app._mcp_server.lifespan = ...`) that FastMCP 3.x silently dropped;
the lifespan now uses the public `lifespan=` constructor kwarg (see
Contract 5.1 below).

```mermaid
sequenceDiagram
    autonumber
    participant CLI as akosha mcp server (port 8682)
    participant LS as lifespan(server)
    participant EMB as EmbeddingService (mock-only)
    participant HS as HotStore (DuckDB or pgvector)
    participant KG as KnowledgeGraphBuilder
    participant DH as Dhara (port 8683)
    participant FA as FitnessAnalyzer

    CLI->>LS: app.startup
    LS->>LS: validate_auth_config()
    LS->>LS: setup_telemetry()
    LS->>EMB: await initialize() (graceful fallback to mock)
    LS->>KG: KnowledgeGraphBuilder()
    LS->>HS: create_hot_store() -> initialize()
    LS->>CLI: register_all_tools(...)
    CLI->>DH: PUT component_endpoint/akosha = http://localhost:8682/mcp
    Note over CLI,DH: 5 attempts exponential backoff, then 5-min heartbeat
    LS->>FA: start() (background task every 60s)
    LS->>DH: list_services / get component_endpoint/{name}
    DH-->>FA: registered component URLs

    Note over CLI,FA: Steady state: poll traces, write fitness to Dhara
    loop every 60s
        FA->>HS: query_local_traces(task_class)
        FA->>FA: compute FitnessSignal
        FA->>DH: PUT routing_fitness/{tc}/{selector}
    end
```

______________________________________________________________________

## 3. MCP Read Surface

The read surface is the hot path. Tools are grouped by the subsystem
they exercise.

### Embeddings

| Tool | What it reads | Use when |
|------|---------------|----------|
| `generate_embedding` | Calls `EmbeddingService.generate_embedding` (mock-only in-process; real embeddings must be routed through a configured MCP-side provider like Ollama or OpenAI) | Inline vectorization for an ad-hoc search or semantic classification |
| `generate_batch_embeddings` | Same, vectorized | Bulk-ingest pipelines, code-graph pre-embedding |

Both return `mode: "real" | "fallback"` so callers can degrade
gracefully when the model isn't loaded.

### Cross-system semantic search

| Tool | What it reads | Use when |
|------|---------------|----------|
| `search_all_systems` | Generates an embedding for the query, then (planned) searches `HotStore.conversations`; **current implementation returns a single mock result** (see `akosha/mcp/tools/akosha_tools.py:347-357`) | Default recall; semantic similarity ≥ `threshold` |
| `query_local_traces` | `HotStore.query_traces` with SQL `WHERE` filters on `system_id`, `timestamp`, `metadata.attributes.task_class` (HNSW index not used) | Bodai feedback loop, fitness analyzer, time-bounded audit |

### Code-graph and code search

| Tool | What it reads | Use when |
|------|---------------|----------|
| `list_ingested_code_graphs` | `HotStore.list_code_graphs` (rows in `code_graphs`) | Browse the indexer output |
| `get_code_graph_details` | `HotStore.get_code_graph` by `(repo_path, commit_hash)` | Drill into one snapshot |
| `find_similar_repositories` | All code graphs; `array_cosine_similarity` of node-type histograms | Cross-repo structural similarity |
| `get_cross_repo_function_usage` | All code graphs; scans `nodes` for `function_name` substring | "Who calls X?" across all indexed repos |
| `search_code_patterns` | PyCharm adapter **and** code-graph `nodes[].source` regex | Cross-cutting regex with IDE fallback |
| `get_code_problems` | `nodes[].problems[]` filtered by severity | IDE diagnostics rollup |
| `find_function_usage` | Code-graph `nodes[]` matching `function_name` substring; PyCharm `find_usages` as enrichment | "Where is function `parse_config` referenced?" |
| `analyze_imports` | Code-graph `nodes[].type == "import"` vs `edges[].type == "imports"` | Unused / circular / pattern analysis |

### Analytics (time-series + anomaly + correlation + changepoint)

| Tool | What it reads | Use when |
|------|---------------|----------|
| `get_system_metrics` | `TimeSeriesAnalytics.get_metric_names()` (in-process) | List active metric names |
| `analyze_trends` | Linear regression over `add_metric` data | Direction + strength of one metric over `time_window_days` |
| `detect_anomalies` | Z-score over the metric | Statistical outliers (configurable `threshold_std`) |
| `correlate_systems` | Pearson correlation across systems | "Which systems move together?" |
| `analyze_changepoints` | `ChangePointAnalytics` over Dhara time series (pytrendy) | Cliff / structural-break detection — registers only when `changepoint_analytics` is provided to `register_akosha_tools` |

### Knowledge graph

| Tool | What it reads | Use when |
|------|---------------|----------|
| `query_knowledge_graph` | `KnowledgeGraphBuilder.get_neighbors` | "What did `user:alice` work on?" |
| `find_path` | Bidirectional BFS over `edges` | Shortest path between two entities within `max_hops` |
| `get_graph_statistics` | `KnowledgeGraphBuilder.get_statistics` | Graph size + entity/edge type histograms |

### Fitness / routing

| Tool | What it reads | Use when |
|------|---------------|----------|
| `run_fitness_analysis` | All registered component endpoints via `BodaiComponentMCPClient.query_local_traces`; then `FitnessAnalyzer.run_fitness_analysis` | Manual cycle trigger, integration tests, dashboards |
| `get_fitness_analyzer_status` | `FitnessAnalyzer._running`, `_component_endpoints`, `_poll_interval` | Health check |

### Session-Buddy integration (push)

| Tool | What it writes | Use when |
|------|----------------|----------|
| `store_memory` | `HotStore.conversations` via `HotRecord` (validated 384-dim embedding) | Session-Buddy pushes a memory |
| `batch_store_memories` | Up to 1000 rows in one call (per-memory partial success) | Bulk sync from Session-Buddy |

### EventBridge

| Tool | What it does | Use when |
|------|--------------|----------|
| `publish_to_eventbridge` | Wraps `publish_pattern_detected` / `publish_anomaly_detected` / `publish_insight_generated` / `publish_aggregation_completed` via `_dispatch_topic` | Akosha wants to emit an analytics envelope to Bodai |

### Health

| Tool | What it reads | Use when |
|------|---------------|----------|
| `get_liveness` | process state | Kubernetes liveness probe |
| `get_readiness` | dependency health | Kubernetes readiness probe |
| `health_check_service` | Single `DependencyConfig` | Targeted dep check |
| `health_check_all` | All `DEFAULT_DEPENDENCIES` (session_buddy on 8678, mahavishnu on 8680) | Boot readiness |
| `wait_for_dependency` | Polls one dep until ready | Boot ordering |
| `wait_for_all_dependencies` | Polls all | Boot ordering |

______________________________________________________________________

## 4. Cross-Component Visibility

What other components see in Akosha, and the reverse direction —
Akosha is **read-mostly for everyone else** and writes fitness signals
out to Dhara. The data-flow direction is the opposite of Session-Buddy's.

| Consumer | Surface | What it reads from Akosha | What it writes to Akosha |
|----------|---------|---------------------------|--------------------------|
| **Session-Buddy** | `mcp__akosha__store_memory`, `mcp__akosha__batch_store_memories`, `mcp__akosha__publish_to_eventbridge` | (no read traffic in the canonical flow; SB reads its own reflections) | HotStore `conversations` rows pushed via the SB tools |
| **Dhara** | (passive — Akosha writes keys) | (no read traffic; Dhara owns versioned snapshots) | Holds `component_endpoint/akosha` (Akosha writes), `routing_fitness/{tc}/{selector}` (FitnessAnalyzer writes) |
| **Mahavishnu** | `mcp__akosha__search_all_systems`, `mcp__akosha__query_knowledge_graph`, `mcp__akosha__find_path`, `mcp__akosha__correlate_systems`, `mcp__akosha__analyze_changepoints` (when Dhara client wired) | HotStore `conversations` for semantic recall; FitnessAnalyzer reads `component_endpoint/*` from Dhara to discover poll targets | Sends `system_id` telemetry via the `publish_to_eventbridge` flow; otherwise read-only |
| **Crackerjack** | `mcp__akosha__search_code_patterns`, `find_function_usage`, `get_code_problems`, `analyze_imports`, `pycharm_health` | Code graphs in `HotStore.code_graphs` (indexer pushes them) | Indexer pushes rows via `store_code_graph` (called indirectly via `IngestionWorker`) |
| **Oneiric** | Settings + adapter discovery | (config only — does not query data) | (config only) — provides `PgvectorAdapter` for `PgvectorHotStore` and `EventBridge` for `eventbridge_publisher` |
| **Claude Code** | MCP client (`mcp__akosha__*`) | All read tools | All write tools (rare; usually via Session-Buddy) |

### What Akosha does NOT store

To avoid double-bookkeeping with neighbors, Akosha intentionally does
**not** store:

- **Raw reflection conversations** — those live in Session-Buddy; Akosha only holds aggregates or hot-tier copies (≤7 days).
- **Pool / worker runtime state** — Dhara and Mahavishnu own that.
- **Crackerjack fix attempts** — Crackerjack owns them; Akosha reads metrics indirectly via OTel traces.
- **LLM provider configuration / API keys** — Oneiric + env vars.
- **Workspace graphs as primary source** — Mahavishnu owns the indexer; Akosha is the secondary index for cross-repo recall.
- **Long-term KG state across restarts** — `KnowledgeGraphBuilder` is in-process; the canonical cross-system KG is replicated to Dhara / Session-Buddy.

______________________________________________________________________

## 5. Integration Contract

The contract between Akosha and its consumers is implicit in the
schema and the MCP surface, but four specific contracts caused real
bugs and should be made explicit.

### Contract 5.1 — Lifespan must be passed to the FastMCP constructor, not assigned to a private attribute

**Bug**: `akosha/mcp/server.py` (pre-fix) defined the lifespan closure
**after** the `FastMCP(...)` constructor and then assigned it via
`app._mcp_server.lifespan = lifespan` — a private-attribute poke that
FastMCP 3.x silently drops because the internal `Server` captures its
own lifespan reference at `__init__` time. With the lifespan
no-op'd, two failure modes surfaced:

1. **MCP routing 404**: Claude Code's transport auto-detector fell back to REST-style `/mcp/tools/call` routing, which returned 404 because only `/mcp` is mounted.
1. **Phase 0 registration never fired**: Akosha never wrote `component_endpoint/akosha` to Dhara, so `FitnessAnalyzer` could not discover Akosha as a polling target.

**Contract**: `create_app()` MUST pass the lifespan via the public
`lifespan=` kwarg to the `FastMCP(...)` constructor. Any code path that
relies on `_register_component_to_dhara` running at startup (i.e.
FitnessAnalyzer discovery) will fail silently if the lifespan is
attached post-construction.

**Regression test**: `tests/integration/test_mcp_integration.py::TestMCPIntegration::test_mcp_server_initialization`
asserts `app._mcp_server is not None` and the tool-registration test
(`test_tool_registration`) verifies that `register_akosha_tools` actually
attaches 9+ tools. Both fail if the lifespan is dropped.

### Contract 5.2 — `search_all_systems` must return real results, not a single mock

**Bug**: `akosha/mcp/tools/akosha_tools.py:347-357` (current code)
generates the query embedding but does **not** call
`HotStore.search_similar`. The tool returns a single canned result:

```python
results = [
    {
        "system_id": system_id or "system-1",
        "conversation_id": "conv-1",
        "content": f"Mock result for: {query}",
        "similarity": 0.85,
        "timestamp": datetime.now(UTC).isoformat(),
    }
]
```

This was acceptable while the hot tier was being wired, but it
**silently lies** to callers that believe `search_all_systems` is a
production recall path. Downstream code (e.g., Mahavishnu agent loop)
will see the same `conv-1` for every query and treat it as a hit.

**Contract**: `search_all_systems(query, limit, threshold, system_id)`
MUST delegate to `HotStore.search_similar(query_embedding, ...)` once
the hot tier is fully wired. Until then, the docstring MUST say
"mock" and the tool MUST return `mode: "mock"` (currently it returns
`mode: "real" | "fallback"` based on the embedding service, which
is a green-light to consumers that the recall is real).

**Regression test**: `tests/integration/test_full_integration.py::test_storage_layers`
asserts the real path (`hot_store.search_similar` returns the stored
record), but no test currently asserts that `search_all_systems` goes
through that path. Add
`test_search_all_systems_returns_real_results_after_store` that
inserts a unique-marker record into `HotStore`, calls `search_all_systems`,
and asserts the marker appears in `results`.

### Contract 5.3 — `publish_to_eventbridge` envelope must carry `source=akosha` and `version=1.0.0`

**Bug**: A pre-fix version of the publisher used
`source="akosha.observability"` instead of `"akosha"`, which broke
Mahavishnu's `bodai_subscriber` filter on `headers.source == "akosha"`
and silently dropped every Akosha event. The regression only surfaced
in dashboard integration tests, not unit tests.

**Contract**: Every envelope produced by `publish_pattern_detected`,
`publish_anomaly_detected`, `publish_insight_generated`,
`publish_aggregation_completed` MUST set:

- `headers["source"] = "akosha"` (the constant `SOURCE` in `eventbridge_publisher.py`)
- `headers["version"] = EVENT_VERSION` (currently `"1.0.0"`)
- `headers["event_id"]` (a fresh `uuid.uuid4()` per envelope)
- `headers["timestamp"]` (UTC ISO-8601)

**Regression test**: `tests/integration/test_oneiric_transport_roundtrip.py::test_publish_pattern_detected_round_trips_through_real_eventbridge`
asserts `envelope.headers.get("source") == "akosha"` against a real
Oneiric `EventBridge`. The parallel in-memory recorder test
`tests/integration/test_eventbridge_e2e.py::test_publish_pattern_detected_round_trips_through_transport`
asserts the same shape via the public API.

### Contract 5.4 — FitnessAnalyzer must register component endpoints before the first cycle

**Bug**: A pre-fix version of `_populate_component_endpoints_from_dhara`
returned early if `asyncio.get_running_loop()` raised
`RuntimeError` (no loop). The lifespan startup path runs before the
loop is established in some test harnesses, so the component list
ended up empty and `run_fitness_analysis` always returned
`{"status": "no_data"}` — masking real downstream failures.

**Contract**: `_populate_component_endpoints_from_dhara(analyzer)`
MUST fall back to `asyncio.run(...)` when no running loop is
detected (`akosha/mcp/tools/__init__.py:205-213`). The function MUST
also tolerate a missing Dhara (`registry.get` raises) without aborting
startup; a warning is logged and `discovered` stays 0.

**Regression test**: `tests/unit/test_mcp_tools_profiles.py` and
`tests/integration/test_mcp_integration.py::TestMCPIntegration::test_tool_registration`
both verify that `register_fitness_tools` is reached under FULL
profile and `init_fitness_analyzer` populates the analyzer. The
`test_fitness_analyzer_status_empty` (in `tests/unit/test_fitness_*`)
would catch a missing import path; if you add a regression for this
contract, store it as `tests/integration/test_fitness_analyzer_discovery.py::test_falls_back_when_no_loop`.

### General contract test policy

- **No mocks on the hot tier for round-trip tests**: tests that verify a write/read contract (e.g. `store_memory` → `search_all_systems`) MUST use a real `HotStore` in `tmp_path`, not a `MagicMock`. See `tests/integration/test_full_integration.py` for the canonical pattern.
- **Real Oneiric EventBridge for envelope tests**: `tests/integration/test_oneiric_transport_roundtrip.py` constructs a real `EventBridge` from `oneiric.core.lifecycle.LifecycleManager` and asserts the round-trip envelope; do not collapse to `AsyncMock`.
- **Profile tests must cover all three tiers**: `tests/unit/test_mcp_tools_profiles.py` covers MINIMAL, STANDARD, and FULL with a `DummyFastMCP` that records every registered tool. Add new groups to `REGISTRATION_TOOLS` in `akosha/mcp/tools/profiles.py` **and** update the count assertion in that test.

These four contracts are the minimum bar; new MCP wrappers should add
similar round-trip tests when introducing new write/read pairs.

______________________________________________________________________

## 6. Sample Queries

Realistic MCP invocations against Akosha from a Claude Code session.
These are the queries a developer would actually run during work —
not contrived examples.

### Q1 — Cross-system semantic recall

**Goal**: Find every conversation across all ingested systems that
mentions JWT authentication.

```python
mcp__akosha__search_all_systems(
    query="JWT authentication best practices",
    limit=10,
    threshold=0.7,
)
```

Returns up to 10 conversations across all `system_id`s whose
embedding cosine similarity is ≥ 0.7. Expected output shape:

```
{
  "query": "JWT authentication best practices",
  "total_results": 10,
  "results": [
    {"system_id": "...", "conversation_id": "...", "content": "...", "similarity": 0.92, "timestamp": "..."}
  ],
  "mode": "real" | "fallback"
}
```

**Caveat**: Until Contract 5.2 is fixed, this returns a single mock
result. Call `query_local_traces` for a real recall (see Q2).

### Q2 — Query OTel traces by system_id and time range

**Goal**: Find recent traces from `mahavishnu` within the last hour,
filtered to `code_generation` task class.

```python
mcp__akosha__query_local_traces(
    system_id="mahavishnu",
    start_time="2026-07-29T10:00:00Z",
    end_time="2026-07-29T11:00:00Z",
    task_class="code_generation",
    limit=100,
)
```

Pushes the filters into SQL `WHERE` (HNSW index not used). Returns a
list of trace records with `conversation_id`, `content`, `timestamp`,
`metadata`.

### Q3 — Embed a custom query for offline similarity

**Goal**: Vectorize a query to feed into an external ranker.

```python
mcp__akosha__generate_embedding(
    text="how to implement JWT authentication",
)
```

Returns `{"embedding": [..384 floats..], "mode": "real" | "fallback"}`.

### Q4 — Find function usage across all indexed repos

**Goal**: Where is `parse_config` referenced?

```python
mcp__akosha__find_function_usage(
    function_name="parse_config",
    language="python",
    limit=20,
)
```

Walks every `code_graphs` row, scans `nodes[]` for substring matches,
optionally enriches with PyCharm's `find_usages`.

### Q5 — Detect anomalies in error_rate

**Goal**: Find statistical outliers in error rate across all systems.

```python
mcp__akosha__detect_anomalies(
    metric_name="error_rate",
    time_window_days=7,
    threshold_std=3.0,
)
```

Returns `anomaly_count`, `total_points`, `anomaly_rate`, and up to 10
anomalies with `z_score` and `deviation`.

### Q6 — Analyze trends in conversation_count

**Goal**: Is the volume of conversations increasing on `system-1`?

```python
mcp__akosha__analyze_trends(
    metric_name="conversation_count",
    system_id="system-1",
    time_window_days=7,
)
```

Returns `trend_direction` (`increasing` | `decreasing` | `stable`),
`trend_strength` (R²), `percent_change`, `confidence`.

### Q7 — Cross-system correlation

**Goal**: Find systems whose `quality_score` moves together.

```python
mcp__akosha__correlate_systems(
    metric_name="quality_score",
    time_window_days=7,
)
```

Returns Pearson `correlation` for every significant system pair and a
`strength` label (`strong` if `|corr| > 0.7`).

### Q8 — Query the knowledge graph

**Goal**: What did `user:les` work on?

```python
mcp__akosha__query_knowledge_graph(
    entity_id="user:les",
    edge_type="worked_on",
    limit=50,
)
```

Returns `total_neighbors` + `neighbors[]` (each with `entity_id`,
`entity_type`, `edge_type`, `weight`, `properties`).

### Q9 — Find shortest path between entities

**Goal**: How is `user:les` connected to `system:mahavishnu`?

```python
mcp__akosha__find_path(
    source_id="user:les",
    target_id="system:mahavishnu",
    max_hops=3,
)
```

Bidirectional BFS over `edges`. Returns `{"path_found": True, "path": ["user:les", "project:mahavishnu", "system:mahavishnu"], "hops": 2}`.

### Q10 — Manually trigger a fitness analysis cycle

**Goal**: Force a routing-fitness poll and confirm signals were written.

```python
mcp__akosha__run_fitness_analysis()
```

Returns `{"status": "completed", "task_classes": [...], "selectors_per_class": {"code_generation": 3, ...}, "total_signals": 7}`.

### Q11 — Push a Session-Buddy memory into Akosha (write)

**Goal**: Session-Buddy wants to push one memory.

```python
mcp__akosha__store_memory(
    memory_id="mem_abc123",
    text="How to implement JWT authentication",
    embedding=[0.1, 0.2, ..., 0.9],  # 384 dims
    metadata={"source": "http://localhost:8678", "type": "session_memory"},
)
```

Returns `{"status": "stored", "memory_id": "mem_abc123", "stored_at": "...", "embedding_dim": 384, "source": "http://localhost:8678"}`.

### Q12 — Publish an analytics event (write)

**Goal**: Emit a `pattern.detected` event to the Bodai EventBridge.

```python
mcp__akosha__publish_to_eventbridge(
    topic="pattern.detected",
    payload={
        "pattern_id": "pat_001",
        "pattern_type": "anomaly_burst",
        "description": "Error rate spike on mahavishnu",
        "confidence": 0.95,
    },
)
```

Returns `{"status": "published"}` (or `{"status": "queued", "workflow_id": "..."}` if `async_callback=True`).

______________________________________________________________________

## 7. Diagrams

Three diagrams are persisted with this document. Two are embedded
above:

1. **Schema map** (Section 1) — `erDiagram` of all three storage tiers, the in-process fitness buffer, and the knowledge graph; arrows show the `AgingService` hot→warm migration and the FitnessAnalyzer→Dhara write.
1. **Phase 0 startup** (Section 2) — `sequenceDiagram` of the lifespan-initiated embedding/hot-store/tool registration, the Phase 0 Dhara write, and the steady-state 60-second fitness loop.

The third — **Cross-system data flow** — lives in the global Bodai
docs at `bodai/docs/memory/INDEX.md` because it spans all five
components, not just Akosha. The **Memory routing decision tree**
(global) will be authored in Stage 3 of the documentation plan.
Per-component diagrams (storage tiers, lifespan) live in each
component's `docs/architecture/MEMORY_ARCHITECTURE.md`.

______________________________________________________________________

## 8. Operational Notes

### Embedding generation latency

| Mode | Model | Latency per text (typical) | Throughput (batch) |
|------|-------|---------------------------|---------------------|
| `real` | (not used in this process; route via configured MCP-side provider) | n/a (mock is the only path) | n/a |
| `fallback` | deterministic hash-based mock | \<1 ms | n/a |

The model loads lazily in `EmbeddingService.initialize()` via
`loop.run_in_executor`, so the lifespan startup is not blocked. When
`sentence-transformers` is not installed (`ImportError`), the service
marks itself unavailable and continues with fallback — see
`akosha/processing/embeddings.py:81-91`.

### Fitness analyzer poll interval

`_DEFAULT_POLL_INTERVAL_SECONDS = 60` in
`akosha/processing/fitness_analyzer.py:31`. The interval is enforced by
`await asyncio.sleep(self._poll_interval)` in `_run_loop`. Component
endpoints are polled in parallel via `asyncio.gather` with a 15-second
per-component timeout. DLQ threshold: 3 consecutive write failures per
key (after that the signal is logged at ERROR and dropped). Buffer
size: 1000 entries (deque `maxlen`).

### Embedding model configuration

```yaml
# settings/akosha.yaml (no embedded model block — uses defaults)
storage:
  hot:
    backend: "duckdb-memory"        # or "pgvector" / "duckdb-ssd"
    pg_url: ""                       # only when backend=pgvector
```

```bash
# Force pgvector backend in production
export AKOSHA__STORAGE__HOT__BACKEND=pgvector
export AKOSHA__STORAGE__HOT__PG_URL=postgresql://akosha:***@localhost:5432/akosha

# Embedding model — currently hard-coded; override requires editing
# akosha/processing/embeddings.py:38 ("all-MiniLM-L6-v2" default).
```

### Backup and migration

- The warm store is the persistent state (`${AKOSHA_DATA_PATH}/warm/warm.db`); the hot store is `:memory:` by default and rebuilt on every restart.
- Schema migrations are run via `crackerjack run` (which includes `akosha migrate`); see `akosha/cli/commands/migrate.py` and `mahavishnu/migrations/` for cross-component migrations.
- `dara migrate --repo akosha` is the canonical entrypoint; it walks `akosha/storage/models.py` model definitions and applies the ordered DDL.

### Performance characteristics

| Operation | Typical latency | Hot path? |
|-----------|-----------------|-----------|
| `generate_embedding` (real) | 15-50 ms | Yes (preflight for recall) |
| `generate_embedding` (fallback) | \<1 ms | Yes |
| `search_all_systems` | (mock; ~ms) → will be 5-30 ms once Contract 5.2 is fixed | Yes |
| `query_local_traces` (SQL WHERE) | 10-50 ms | Yes (FitnessAnalyzer) |
| `find_similar_repositories` | 200-800 ms (fans out to N code graphs) | No |
| `get_cross_repo_function_usage` | 100-500 ms | No |
| `analyze_trends` / `detect_anomalies` / `correlate_systems` | 50-300 ms (in-process) | No |
| `analyze_changepoints` (pytrendy) | 1-5 s (queries Dhara + scipy) | No |
| `run_fitness_analysis` | 2-10 s (parallel HTTP polls + Dhara write) | Yes (60s cycle) |
| `store_memory` / `batch_store_memories` | 5-20 ms per record | Yes (HTTP push from SB) |

### Failure modes

- **Hot store `:memory:` lost on restart**: `IngestionWorker` rebuilds it on next poll; expect ~1-2 minute cold-start.
- **`pgvector` connection lost**: `PgvectorHotStore` raises `RuntimeError` on every operation; `create_hot_store()` factory does not fall back to DuckDB automatically — operators must restart the MCP server with `AKOSHA__STORAGE__HOT__BACKEND=duckdb-memory`.
- **Embedding model load fails**: `EmbeddingService.is_available()` returns False; downstream `mode` flag is `"fallback"`. `search_all_systems` will still return the mock result (see Contract 5.2).
- **FitnessAnalyzer Dhara write fails**: DLQ after 3 attempts; signal is dropped, ERROR log emitted. Circuit breaker (if provided) protects downstream.
- **Phase 0 Dhara write fails**: bounded exponential backoff (5 attempts, ~31s); heartbeat retries every 5 minutes. Akosha still starts up — `FitnessAnalyzer` simply cannot discover it.
- **Ingestion worker S3/R2 unavailable**: `IngestionWorker._discover_uploads` raises; logged, lifespan continues. Hot store is unreachable for new data until the adapter recovers.

______________________________________________________________________

## See Also

- `bodai/docs/memory/INDEX.md` (Stage 3) — Global memory routing decision tree and cross-system data flow.
- `akosha/storage/models.py` — Authoritative Pydantic schema definitions (`HotRecord`, `WarmRecord`, `ColdRecord`, `SystemMemoryUpload`).
- `akosha/storage/hot_store.py` — Hot-tier SQL schema and HNSW index configuration.
- `akosha/storage/aging.py` — Hot→warm migration; `AgingService.migrate_hot_to_warm(cutoff_days=7)`.
- `akosha/mcp/tools/akosha_tools.py` — MCP read surface implementations (embeddings, search, analytics, graph).
- `akosha/mcp/tools/session_buddy_tools.py` — `store_memory` / `batch_store_memories` push ingestion.
- `akosha/mcp/tools/eventbridge_tools.py` — `publish_to_eventbridge` MCP wrapper.
- `akosha/processing/fitness_analyzer.py` — FitnessAnalyzer (poll loop, buffer, Dhara writes).
- `akosha/processing/knowledge_graph.py` — In-process KG with bidirectional BFS path finding.
- `tests/integration/test_full_integration.py` — Canonical storage round-trip integration test.
- `tests/integration/test_oneiric_transport_roundtrip.py` — Contract 5.3 regression: real EventBridge round-trip.
- `tests/integration/test_eventbridge_e2e.py` — Contract 5.3 regression: in-memory recorder.
- `tests/integration/test_mcp_integration.py` — Contract 5.1 regression: lifespan wiring.
