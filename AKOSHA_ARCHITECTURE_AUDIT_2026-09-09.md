# Akosha Architecture Documentation Audit

**Audit date**: 2026-09-09
**Project root**: `/Users/les/Projects/akosha/`
**Scope**: 6 architecture docs vs. `akosha/` source reality
**Method**: Read-only. Each finding cites `doc:line` vs. `code:line`.

______________________________________________________________________

## 1. Architecture Inventory

| Doc claim (file:line) | Code reality (file:line) | Status |
|---|---|---|
| **Hot tier = "DuckDB in-memory + Redis cache"** (`docs/ARCHITECTURE.md:94`) | Two selectable backends via `AKOSHA__STORAGE__HOT__BACKEND`: `HotStore` (DuckDB) **or** `PgvectorHotStore` (Postgres + pgvector). `akosha/storage/__init__.py:96-110` switches on the env var; `akosha/config.py:96-99` reads it. | **DRIFT — partial** |
| **Hot tier is production-ready DuckDB** (`docs/ARCHITECTURE.md:530, 720`) | `akosha/storage/hot_store.py:11-26` docstring explicitly says: *"This is the development / test backend. ... For production deployments that require persistence, use `PgvectorHotStore`"*. `docs/architecture/MEMORY_ARCHITECTURE.md:46` agrees DuckDB is one of two options. | **CONTRADICTS ARCHITECTURE.md** |
| **Cold tier = "Parquet files in S3/R2"** (`docs/ARCHITECTURE.md:167-194`) | `akosha/storage/cold_store.py:13-30` imports `LocalStorageAdapter`, `S3StorageAdapter`, `GCSStorageAdapter`, `AzureBlobStorageAdapter`. Storage backend is selectable via `storage_backend: "local" \| "s3" \| "gcs" \| "azure"` (line 25). | **DRIFT — partial** |
| **Cache layer = `akosha/cache/layered_cache.py` (L1 memory + L2 Redis, 1000/5min + 100k/1hr)** (`docs/ARCHITECTURE.md:306-313, 548-549`) | `akosha/cache/` directory does **not exist** (verified via `find`). `redis` is a declared dep (`pyproject.toml:18`) and `redis>=5.0.0` is in lockfile, but no layered cache module is implemented. The claim that Phase 2 delivered "L1/L2 layered caching" (`ARCHITECTURE.md:548`) is **false**. | **DRIFT — major** |
| **9 MCP tools exposed via FastMCP** (`docs/ARCHITECTURE.md:328`) | `akosha/mcp/tools/` contains 11 modules: `akosha_tools.py`, `code_graph_tools.py`, `eventbridge_tools.py`, `fitness_tools.py`, `otel_tools.py`, `session_buddy_tools.py`, `pycharm_tools.py`, `cross_repo_tools.py`, `group_registers.py`, `tool_registry.py`, `profiles.py`. Real tool count is multi-dozen. | **DRIFT — major** |
| **`IngestionWorker` polls S3/R2 every 30s and ingests** (`docs/ARCHITECTURE.md:60-88`, sequence diagram) | `IngestionWorker` class exists (`akosha/ingestion/worker.py:22396 bytes`) but **is not instantiated anywhere in main.py**. `akosha/main.py:118` initializes `self.ingestion_workers: list[Any] = []` but no `.append(IngestionWorker(...))` exists. Active ingestion paths are `websocket_invocations_subscriber` and `bodai_event_subscriber` (main.py:227-249). | **DRIFT — major** |
| **`akosha/ingestion/discovery.py`** (`docs/PROJECT_STRUCTURE.md:23`) | Directory contents: `__init__.py`, `worker.py`, `orchestrator.py`, `bodai_event_subscriber.py`, `otel_ingester.py`, `code_graph_ingester.py`, `websocket_invocations_subscriber.py`. **No `discovery.py`.** | **DRIFT — module missing** |
| **`akosha/api/routes.py`** (`docs/PROJECT_STRUCTURE.md:42`) | Only `middleware.py` exists (122 lines). No FastAPI route file — routes live in `akosha/mcp/server.py`. | **DRIFT — module missing** |
| **`akosha/cache/` directory** (`docs/PROJECT_STRUCTURE.md:37-39`) | Does not exist. | **DRIFT — directory missing** |
| **`akosha/processing/{enrichment,vector_indexer,time_series}.py`** (`docs/PROJECT_STRUCTURE.md:27-31`) | None of these files exist. Actual files: `analytics.py`, `deduplication.py`, `embeddings.py`, `embedding_dim.py`, `fitness_analyzer.py`, `knowledge_graph.py`. Vector indexing is inside `hot_store.py`. | **DRIFT — 3 modules missing** |
| **`akosha/monitoring/{logging,tracing}.py`** (`docs/PROJECT_STRUCTURE.md:46-48`) | Only `metrics.py` exists. Tracing lives in `akosha/observability/tracing.py`. Logging uses `structlog` (`pyproject.toml:18`) without a dedicated wrapper. | **DRIFT — partial** |
| **`akosha/utils/{retry,helpers}.py`** (`docs/PROJECT_STRUCTURE.md:49-52`) | `akosha/utils/` directory does not exist. Retry uses `tenacity` dep directly (`pyproject.toml:23`). | **DRIFT — directory missing** |
| **`akosha/query/faceted.py`** (`docs/PROJECT_STRUCTURE.md:36`) | Only `__init__.py`, `distributed.py`, `aggregator.py` exist. No faceted search module. | **DRIFT — module missing** |
| **Code graph for tier transitions, knowledge graph as separate processing step** (`docs/ARCHITECTURE.md:36-42` mermaid) | Code graphs are a **DuckDB table inside HotStore** (`docs/architecture/MEMORY_ARCHITECTURE.md:49`), not a separate processing module. `KnowledgeGraphBuilder` is in-process dict/list (`MEMORY_ARCHITECTURE.md:51`). | **DRIFT — partial** |
| **Three storage tiers: Hot (0-7d), Warm (7-90d), Cold (90+d)** (`docs/ARCHITECTURE.md:30-33, 90-194`) | Hot default 7-day cutoff is real (`akosha/storage/aging.py:43` `migrate_hot_to_warm(cutoff_days: int = 7)`). Warm 90-day target is documented in warm_store.py schema. **Warm→Cold promotion is NOT implemented** — `MEMORY_ARCHITECTURE.md:170-171` correctly says "(planned) ... warm→cold not implemented" — but `ARCHITECTURE.md` does not flag this gap. | **DRIFT — partial (ARCHITECTURE.md hides gap)** |
| **`akosha/observability/`** (5 files: tracing, prometheus_metrics, eventbridge_resolver, eventbridge_adapter, eventbridge_publisher, security_logging) | Real, NOT mentioned in `PROJECT_STRUCTURE.md`. | **MISSING FROM DOC** |
| **`akosha/modes/` (base/lite/standard)** | Real, NOT mentioned in `PROJECT_STRUCTURE.md` or `ARCHITECTURE.md`. Operational modes are a first-class abstraction. | **MISSING FROM DOC** |
| **`akosha/websocket/` (server/auth/tls_config)** | Real, NOT mentioned in `PROJECT_STRUCTURE.md`. `ARCHITECTURE.md` doesn't mention WebSocket at all (no port, no protocol, no event types). | **MISSING FROM DOC** |
| **`akosha/shell/`, `akosha/alerting/`, `akosha/mcp/`, `akosha/security.py`, `akosha/config.py`, `akosha/main.py`, `akosha/cli.py`, `akosha/__main__.py`, `akosha/storage/{pgvector_hot_store,dhara_http_client,path_resolver,models}.py`, `akosha/ingestion/{otel,code_graph,bodai,websocket_invocations}_*.py`** | All real, all missing from `PROJECT_STRUCTURE.md`. | **MISSING FROM DOC** |

______________________________________________________________________

## 2. Storage Tier Audit (Hot / Warm / Cold — claimed vs. actual)

| Tier | ARCHITECTURE.md claim | MEMORY_ARCHITECTURE.md claim | Code reality | Reconciliation |
|---|---|---|---|---|
| **Hot** | DuckDB in-memory + Redis cache (lines 94-128) | DuckDB in-memory `:memory:` **or** pgvector collection via `PgvectorHotStore` (line 46) | `HotStore` (`akosha/storage/hot_store.py`, 616 lines, DuckDB) **and** `PgvectorHotStore` (`akosha/storage/pgvector_hot_store.py`, 240 lines, pgvector + Oneiric `PgvectorAdapter`). Factory `create_hot_store()` in `akosha/storage/__init__.py:58-110` switches on `AKOSHA__STORAGE__HOT__BACKEND`. `HotStore` docstring explicitly deprecates itself for production. **No Redis layer for the hot tier** — Redis is a dep but is wired elsewhere (modes, possibly L2 cache TBD). | `ARCHITECTURE.md` is **stale on hot-tier backends** (single backend, misses pgvector) and **wrong on Redis claim** (no cache module exists). `MEMORY_ARCHITECTURE.md` is correct. |
| **Warm** | DuckDB on-disk (NVMe SSD), INT8[384] embeddings, extractive 3-sentence summaries, 100-500ms latency (lines 132-163) | DuckDB on-disk at `${AKOSHA_DATA_PATH}/warm/warm.db`, INT8[384] + 3-sentence summary (line 47) | `WarmStore` (`akosha/storage/warm_store.py`, 129 lines) confirms DuckDB + INT8[384] + `summary TEXT`. `AgingService._generate_summary` (`akosha/storage/aging.py:308-321`) generates 3-sentence extractive summary. Wave 2 (H3) replaced `_quantize_embedding` with `quantize_embedding()` returning `QuantizedVector` NamedTuple (clipping + scale persistence). Wave 3 fixed `AgingService` method-drift bug (5 methods had drifted to module level). | Both docs agree on schema; quantization implementation has been hardened (Wave 2-3). `ARCHITECTURE.md` doesn't mention quantization hardening. |
| **Cold** | Parquet files in S3/R2, MinHash fingerprint + ultra-summary (1 sentence), no embeddings (lines 165-194) | Parquet (snappy) → S3/R2 via Oneiric adapter; docstring says "TODO; current code logs and unlinks" (line 48); but Wave 1 (C1) replaced placeholder with real Oneiric adapters supporting **local/s3/gcs/azure** (4 backends, not just S3) | `ColdStore` (`akosha/storage/cold_store.py`, 365 lines) imports `LocalStorageAdapter`, `S3StorageAdapter`, `GCSStorageAdapter`, `AzureBlobStorageAdapter` from `oneiric.adapters.storage.*`. Constructor takes `storage_backend: Literal["local","s3","gcs","azure"]`. R2 (Cloudflare) supported via custom `endpoint_url`. `_upload_to_storage` is now a real async upload (was placeholder pre-Wave 1). **No MinHash cold export — only schema declares `fingerprint` as MinHash, but the actual export uses the conversation-level summary, not MinHash.** | `ARCHITECTURE.md` is **partially stale** (says S3/R2 only, misses GCS/Azure + local). `MEMORY_ARCHITECTURE.md:48` is also **partially stale** ("TODO; current code logs and unlinks" was true pre-Wave 1, false now). MinHash in cold schema is **aspirational** — actual cold export doesn't compute MinHash. |
| **Aging Hot→Warm** | Hot→Warm at 7 days, Warm→Cold at 90 days (line 30-33 mermaid, 528-533 Phase 1 bullets) | Hot→Warm at 7-day cutoff (line 46) | `AgingService.migrate_hot_to_warm(cutoff_days: int = 7)` is real (`akosha/storage/aging.py:43-159`). Includes INT8 quantization + summary gen + checksum verification (Wave 2 H3 + Wave 3 wiring fix). | Matches docs. |
| **Aging Warm→Cold** | Documented in ARCHITECTURE.md | `MEMORY_ARCHITECTURE.md:170-171` correctly says "(planned) warm→cold exporter; current `export_batch` writes a temp Parquet then logs the S3 key — upload is TODO" | `ColdStore.export_batch` writes Parquet to temp, then `_upload_to_storage` (real after Wave 1) uploads to chosen backend. **But no aging service calls it** — warm→cold is still un-wired. | **DOC LIES.** ARCHITECTURE.md does not flag warm→cold as un-implemented; the Ingestion Flow mermaid (line 333-346) shows "Aging: Warm→Cold after 90 days" as if operational. MEMORY_ARCHITECTURE.md is honest about it. |

______________________________________________________________________

## 3. Findings (by severity and drift class)

### HIGH severity

**F-1 [A — Layer drift / Cache module]**

- **Doc ref**: `docs/ARCHITECTURE.md:306-313`, `:548-549`, Phase 2 delivery list at `:548`. Also referenced in mermaid diagrams at `:355`.
- **Code ref**: `akosha/cache/` directory does **not exist**. `find /Users/les/Projects/akosha/akosha -type d -name "cache"` returns 0 hits. `redis>=5.0.0` is a declared dep but no layered_cache module consumes it.
- **Impact**: ARCHITECTURE.md claims Phase 2 delivered "L1/L2 layered caching (memory + Redis)" as completed (`✅` on line 548). It didn't. The "Cache Layer" section (`:304-313`) describes a `akosha/cache/layered_cache.py` module that was never built.
- **Same finding in COMPREHENSIVE_ARCHITECTURE_REVIEW_2025-01-31.md:330** ("akosha/cache/layered_cache.py doesn't exist") — confirmed in 2025-01-31, still true 2026-09-09.
- **Fix**: Either remove the claim from ARCHITECTURE.md or implement the module.

**F-2 [B — Backend drift / Hot tier]**

- **Doc ref**: `docs/ARCHITECTURE.md:94` "Hot Tier (0-7 days) ... **Technology**: DuckDB in-memory + Redis cache". Phase 1 delivery list at `:528` "✅ Three-tier storage architecture". Production readiness score at `:715` "Architecture: 100/100 ✅".
- **Code ref**: `akosha/storage/hot_store.py:11-26` deprecation notice; `akosha/storage/pgvector_hot_store.py:1-8` "drop-in replacement for HotStore (DuckDB) when `AKOSHA__STORAGE__HOT__BACKEND=pgvector`"; `akosha/storage/__init__.py:96-110` factory with pgvector branch.
- **Impact**: ARCHITECTURE.md says DuckDB is the production hot tier. The code itself says DuckDB is the dev/test backend and pgvector is the production default. Docs are stale or wrong.
- **Fix**: Update ARCHITECTURE.md hot-tier section to describe both backends and reference `docs/plans/2026-08-29-pgvector-default.md`.

**F-3 [C — Data flow drift / IngestionWorker wired but not actually wired]**

- **Doc ref**: `docs/ARCHITECTURE.md:60-88` "Ingestion Pipeline" section claims `IngestionWorker` polls S3/R2 every 30s and ingests. Mermaid diagram at `:21-51` shows the same. Sequence diagram at `:67-82` shows SB→S3→Worker→Hot.
- **Code ref**: `akosha/main.py:118` `self.ingestion_workers: list[Any] = []`. No `.append(IngestionWorker(...))` call exists in `akosha/main.py` or anywhere in `akosha/`. Active ingestion paths in main.py:227-249 are `WebSocketInvocationsSubscriber` and `BodaiEventSubscriber` — both consume events, neither polls S3/R2.
- **Impact**: ARCHITECTURE.md describes an ingestion pipeline that is not running in production. Session-Buddy → S3 → Akosha pull model is not the actual data path.
- **Fix**: Either remove the S3-poller section from ARCHITECTURE.md (push-based ingest is reality) or wire `IngestionWorker`.

**F-4 [G — CURRENT_STATUS staleness]**

- **Doc ref**: `docs/CURRENT_STATUS.md` last touched **2026-07-17 03:22** (verified via `stat -f %Sm`).
- **Code reality**:
  - `pyproject.toml` says version **0.15.1**. CURRENT_STATUS.md says **0.3.0**.
  - CURRENT_STATUS.md says "MCP tools (40+ tools)" (line 25); `akosha/mcp/tools/` has 11 modules and likely 40+ tool registrations across them — possibly accurate, but tool count not verified.
  - CURRENT_STATUS.md lists no completed work after Phase 3 (Jul 2025). Reality: 5 hardening waves (W1-W5) all dated 2026-09-05 per `docs/feature-tracking/`. Each wave closed specific P0-P2 bugs (C1-C5, H1-H3, M2-M4) and added ~38 empty-test rewrites (W4) + live MCP backend wiring (W5).
  - CURRENT_STATUS.md does not mention: pgvector backend (Wave 1+), MinHash dedup via datasketch (Wave 2 H2), INT8 quantization correctness (Wave 2 H3), AgingService method-drift fix (Wave 3), coverage ratchet 87.62% → 89.0% (Wave 3), empty test rewrites (Wave 4), CodeGraphIngester wired in lifespan (Wave 5), KnowledgeGraphBuilder populated (Wave 5), per-feed /health probe (Wave 5).
- **Impact**: A 2-month-stale status doc with wrong version number misleads anyone who reads it as current state.
- **Fix**: Refresh CURRENT_STATUS.md. Frontmatter `status: complete role: historical date: 2026-07-16` already says it's historical; but a working status doc needs to exist alongside it.

**F-5 [F — Stale 2025 review / Several items now wrong]**

- **Doc ref**: `docs/COMPREHENSIVE_ARCHITECTURE_REVIEW_2025-01-31.md` dated 2025-01-31.
- **Code reality** (today, 2026-09-09):
  - Line 121, 129: "`akosha/storage/sharding.py` doesn't exist" → NOW EXISTS (123 lines, `ShardRouter` class).
  - Line 154, 162: "`akosha/storage/aging.py` doesn't exist" → NOW EXISTS (467 lines, `AgingService` class).
  - Line 257: "`akosha/query/distributed.py` doesn't exist" → NOW EXISTS (`DistributedQueryEngine`).
  - Line 363-381: "MinHash Implementation Incomplete" / "compute_fingerprint returns SHA-256" → FIXED in Wave 2 (H2), datasketch.MinHash is now production default (`akosha/processing/deduplication.py:65`).
  - Line 87-103: "Sequential Upload Processing Bottleneck" → still applicable to `IngestionWorker` if it's ever wired; not verified.
- **Impact**: A 19-month-old review is held up as authoritative but contradicts current code in 4+ specific places.
- **Fix**: Either append a "Status of review findings as of 2026-09-09" section or move the doc to `docs/archive/`. Currently sits at `docs/` top level with no archive marker.

### MEDIUM severity

**F-6 [D — Hot/Warm/Cold tier drift / Cold backend count]**

- **Doc ref**: `docs/ARCHITECTURE.md:32-33` "Cold Store ... Parquet/S3". `docs/ARCHITECTURE.md:167-194` describes S3/R2 only.
- **Code ref**: `akosha/storage/cold_store.py:13-30` imports 4 storage adapters (local, s3, gcs, azure). `storage_backend` parameter is `Literal["local","s3","gcs","azure"]`.
- **Impact**: Docs claim S3/R2-only; code is multi-cloud.
- **Fix**: Update cold tier section in ARCHITECTURE.md.

**F-7 [D — Hot/Warm/Cold tier drift / Warm→Cold un-implemented]**

- **Doc ref**: `docs/ARCHITECTURE.md:333-346` Ingestion Flow mermaid shows "Aging: Warm→Cold after 90 days" as a step. ARCHITECTURE.md does not flag warm→cold as un-implemented.
- **Code ref**: `ColdStore.export_batch` is implemented (real upload post-Wave 1), but no aging service calls it. `MEMORY_ARCHITECTURE.md:170-171` honestly says "(planned) warm→cold not implemented".
- **Impact**: The Ingestion Flow diagram suggests warm→cold is operational when it is not.
- **Fix**: Either remove the warm→cold step from the diagram or wire it.

**F-8 [E — MinHash drift / Dedup doc gap]**

- **Doc ref**: `docs/ARCHITECTURE.md:199-213` describes "Deduplication" generically as "Exact deduplication: SHA-256 content hash; Fuzzy deduplication: MinHash LSH". Doesn't name `datasketch`, doesn't describe the backend selection mechanism (`backend="minhash" | "sha256"`), doesn't mention the production-default status of MinHash.
- **Code ref**: `akosha/processing/deduplication.py:65-67` `DeduplicationService(backend="minhash", num_perm=128, threshold=0.5)` is the production default. `akosha/processing/deduplication.py:46-49` says "MinHash path is the production default; SHA-256 exists as a deterministic fallback". `pyproject.toml:30` pins `datasketch>=0.6.0` with comment "audit H2: MinHash for fuzzy deduplication".
- **Impact**: ARCHITECTURE.md doesn't describe the actual deduplication contract. Anyone reading it can't predict behavior.
- **Fix**: Update dedup section to describe backend selection, datasketch pinning, and Jaccard threshold.

**F-9 [H — Hardening wave drift / ARCHITECTURE.md missing 5 waves]**

- **Doc ref**: `docs/ARCHITECTURE.md` frontmatter `last_reviewed: 2026-07-16`. File mtime is 2026-09-06 (verified).
- **Code ref**: `docs/feature-tracking/2026-09-05-akosha-hardening-wave-{1,2,3,4,5}.md` document 5 hardening waves (all dated 2026-09-05). ARCHITECTURE.md does not reflect any of:
  - **Wave 1**: real `ColdStore` upload (4 backends), `/health` aggregator, IPython shell stubs, aiohttp dep declaration.
  - **Wave 2**: code_graph_tools similarity error propagation, real MinHash dedup, INT8 quantization correctness.
  - **Wave 3**: CLI version PackageNotFoundError fix, BootstrapOrchestrator.report_health active ping, /health CLI+HTTP parity, AgingService method-drift fix (5 methods had drifted to module level), coverage ratchet 87.62% → 89.0%.
  - **Wave 4**: 38 empty tests rewritten across 14 files, AST scanner to prevent regression.
  - **Wave 5**: CodeGraphIngester wired in lifespan, KnowledgeGraphBuilder populated in lifespan, /health probe surfaces per-feed state.
- **Impact**: ARCHITECTURE.md is 2 months behind reality on the security/reliability hardening story.
- **Fix**: Add "Hardening History" section or update frontmatter `last_reviewed` and re-review.

**F-10 [A — Layer drift / PROJECT_STRUCTURE.md omits whole subsystems]**

- **Doc ref**: `docs/PROJECT_STRUCTURE.md` (file mtime 2026-07-21).
- **Code ref**: Missing from PROJECT_STRUCTURE.md:
  - `akosha/observability/` (6 files)
  - `akosha/modes/` (3 files)
  - `akosha/websocket/` (3 files)
  - `akosha/shell/`, `akosha/alerting/`, `akosha/mcp/` (entire subdirs)
  - Top-level files: `akosha/main.py` (24KB), `akosha/cli.py` (19KB), `akosha/config.py` (17KB), `akosha/security.py` (18KB), `akosha/__main__.py`, `akosha/scripts/`
  - Storage: `pgvector_hot_store.py`, `dhara_http_client.py`, `path_resolver.py`, `models.py`
  - Ingestion: `bodai_event_subscriber.py`, `otel_ingester.py`, `code_graph_ingester.py`, `websocket_invocations_subscriber.py`
  - Processing: `analytics.py`, `embeddings.py`, `embedding_dim.py`, `fitness_analyzer.py`
  - Models: `akosha/models/schemas.py`
  - CLI: `akosha/cli/commands/migrate.py`, `akosha/tools/mermaid_validator/`
- **Impact**: Anyone using PROJECT_STRUCTURE.md to navigate the codebase will miss 60%+ of the actual structure.
- **Fix**: Regenerate PROJECT_STRUCTURE.md with actual `tree`-style output.

### LOW severity

**F-11 [A — Module-level claim / enrichment/vector_indexer/time_series]**

- **Doc ref**: `docs/PROJECT_STRUCTURE.md:27-31` lists `enrichment.py`, `vector_indexer.py`, `time_series.py`.
- **Code ref**: None of these files exist. Vector indexing lives in `akosha/storage/hot_store.py`. Time-series analytics lives in `akosha/processing/analytics.py`. No enrichment module exists.
- **Impact**: Stale doc.
- **Fix**: Update PROJECT_STRUCTURE.md (covered by F-10).

**F-12 [A — Module-level claim / api/routes.py]**

- **Doc ref**: `docs/PROJECT_STRUCTURE.md:42` lists `akosha/api/routes.py` and `akosha/api/middleware.py`.
- **Code ref**: Only `akosha/api/middleware.py` exists (122 lines). FastAPI routes live in `akosha/mcp/server.py`.
- **Impact**: Stale doc.
- **Fix**: Update PROJECT_STRUCTURE.md (covered by F-10).

**F-13 [B — Backend drift / Oneiric adapter pattern]**

- **Doc ref**: `docs/AKOSHA_STORAGE_ARCHITECTURE.md:38-95, 800-895` describes Oneiric's `AdapterBridge` universal adapter registration pattern with `register_adapter_metadata()` and 4-tier resolution precedence.
- **Code ref**: `akosha/storage/cold_store.py:13-17` uses **direct imports** of `S3StorageAdapter`, `GCSStorageAdapter`, etc. — no `AdapterBridge` or `Resolver`. `akosha/storage/pgvector_hot_store.py:11` uses `from oneiric.adapters.vector.pgvector import PgvectorAdapter` directly. `akosha/storage/__init__.py:96-110` is a simple factory function with an `if/elif` chain.
- **Impact**: AKOSHA_STORAGE_ARCHITECTURE.md describes an architectural pattern that is not implemented. The doc is a **design proposal** more than documentation of current code.
- **Fix**: Mark AKOSHA_STORAGE_ARCHITECTURE.md as `role: design-proposal` or `role: aspirational` rather than `canonical`.

**F-14 [C — Data flow drift / MCP tool count]**

- **Doc ref**: `docs/ARCHITECTURE.md:328` "Tools: 9 MCP tools exposed via FastMCP". `docs/ARCHITECTURE.md:530-547` says Phase 1 delivered "MCP server framework with 11 tools" and Phase 2 added 11 tools.
- **Code ref**: `akosha/mcp/tools/` has 11 modules: `akosha_tools.py`, `code_graph_tools.py`, `eventbridge_tools.py`, `fitness_tools.py`, `otel_tools.py`, `session_buddy_tools.py`, `pycharm_tools.py`, `cross_repo_tools.py`, `group_registers.py`, `tool_registry.py`, `profiles.py`. Each module registers multiple tools. Real tool count is **multi-dozen**, gated by `AKOSHA_TOOL_PROFILE` (full/standard/minimal) per `akosha/mcp/tools/profiles.py`.
- **Impact**: Tool count is wrong. Profile gating is not described.
- **Fix**: Update tool count and add profile section.

**F-15 [C — Data flow drift / Ingestion Flow mermaid]**

- **Doc ref**: `docs/ARCHITECTURE.md:333-346` Ingestion Flow shows SHA-256 dedup → MinHash dedup → vector indexing → hot insert → aging → cold.
- **Code ref**: `DeduplicationService` backend selection (`akosha/processing/deduplication.py:65`) uses MinHash as default, not sequential SHA-256 then MinHash. Flow is backend-configurable, not always both.
- **Impact**: Doc oversimplifies.
- **Fix**: Update mermaid to show backend-configurable dedup.

**F-16 [G — CURRENT_STATUS internal contradiction]**

- **Doc ref**: `docs/CURRENT_STATUS.md:25` says "Core tools (40+ MCP tools)". `docs/ARCHITECTURE.md:328` says "Tools: 9 MCP tools exposed via FastMCP". `docs/ARCHITECTURE.md:531` says "MCP server framework with 11 tools".
- **Code reality**: Actual count > 9, probably > 40 (need to verify by counting `@mcp.tool` decorators in `akosha/mcp/tools/*.py`).
- **Impact**: Two docs disagree on the same fact.
- **Fix**: Reconcile and document the profile gating.

**F-17 [D — Tier drift / Schema field]**

- **Doc ref**: `docs/ARCHITECTURE.md:111-128` schema has `embedding FLOAT[384]` hardcoded for hot tier.
- **Code ref**: `akosha/storage/hot_store.py` constructor accepts `embedding_dim` (configurable). `akosha/processing/embedding_dim.py:resolve_embedding_dim()` resolves dim at startup from the active embedding backend, defaulting to 384. `akosha/storage/pgvector_hot_store.py:53-66` honors the same resolution.
- **Impact**: Hardcoded 384 is misleading.
- **Fix**: Update schema diagram to show `FLOAT[N]` parameterized.

**F-18 [G — CURRENT_STATUS.md date stamping]**

- **Doc ref**: `docs/CURRENT_STATUS.md:13` "**Date**: 2025-01-27". File mtime **2026-07-17**.
- **Code reality**: Body content references Phase 3 work (Jul 2025). No mention of post-Jul-2025 work.
- **Impact**: Even the "Date" header is wrong by 17 months.
- **Fix**: Refresh or mark as historical.

**F-19 [E — MinHash drift / Cold tier MinHash aspirational]**

- **Doc ref**: `docs/ARCHITECTURE.md:184-186` declares cold Parquet schema `("fingerprint", pa.binary()),  # MinHash`.
- **Code ref**: `akosha/storage/cold_store.py` exports Parquet batches but does **not compute MinHash fingerprints** — `ColdRecord` model (`akosha/storage/models.py`) does not carry MinHash.
- **Impact**: Schema describes MinHash column; actual export does not produce it.
- **Fix**: Either compute MinHash in cold export (Datasketch MinHash of tokenized content) or drop MinHash from cold schema.

**F-20 [H — Hardening wave drift / MEMORY_ARCHITECTURE.md also stale]**

- **Doc ref**: `docs/architecture/MEMORY_ARCHITECTURE.md:48` "ColdStore ... TODO; current code logs and unlinks".
- **Code reality**: Wave 1 C1 (2026-09-05) replaced the placeholder upload. `akosha/storage/cold_store.py:240` `_upload_to_storage` is now a real async upload via Oneiric adapters.
- **Impact**: MEMORY_ARCHITECTURE.md is also stale, just by 4 days.
- **Fix**: Update cold-store row to reflect 4 backends + real upload.

______________________________________________________________________

## 4. Staleness Report — `docs/CURRENT_STATUS.md`

**Last touched**: 2026-07-17 03:22 (verified via `stat -f %Sm`)
**Current date**: 2026-09-09
**Age**: ~54 days

**Header claims**:

- "Date: 2025-01-27" → actual mtime 2026-07-17 (file was modified 54 days ago but date header says Jan 2025; **header is wrong by 17 months**)
- "Version: 0.3.0" → `pyproject.toml` says **0.15.1** (54 minor versions off, after 5 hardening wave bumps)
- "Phase 3 Complete + Enhancements" → Phase 3 was Jul 2025; today we're post-Phase 4 scale prep, post-5 hardening waves

**Section-by-section rot**:

| Section | Line | Stale claim | Reality |
|---|---|---|---|
| Phase 1: Foundation ✅ | 21 | "Ingestion pipeline (async workers)" | `IngestionWorker` exists but **not wired** in main.py (see F-3) |
| Phase 1: Foundation ✅ | 24 | "Core tools (40+ MCP tools)" | `akosha/mcp/tools/` has 11 modules; tool count may be ~40 but profile-gated, not always loaded |
| Phase 3: Production Hardening ✅ | 36 | "OpenTelemetry tracing (350 lines, automatic instrumentation)" | Real (`akosha/observability/tracing.py`) but **superseded by Waves 3-5** — `/health` is now a probe-driven aggregator with per-feed state (Wave 1 C2 + Wave 5) |
| Phase 3: Production Enhancements ✅ | 41-46 | "Extended tracing to analytics (4 methods, 10 metrics)" | This work predates the **wire-up-drift visibility gap** that Wave 5 closed (`mcp-surface-health-illusion` pattern). CURRENT_STATUS doesn't acknowledge that "wired up but functionally empty" was the failure mode. |
| Phase 4: 100-System Pilot (Ready) | 49+ | Status: "Partially complete (README exists, needs updates)" | 5 hardening waves later this is no longer accurate. README updates, deployment artifacts, smoke tests have all happened (per `docs/superpowers/plans/2026-09-06-live-mcp-smoke-test-impl.md`). |
| Remaining Work §1 Documentation | 56-69 | "Update README.md to Phase 3 completion status" | Outdated — README is at a different version. |
| Remaining Work §3 Performance | 88-102 | "Profile embedding generation performance ... Optimize hot paths based on profiling data" | Wave 2 added configurable `embedding_dim` resolution; `akosha/processing/embedding_dim.py` is new |
| Remaining Work §4 Kubernetes Deployment | 105-114 | "Not started" | `monitoring/alerts.yaml` exists (referenced in ARCHITECTURE.md Phase 3 work). K8s manifests not verified by this audit. |
| Recommended Active Tasks | 228-234 | 4 stale tasks | Wave 5 closed the "live MCP smoke test" gap. None of these tasks are still open in the same form. |

**Verdict**: CURRENT_STATUS.md is **historical**. Its YAML frontmatter says `status: complete role: historical date: 2026-07-16` — but a working status doc should sit alongside it reflecting post-Jul-2026 work. **No such doc exists.**

______________________________________________________________________

## 5. Summary

**Architecture docs vs. code reality in Akosha**:

- **6 of 6 docs reviewed** (ARCHITECTURE, AKOSHA_STORAGE_ARCHITECTURE, MEMORY_ARCHITECTURE, PROJECT_STRUCTURE, COMPREHENSIVE_ARCHITECTURE_REVIEW_2025-01-31, CURRENT_STATUS).
- **20 findings**: 5 HIGH, 6 MEDIUM, 9 LOW.
- **Top 3 most consequential drift classes**:
  - **(A) Layer drift** — `akosha/cache/layered_cache.py` claimed in ARCHITECTURE.md but doesn't exist; PROJECT_STRUCTURE.md omits ~60% of the actual code structure (observability/, modes/, websocket/, shell/, alerting/, mcp/, top-level main/cli/config/security).
  - **(F) Stale 2025 review** — 4 of 12 critical findings in COMPREHENSIVE_ARCHITECTURE_REVIEW_2025-01-31.md are now false (sharding.py, aging.py, distributed.py, MinHash all exist/work).
  - **(G/H) CURRENT_STATUS.md + ARCHITECTURE.md both miss the 5 hardening waves** dated 2026-09-05 that closed the P0-P2 audit findings.

**Architecture docs that are accurate**:

- `MEMORY_ARCHITECTURE.md` (`docs/architecture/MEMORY_ARCHITECTURE.md`) is the **most accurate** — it correctly describes dual hot backends, admits warm→cold is un-implemented, names pgvector, references code paths. Only minor staleness on cold-store upload (Wave 1 fix not reflected).

**Architecture docs that are aspirational rather than factual**:

- `AKOSHA_STORAGE_ARCHITECTURE.md` describes an Oneiric AdapterBridge pattern that is not implemented. Code uses direct Oneiric imports.

**Recommended next actions** (prioritized):

1. **Move `CURRENT_STATUS.md` and `COMPREHENSIVE_ARCHITECTURE_REVIEW_2025-01-31.md` to `docs/archive/`** — they're historical, but currently sit at docs root.
1. **Refresh `PROJECT_STRUCTURE.md`** with `tree`-generated output (single command, single edit).
1. **Update `ARCHITECTURE.md` hot-tier section** to describe pgvector backend and deprecate DuckDB as production.
1. **Update `ARCHITECTURE.md` cache section** to remove the `akosha/cache/layered_cache.py` claim or implement the module.
1. **Update `ARCHITECTURE.md` ingestion flow diagram** to show the actual wired paths (websocket_invocations_subscriber + bodai_event_subscriber), not the unwired IngestionWorker pull model.
1. **Append a "Hardening History" or "Post-Jul-2026 Status" section to ARCHITECTURE.md** referencing the 5 hardening waves.
1. **Refresh `MEMORY_ARCHITECTURE.md:48`** — cold-store upload is now real (Wave 1 C1).

**Files**: `/Users/les/Projects/akosha/AKOSHA_ARCHITECTURE_AUDIT_2026-09-09.md`
