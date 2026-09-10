---
role: audit
topic: docs-drift
last_reviewed: 2026-09-09
status: complete
auditor: parallel-fanout (5 agents)
related:
  - AKOSHA_ARCHITECTURE_AUDIT_2026-09-09.md
  - docs/ARCHITECTURE.md
  - README.md
  - QUICKSTART.md
  - akosha/CLAUDE.md
---

# Akosha Documentation Audit — 2026-09-09

Five parallel agents audited ~50 markdown files against the actual codebase
under `/Users/les/Projects/akosha/`. Akosha v0.15.1 is the Bodai ecosystem's
**Seer (Intelligence)** component on port 8682.

## Executive Summary

**~91 findings total** across 5 lenses:

- **~27 HIGH** — broken contracts, hallucinated APIs, fabricated infrastructure
- **~34 MED** — stale content, missing entries, port drift
- **~25 LOW** — typos, minor staleness, citation drift

**Three systemic patterns dominate**: (1) port drift (3002/8000 vs 8682 in 10+ sites),
(2) tool-name hallucination in README/profiles.py, (3) hardening waves W1–W5
(2026-09-05) never propagated to user-facing docs.

---

## 1. Cross-Cutting Findings (verified by 2+ agents — high confidence)

| Pattern | Caught by | Sites affected |
|---|---|---|
| **Port drift** (3002/8000 → 8682) | ecosystem F1/F2/F26, API F5/F6, runbooks H-2/M-4/L-4 | `settings/akosha.yaml:37`, `QUICKSTART.md:105,108`, `DEPLOYMENT_GUIDE.md:218,222`, `PHASE_3_PRODUCTION_HARDENING.md:505`, `akosha/CLAUDE.md:314,324,327`, `README.md:104,107,356`, `OPERATIONAL_MODES_QUICK_START.md` (AWS_S3_*) |
| **`akosha/cache/layered_cache.py` referenced but deleted** | ecosystem F6, architecture (Class A) | `docs/ARCHITECTURE.md:306, 548` |
| **Hardening waves W1–W5 not reflected in user docs** | ecosystem F14, architecture (Class G/H) | `README.md` still shows Phase 1–3 ✅ Phase 4 unchecked; `USER_GUIDE.md` no wave mentions; `CURRENT_STATUS.md` last touched 2026-07-17 |
| **ARCHITECTURE.md version stamp (0.3.0) vs pyproject (0.15.1)** | ecosystem F3/F15, architecture | `docs/ARCHITECTURE.md:7-9`; date `2025-02-08` vs frontmatter `2026-07-16` |
| **CLI tool count drift** (9/11/26/32/34) | ecosystem F7, MCP tools Finding 5 | `ARCHITECTURE.md:328,547`; `README.md:389-391`; `profiles.py:60-95` |

**Memory alignment** — these patterns match **3 documented ecosystem-wide audit memories**:

- `docs-audit-mcp-tool-hallucination.md` — "every Bodai README has hand-maintained tool counts that don't match" → confirmed (26 vs 32-34)
- `docs-audit-version-stamp-drift.md` — "5/6 Bodai components have inconsistent version strings" → confirmed (0.3.0 / 0.15.1)
- `docs-audit-cross-component-port-drift.md` — "each Bodai repo owns its port table" → confirmed (3002/8000/8682 internal drift)
- `crackerjack-cli-run-subcommand.md` — canonical `crackerjack run` not `crackerjack test` → confirmed in `akosha/CLAUDE.md:418,445`

---

## 2. Severity-Ranked Findings

### HIGH — Fix immediately

**Hallucinated APIs**

- README + `profiles.py` list **6 wrong tool names** (MCP F1/F2):
  - `ingest_session_memory` / `get_cross_system_summary` (real: `store_memory` / `batch_store_memories`)
  - `get_ide_diagnostics` / `search_code` / `get_symbol_info` / `find_usages`
    (real: `search_code_patterns` / `get_code_problems` / `find_function_usage` / `analyze_imports`)
- **4 doc files describe non-existent FastAPI routes** (API F1–F4):
  - `IMPLEMENTATION_GUIDE.md:621-710` (`akosha/api/routes.py` with `/api/v1/search`)
  - `WARM_TIER_STORAGE_STRATEGY.md:1125-1213` (`akosha/api/health.py`)
  - `PHASE_3_PRODUCTION_HARDENING.md:498-500` (`/api/v1/analytics/metrics`)
  - `akosha/CLAUDE.md:314,327` (`/api/v1/metrics`)
- **Code-graph tools bypass REGISTRATION_TOOLS** (MCP F3): 4 tools
  (`list_ingested_code_graphs`, `get_code_graph_details`,
  `find_similar_repositories`, `get_cross_repo_function_usage`) wired via
  `@mcp.tool()` directly on `registry.app`, so `discover_tools` cannot enumerate them

**Fabricated infrastructure (runbooks)**

- **`MILVUS_FAILURE.md` is entirely fictional** — `grep -rn "pymilvus|Milvus|MILVUS"`
  in `akosha/` and `pyproject.toml` returns 0 matches. References Milvus statefulset,
  milvusctl, fallback to DuckDB — none of it exists
- **3 runbooks end at "Detection" with no recovery**:
  `INGESTION_BACKLOG.md`, `HOT_STORE_FAILURE.md`, `MAHAVISHNU_DOWN.md`

**Stub-fabricated docs**

- **`docs/ADMIN_SHELL.md:30-136`** documents 5 commands (`aggregate`, `search`,
  `detect`, `graph`, `trends`) — all return `{"status":"stub", ...}` envelopes
  at `akosha/shell/adapter.py:115,146,177,208,241`
- **`docs/QUICK_REFERENCE.md`** describes Oneiric `AdapterMetadata` /
  `register_adapter_metadata` pattern that was never implemented; Phase 1–5
  checklists all marked done against nonexistent work

**Stale architecture claims (architecture audit)**

- **Hot tier production-readiness is fictional**: `ARCHITECTURE.md:94` claims
  "DuckDB in-memory + Redis cache"; `akosha/storage/hot_store.py:11-26`
  docstring explicitly deprecates DuckDB for production in favor of
  `PgvectorHotStore`. The 100/100 architecture score at `ARCHITECTURE.md:715`
  is built on a tier the code itself says isn't production-ready
- **4 of 12 critical findings in `COMPREHENSIVE_ARCHITECTURE_REVIEW_2025-01-31.md`**
  are now false (sharding.py, aging.py, distributed.py, MinHash all exist/work)

**Config drift (ecosystem + runbooks)**

- **`settings/akosha.yaml:37` `mcp_port: 3002`** contradicts
  `akosha/config.py:50-53` docstring stating canonical is 8682
- **`ARCHITECTURE.md:515-517` + `akosha/CLAUDE.md:218-220`** reference
  `config/akosha.yaml` / `akosha_storage.yaml` / `akosha_secrets.yaml` — none exist
- **14 env vars in `DEPLOYMENT_GUIDE.md:240-264`** (`AKOSHA_HOT_PATH`,
  `AKOSHA_MAX_CONCURRENT_INGESTS`, `AKOSHA_L1_CACHE_SIZE`, etc.) — none bound
  in `akosha/config.py`
- **`AWS_S3_BUCKET` / `AWS_S3_REGION`** referenced in
  `OPERATIONAL_MODES_QUICK_START.md:45-46` — never read

### MED — Schedule cleanup

- **`/health` response shape drift**: `IMPLEMENTATION_GUIDE.md:674-677` documents
  `{"status":"healthy"}`; real `/health` returns probe-driven aggregate with
  **503 on degraded** (`akosha/mcp/server.py:951-988`)
- **`/ready` route doesn't exist**: `ARCHITECTURE.md:479` claims it; readiness
  lives on `/health`
- **`/api/v1/...` prefix** referenced in 4+ docs but never registered
- **CLI tool counts in README**: claims 26 in FULL, actual 32–34
- **3 core akosha tools missing from `REGISTRATION_TOOLS`**: `find_path`,
  `get_graph_statistics`, `analyze_changepoints`
- **`discover_tools` meta-tool itself undocumented** (`akosha/mcp/tools/__init__.py:280`)
- **USER_GUIDE covers only 3 of 9 tool groups** (no PyCharm/OTel/Fitness/
  EventBridge/Cross-Repo/Session-Buddy/code-graph mentions)
- **`DEPLOYMENT_GUIDE.md:319-333` Prometheus metric names disagree** with both
  `PROMETHEUS_METICS.md` and `akosha/observability/prometheus_metrics.py`
- **`docs/PROMETHEUS_METICS.md` filename typo** (METRICS not METRICS)
- **Hardcoded version `'0.1.0'`** in `docs/ADMIN_SHELL.md:147` (real is 0.15.1)
- **`.env.example` referenced** in `akosha/CLAUDE.md:23,204` but doesn't exist
- **3 stub runbooks missing recovery content** (INGESTION_BACKLOG,
  HOT_STORE_FAILURE, MAHAVISHNU_DOWN)
- **Deployment name drift**: runbooks reference `akosha-api` and `akosha-mcp`
  deployments; real `kubernetes/*.yaml` has `akosha-ingestion`, `akosha-query`,
  `akosha-hot-store`, `akosha-warm-store`, `akosha-aging`
- **`k8s/` vs `kubernetes/`** both exist with overlapping files

### LOW — Tracked but not blocking

- `crackerjack test` → `crackerjack run` in `akosha/CLAUDE.md:418,445`
- `crickerjack` typo in `akosha/CLAUDE.md:448,494,500`
- `git clone https://github.com/yourusername/akosha.git` placeholder in
  `OPERATIONAL_MODES_QUICK_START.md:25,506`
- README test-count summary "32/32 passing" but `test_embeddings.py` shows
  `(10 passing, 4 skipped)` — skipped ≠ passing
- "diviner" vs "seer" terminology mismatch (`cli.py:499` vs README/CLAUDE.md)
- `README.md:394` cites "profiles.py:60-95" — actual is 63-100
- Hardcoded `--port 8000` in `README.md:356`
- `pytrendy>=0.2` dep — no docs cover it

---

## 3. Affected Files Heat Map

| Doc | Finding count | Drift class |
|---|---|---|
| `akosha/CLAUDE.md` | 8+ | port, env vars, fictional routes, CLI typos |
| `docs/ARCHITECTURE.md` | 7+ | version, config paths, cache module, tool count, env vars, date, hot tier |
| `README.md` | 6+ | port, tool count, hardening waves, version, CLI default |
| `docs/DEPLOYMENT_GUIDE.md` | 6+ | port, env vars, metric names, deployment names, date |
| `QUICKSTART.md` | 5+ | port, env vars |
| `docs/IMPLEMENTATION_GUIDE.md` | 4+ | non-existent `akosha/api/routes.py`, health shape, port |
| `akosha/mcp/tools/profiles.py` | 4 | REGISTRATION_TOOLS wrong names, description drift |
| `docs/PHASE_3_PRODUCTION_HARDENING.md` | 3+ | port, `/api/v1/analytics/metrics`, deployment names |
| `docs/runbooks/MILVUS_FAILURE.md` | 1 (entire file fabricated) | Milvus infrastructure |
| `docs/ADMIN_SHELL.md` | 5 (commands return stub) | shell adapter mismatch |
| `docs/QUICK_REFERENCE.md` | 1 (entire doc unimplemented) | Oneiric adapter pattern |
| `docs/WARM_TIER_STORAGE_STRATEGY.md` | 3+ | non-existent `akosha/api/health.py` |
| `docs/runbooks/{INGESTION_BACKLOG,HOT_STORE_FAILURE,MAHAVISHNU_DOWN}.md` | 3 (stub recovery) | missing content |
| `docs/CURRENT_STATUS.md` | 1 (54 days old, version 0.3.0) | rot |
| `settings/akosha.yaml` | 1 (`mcp_port: 3002`) | config drift |

---

## 4. Recommended Fix Clusters (sequenced)

### Cluster 1: Port standardization (one search/replace)
Replace `8000` / `3002` → `8682` across `settings/akosha.yaml:37`,
`QUICKSTART.md:105,108`, `DEPLOYMENT_GUIDE.md:218,222`,
`PHASE_3_PRODUCTION_HARDENING.md:505`, `akosha/CLAUDE.md:314,324,327`,
`README.md:104,107,356`. **Resolves ~10 findings across 3 audit lenses.**

### Cluster 2: Hallucinated tool names (mechanical rename)
Update `README.md:419-420,424-428` and `profiles.py:55,82-92` to actual
registered names. **Resolves 2 HIGH findings (MCP F1, F2).**

### Cluster 3: ARCHITECTURE.md full rewrite
One doc, 7+ drift points (version stamp, config paths, cache module,
tool count, env vars, date, hot tier). High leverage — single PR closes
many findings.

### Cluster 4: Delete or rewrite fabricated docs
- `docs/runbooks/MILVUS_FAILURE.md` → delete (no Milvus exists) or replace
  with DuckDB/pgvector failover runbook
- `docs/IMPLEMENTATION_GUIDE.md:621-710` → delete (file it describes doesn't exist)
- `docs/WARM_TIER_STORAGE_STRATEGY.md:1125-1213` → delete (file it describes doesn't exist)
- `docs/QUICK_REFERENCE.md` → rewrite or mark `superseded_by:`
- `docs/ADMIN_SHELL.md` → add "stub" banner to intelligence-commands section
- 3 stub runbooks → either author recovery steps or set `status: draft`

### Cluster 5: Hardening wave propagation
Add "Recent hardening (2026-09-05)" section to `README.md` and brief note in
`USER_GUIDE.md`. Update `CURRENT_STATUS.md` against v0.15.1 reality.

### Cluster 6: `/health` contract documentation
Document the real response shape (probe-driven aggregate + 503 on degraded) in
`docs/IMPLEMENTATION_GUIDE.md:674-677` and `docs/ARCHITECTURE.md:475-479`. This
is the contract that the 2026-09-05 hardening waves formalized.

---

## 5. Open Questions / False Premises

1. **`discover_tools` coverage** (MCP F3): Are the 4 code-graph tools
   *intentionally* invisible to the meta-tool, or is this a bug? Either add to
   `REGISTRATION_TOOLS` or document the intentional gap.
2. **`CURRENT_STATUS.md` ownership**: Should this be a working status doc or
   move to `docs/archive/`? Frontmatter says `role: historical` but no working
   status doc replaces it.
3. **Hardening wave lifecycle**: W1–W5 are all `built` (committed) but only W5
   reached `wired`. When do they reach `adopted`? Per CLAUDE.md wire-up-contract,
   "built but not wired" should be a transient state — yet all 5 are sitting
   there with no closure date.
4. **Three parallel Kubernetes layouts**: `k8s/` (24 files, Jan-Feb 2026) vs
   `kubernetes/` (15 files, Feb-Aug 2026) — should one be archived?
5. **MEMORY.md scope for `akosha/`**: Per memory
   `akosha-singleton-pattern-w0-dispatch.md`, the lifespan/registry split
   caused wire-up drift. The Wave 5 hardening closed part of this but
   `profiles.py` REGISTRATION_TOOLS still misses 5 tools. Re-verification
   recommended after Cluster 2 fix.

---

## 6. Total Findings by Audit

| Audit | Findings | HIGH | MED | LOW |
|---|---|---|---|---|
| Ecosystem integration | 29 | 7 | 14 | 8 |
| Architecture | 20 | 5 | 6 | 9 |
| MCP tool surface | 10 | 4 | 4 | 2 |
| API endpoints | 12 | 6 | 3 | 3 |
| Runbooks & ops | 20 | 9 | 7 | 4 |
| **Total** | **~91** | **~31** | **~34** | **~26** |

---

**Memory update recommendation**: after Cluster 1 lands, refresh
`docs-audit-cross-component-port-drift.md` to record the standardized 8682.

**Per CLAUDE.md "Process Discipline"**: this audit satisfies the wire-up audit
gate for Akosha docs. No new feature introduced; only documentation drift
repair.

## Audit Method

Each finding was verified by reading both sides — the doc claim and the code
reality. Every HIGH/MED finding cites both `doc:line` and `code:line` in the
per-lens reports. The audit was read-only — no files were modified during
data collection.

Audit agents dispatched in parallel at 2026-09-09 15:45 PDT; all five
completed within ~4 minutes (total subagent tokens ~22K).
