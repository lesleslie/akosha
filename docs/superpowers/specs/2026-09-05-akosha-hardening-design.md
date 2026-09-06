---
status: complete
role: implementation
date: 2026-09-05
last_reviewed: 2026-09-05
superseded_by: null
blocks_on: []
topic: convergence-control-plane
---

# Akosha Comprehensive Hardening — Design Spec

> **Why this spec exists.** An audit on 2026-09-05 surfaced 10 production bugs (3 critical, 3 high, 4 medium), a failing coverage gate (84.79% vs 87.62%), 9 modules with zero test coverage, 5 currently-broken tests, 382 empty no-assert tests, and an Akosha MCP backend whose intelligence layer is empty (0 entities, 0 traces, 0 metrics). The audit findings are catalogued in conversation memory under topic `akosha-audit-2026-09-05` and the live MCP server observations are in `get_graph_statistics` / `query_local_traces` / `get_system_metrics` calls returning zero rows.

## 1. Outcome

**User-observable change.** Akosha returns to a state where every advertised feature is wired and tested: `/health` accurately reports dependency health, ColdStore exports actually upload, deduplication actually dedupes, INT8 quantization is mathematically correct, the IPython shell commands raise instead of pretending success, and the live Akosha MCP backend has a populated knowledge graph / OTel trace ingest / code-pattern index.

**Success signal.** Five waves land sequentially to local main (no PRs, per `bodai-pre-1.0-merge-policy`):

1. `pytest --cov-fail-under=90.0` passes (up from 84.79% — past the 90% ratchet milestone in `.coverage-ratchet.json`).
2. The 5 currently-failing `tests/unit/test_version_sync.py::*` tests pass.
3. No production module is left with the audit-flagged bug signatures (grep for `TODO: Implement` returns 0 in `akosha/storage/`, `akosha/processing/`, `akosha/mcp/server.py`, `akosha/shell/adapter.py`).
4. Live Akosha MCP returns non-empty results from `query_knowledge_graph`, `query_local_traces`, and `search_code_patterns`.
5. `python -m akosha` and `python -m akosha.mcp` invoke without import errors and have ≥80% test coverage.

## 2. Goals

1. Fix all 3 critical bugs from the audit (silent data loss, lying /health, fake-success shell commands).
2. Fix all 3 high-severity bugs (similarity zero-on-error, dedup-not-dedup, broken INT8 quantization).
3. Fix all 4 medium bugs (missing `aiohttp` dep, error-swallowing CLI, self-attesting orchestrator, /health vs CLI probe disagreement).
4. Restore coverage to ≥90% — past the next ratchet milestone.
5. Rewrite 382 empty no-assert tests to include `pytest.raises` / `assert` statements, focusing on security, validation, tracing, and eventbridge error paths.
6. Wire live Akosha MCP backend data feeds (knowledge graph writer, OTel ingester, code indexer) so operators get non-empty results.
7. Surface "stub" status on any feature that intentionally isn't implemented yet, replacing the current silent-no-op pattern.
8. Each wave is independently shippable to local main with its own commit and feature-tracking entry.

## 3. Non-Goals

1. **Refactoring for style/aesthetics** (no renaming, no module reorganization beyond what bug fixes require). The audit's systemic patterns get addressed through targeted bug fixes; broader refactoring is its own future plan.
2. **Adding new features** beyond what's needed to make existing features work. No new MCP tools, no new storage backends beyond the S3/R2 adapter wiring, no new shell commands.
3. **Cross-repo changes** (no Mahavishnu / Dhara / Session-Buddy / Crackerjack modifications). The live MCP backend wiring stays within Akosha's existing surface (uses `mcp-common.health.DependencyConfig` patterns already imported elsewhere).
4. **Documentation overhauls.** Feature-tracking entries are added; the README and CLAUDE.md only change if a public-facing contract shifts (and there are none planned).
5. **Performance optimization.** Test execution time and Akosha runtime are not measured or improved beyond what test additions naturally change.
6. **Version bump beyond 0.14.3 → 0.15.0.** Final version bump is the last commit of Wave 5; intermediate waves stay at 0.14.x.

## 4. Current Findings

The audit was performed 2026-09-05 with two general-purpose subagents (one for source, one for coverage), `.coverage` SQLite parsing (`/Users/les/Projects/akosha/.coverage` dated 2026-09-04), live Akosha MCP introspection, and `pytest --cov=akosha` run (2,020 passed, 5 failed, 8 skipped).

### 4.1 Production bugs (10)

| Severity | ID | File:Line | Issue |
|---|---|---|---|
| CRITICAL | C1 | `akosha/storage/cold_store.py:183-205` | `_upload_to_storage` is a no-op that logs `"Would upload ..."` and deletes the temp file — silent data loss. |
| CRITICAL | C2 | `akosha/mcp/server.py:419-432` | `/health` and `/healthz` return hardcoded `{"status": "ok"}` regardless of dependency state. |
| CRITICAL | C3 | `akosha/shell/adapter.py:115-261` | All five IPython shell commands return fake-success dicts with `count: 0` and "implement X" messages. |
| HIGH | H1 | `akosha/mcp/tools/code_graph_tools.py:274-308` | `_compute_graph_similarity` swallows all exceptions and returns `0.0`. |
| HIGH | H2 | `akosha/processing/deduplication.py:62-84` | `DeduplicationService.compute_fingerprint` uses SHA-256 placeholder; `find_similar` always returns `[]`. |
| HIGH | H3 | `akosha/storage/aging.py:291-309` | `_quantize_embedding` produces wrong INT8 values — no clipping, no scale. |
| MEDIUM | M1 | `akosha/api/middleware.py:116` + `pyproject.toml:11-40` | `aiohttp` is imported but never declared in `[project.dependencies]`. |
| MEDIUM | M2 | `akosha/cli.py:474-480` | CLI `version` swallows all exceptions and prints "unknown". |
| MEDIUM | M3 | `akosha/ingestion/orchestrator.py:71-84` | `report_health` is self-attestation (no real ping). |
| MEDIUM | M4 | `akosha/cli.py:183-221` + `akosha/mcp/server.py:420-425` | CLI `_health_probe` and HTTP `/health` disagree on probe semantics. |

### 4.2 Test gaps

- **Total coverage: 84.79%** vs **threshold: 87.62%** → `--cov-fail-under` gate fails.
- **5 active test failures**: `tests/unit/test_version_sync.py::*` — version drift after recent version-bump commits (`14346ae`, `39e4f02`, `4b0c3db`, `1b19940`).
- **9 modules with zero test coverage** (sorted by LOC):
  - `akosha/processing/fitness_analyzer.py` (267 LOC)
  - `akosha/mcp/client.py` (219 LOC)
  - `akosha/cli/commands/migrate.py` (153 LOC)
  - `akosha/mcp/tools/fitness_tools.py` (121 LOC)
  - `akosha/processing/deduplication.py` (66 LOC)
  - `akosha/mcp/tools/tool_registry.py` (50 LOC)
  - `akosha/observability/eventbridge_adapter.py` (42 LOC)
  - `akosha/mcp/__main__.py` (16 LOC)
  - `akosha/__main__.py` (7 LOC)
- **382 empty no-assert tests** across the test suite, concentrated in `tests/test_security_coverage.py` (132 tests, 18 empty), `tests/test_prometheus_metrics_coverage.py` (74 tests, 38 empty), `tests/test_validation_coverage.py` (72 tests, 35 empty).
- **Branch coverage** is configured (`branch = true`) but unused — `coverage.json` reports `n_partial_branches` only, never `n_branches`.

### 4.3 Live MCP backend empty

| Live query | Current result | Expected |
|---|---|---|
| `get_graph_statistics` | 0 entities, 0 edges | ≥100 entities after indexing |
| `query_local_traces` | 0 traces | live trace ingest from local sources |
| `get_system_metrics` | 0 metrics | ≥1 tracked metric per ecosystem component |
| `search_code_patterns` | 0 results | searches over indexed `akosha/**/*.py` |
| PyCharm integration | `pycharm_available: false` | not part of this plan |

### 4.4 Recurring systemic pattern

Of the last 100 commits, ~50% are "feature was incomplete when merged" fixes — wiring drift, doc-vs-code drift, dead installs, linter churn. The fix for this is process discipline (each phase has Integration Contract blocks), not a refactor.

## 5. Implementation Phases

**Sequential waves, each commits to local main with a feature-tracking entry.** No PRs (per `bodai-pre-1.0-merge-policy`). Each phase produces one commit per deliverable, one wave-end commit, and one feature-tracking entry on completion.

### Phase 1 — P0 Critical Bugs + Broken Tests + Dependency Hygiene (Wave 1)

**Goal:** Eliminate silent data loss, lying health endpoints, and fake-success shell commands. Land the version-sync fix. Declare the missing `aiohttp` dependency.

**Tasks:**

1.1. **ColdStore real upload (C1).** Replace `akosha/storage/cold_store.py:183-205` `_upload_to_storage` with a real implementation using `oneiric.storage.S3StorageAdapter` (already available via `oneiric` dependency, already imported elsewhere). Add `__init__` config for `storage_backend` (`"s3" | "r2" | "memory"`, default `"memory"`), `bucket`, `endpoint_url`. Add `initialize()` that constructs the adapter from settings. Add `close()` that calls `adapter.close()`. The `"memory"` backend writes to a local directory (default `~/.akosha/cold-store/`) for development without cloud credentials.

1.2. **/health real probe (C2).** Replace `akosha/mcp/server.py:419-432` `/health` and `/healthz` handlers with calls into `mcp_common.health.register_health_tools`-style probes: check `HotStore` connection, `Dhara` HTTP reachability (if `dhara_url` configured), and `WebSocket` subscriber state. Return 200 + `{"status": "ok", "checks": {...}}` only when all probes pass; return 503 + structured error otherwise. Use the existing `AkoshaApplication._check_dependency_health` (introduce it; pull from `cli.py:_health_probe`).

1.3. **IPython shell stub surface (C3).** In `akosha/shell/adapter.py:115-261`, replace the five `TODO: Implement actual X logic` blocks. For each command, return `{"status": "stub", "command": "X", "message": "Not yet implemented; tracked in feature-tracking/..."}` until Wave 5 wires them. Operator sees "stub" status instead of fake "success".

1.4. **Version-sync test fix.** Run `pytest -x tests/unit/test_version_sync.py` to capture the exact asserted version. Update either the test constant or `akosha/__init__.py:__version__` (whichever is stale). Add a regression test that asserts `pyproject.toml` `[project] version` matches `akosha/__init__.py:__version__` so version drift fails the gate going forward.

1.5. **aiohttp dependency declaration (M1).** Add `"aiohttp>=3.12.14"` to `pyproject.toml:11-40` `[project.dependencies]`. The version pin matches the current patched line for CVE-2024-23896 / CVE-2024-23334. Run `uv lock` to refresh the lockfile. Add a guard test `tests/unit/test_dependencies.py::test_aiohttp_declared` that fails CI if the import reappears in a place not covered by the pin.

#### Phase 1 — Integration Contract

- **Triggered from**: `akosha.storage.ColdStore.export_batch()` (data ingestion workflow) → `_upload_to_storage()`; HTTP `GET /health` and `GET /healthz` from Kubernetes liveness probes; IPython admin shell session start → operator invokes `search()`/`detect()`/etc.; `pytest` discovery on `tests/unit/test_version_sync.py::*`; `uv sync` from any operator shell.
- **Returns to / updates**: Parquet files written to S3/R2/local backend via `oneiric.storage` adapter; `/health` body with structured `checks` dict; shell command responses with `status: "stub"`; test pass/fail markers; lockfile with pinned `aiohttp` floor.
- **Demonstrable by**:
  - `pytest tests/unit/test_cold_store.py::test_export_batch_writes_to_local` passes (covers memory backend).
  - `curl http://localhost:8682/health` returns `{"status": "degraded", "checks": {...}}` when Dhara is unreachable, `{"status": "ok", "checks": {...}}` when healthy.
  - `akosha-shell` → `search("foo")` returns `{"status": "stub", ...}` instead of `{"status": "success", "count": 0, ...}`.
  - `pytest tests/unit/test_version_sync.py` exits 0.
  - `grep -r "import aiohttp" akosha/` shows every import site has a `[project.dependencies]` floor.
- **Rollback signal**: any Phase 1 test fails; `/health` returns 200 with empty `checks` (means probe was bypassed); ColdStore emits `"Would upload"` log line (means no-op returned).
- **Observability added**: OTel span `akosha.cold_store.upload` with attributes `backend`, `bucket`, `object_key`, `bytes_written`; OTel span `akosha.health.check` with attribute `dependency` per probe; OTel log on each shell `stub` invocation.

### Phase 2 — P1 High Bugs + Zero-Coverage Module Tests (Wave 2)

**Goal:** Eliminate silent-error-swallowing, fake dedup, broken quantization. Land test files for the 9 modules with zero coverage.

**Tasks:**

2.1. **Similarity error propagation (H1).** Refactor `akosha/mcp/tools/code_graph_tools.py:274-308` `_compute_graph_similarity`. Split the empty-input guard (already at L292) from the try/except. Let unexpected errors propagate via `logger.exception(...)`. Add explicit tests for: empty types, single-element types, dimension mismatch, divide-by-zero (zero norm).

2.2. **Real deduplication (H2).** Replace `akosha/processing/deduplication.py:62-84`. Use `datasketch.MinHash` (add to `[project.dependencies]`). Add `MinHashService` with `compute_fingerprint`, `find_similar`, `update_index`. Provide a `HasherFallback` that uses SHA-256 + set membership for environments without `datasketch`. Configuration knob `akosha.dedup.backend: "minhash" | "sha256"`.

2.3. **INT8 quantization correctness (H3).** Rewrite `akosha/storage/aging.py:291-309` `_quantize_embedding` to compute `scale = 127 / max(abs(values))`, clip values to [-127, 127], and persist `scale` alongside the quantized vector. Update `EmbeddingQuantizer` schema in `akosha/storage/models.py` (or wherever the metadata struct lives) to include the scale factor. Add round-trip tests that verify quantization → dequantization stays within `1/127` of the original.

2.4. **Test files for 9 zero-coverage modules:**
  - `tests/unit/processing/test_fitness_analyzer.py` (≥10 tests covering the per-(task_class, selector) signal computation)
  - `tests/unit/mcp/test_client.py` (≥8 tests for the MCP client wrapper)
  - `tests/unit/cli/commands/test_migrate.py` (≥8 tests covering real migration paths, distinct from `test_migrate_cli.py` which only exercises CliRunner flags)
  - `tests/unit/mcp/tools/test_fitness_tools.py` (≥6 tests for the MCP-exposed sibling)
  - `tests/unit/processing/test_deduplication.py` (≥8 tests for `MinHashService`)
  - `tests/unit/mcp/tools/test_tool_registry.py` (≥6 tests for class internals)
  - `tests/unit/observability/test_eventbridge_adapter.py` (≥4 tests with real assertions, replacing the existing 1-test no-op)
  - `tests/unit/test_main_entrypoints.py` (smoke tests invoking `python -m akosha` and `python -m akosha.mcp` via subprocess; ≥4 tests)

#### Phase 2 — Integration Contract

- **Triggered from**: `mcp__akosha__cross_repo_capability_search` and similar MCP tool invocations that call `_compute_graph_similarity`; `akosha.processing.DeduplicationService.find_similar` from ingestion flows; `EmbeddingQuantizer.quantize` from `akosha/storage/aging.py` warm-store path; `pytest` test discovery.
- **Returns to / updates**: Similarity score that propagates real errors instead of silently zeroing; MinHash fingerprints that actually deduplicate; quantized INT8 vectors with persisted scale factor; new test files in `tests/unit/` raising coverage.
- **Demonstrable by**:
  - `pytest tests/unit/mcp/tools/test_code_graph_tools.py::test_similarity_propagates_runtime_errors` passes (raises instead of returning 0.0).
  - `pytest tests/unit/processing/test_deduplication.py::test_find_similar_returns_duplicates` passes (now returns non-empty on known-similar inputs).
  - `pytest tests/unit/storage/test_aging.py::test_quantize_roundtrip_within_eps` passes (round-trip error < 1/127).
  - `pytest --cov=akosha --cov-report=term-missing` shows the 9 modules now have ≥60% line coverage.
- **Rollback signal**: any similarity test returns 0.0 on a fixture designed to raise; `find_similar` returns `[]` on the corpus-of-duplicates fixture; quantization round-trip error exceeds `1/127`.
- **Observability added**: OTel counter `akosha.similarity.zero_on_error` (decremented each time a real error is now propagated instead of swallowed); OTel histogram `akosha.dedup.jaccard_estimate`; OTel histogram `akosha.quantize.scale_factor`.

### Phase 3 — P2 Medium Bugs + Coverage to 90% (Wave 3)

**Goal:** Surface real errors, fix health probe consistency, close the coverage gap to 90%.

**Tasks:**

3.1. **CLI version error specificity (M2).** In `akosha/cli.py:474-480`, catch only `importlib.metadata.PackageNotFoundError`. Let other exceptions surface (let Typer display the traceback).

3.2. **Orchestrator real health probe (M3).** In `akosha/ingestion/orchestrator.py:71-84`, replace `last_heartbeat` self-attestation with an actual ping to `mahavishnu_client`. Use `mcp_common.health.DependencyConfig` (already imported elsewhere in the codebase).

3.3. **Health probe consistency (M4).** Make `akosha/mcp/server.py:420-425` `app.custom_route("/health", ...)` delegate to the same `_health_probe()` that `akosha/cli.py:183-221` uses. Single source of truth for "is Akosha healthy."

3.4. **Coverage gap closure to 90%.** Currently 84.79%; threshold 87.62%; target 90%. Required: ~110 new tests weighted toward the bottom-15 modules identified in the audit (most importantly: `main.py` 66%, `bodai_event_subscriber.py` 68%, `mermaid_validator/renderer.py` 69%). Each new test must include at least one assertion (no empty-test anti-pattern). Update `pyproject.toml:99` `--cov-fail-under` to `90.0` once reached. The coverage ratchet in `.coverage-ratchet.json` is updated by hand to record the milestone.

#### Phase 3 — Integration Contract

- **Triggered from**: `akosha version` CLI invocation; `BootstrapOrchestrator.report_health()` from external monitoring; HTTP `GET /health`; `pytest --cov` from CI.
- **Returns to / updates**: Typer traceback on broken install; `report_health()` dict with `"last_actual_ping"` timestamp; `/health` body identical to CLI probe output; `pyproject.toml` threshold updated; `.coverage-ratchet.json` records milestone.
- **Demonstrable by**:
  - `akosha version` on a corrupted install shows the traceback.
  - `pytest tests/unit/ingestion/test_orchestrator.py::test_report_health_pings_mahavishnu` passes (mocked mahavishnu client).
  - `curl http://localhost:8682/health` returns the same JSON shape as `akosha health`.
  - `pytest --cov-fail-under=90.0` exits 0; `.coverage-ratchet.json` shows `next_milestone: 95`.
- **Rollback signal**: `akosha version` prints "unknown" (means catch-all returned); `report_health()` returns `last_heartbeat` without `last_actual_ping`; `/health` and CLI return different shapes.
- **Observability added**: OTel counter `akosha.cli.version.package_not_found`; OTel histogram `akosha.orchestrator.health_probe_latency`; structured log when `/health` and CLI probe disagree.

### Phase 4 — Test Quality Refactor (Wave 4)

**Goal:** Eliminate the 382 empty no-assert tests; harden the security, validation, tracing, and eventbridge surfaces with failure-path tests.

**Tasks:**

4.1. **Identify all empty tests.** Re-run the audit's static AST scan (the `test_quality.py` script referenced in the audit) to produce a per-file inventory of empty tests. Group by domain: security, validation, tracing, eventbridge, schemas.

4.2. **Rewrite empty tests in-place.** For each empty test, replace with one or more tests that:
  - Include at least one `assert` or `pytest.raises(...)` statement.
  - Probe a failure path (not the happy path).
  - Have a docstring naming the invariant under test.

  Focus domains (sorted by risk):
  - `tests/test_security_coverage.py` — 18 empty of 132 tests → add 18+ failure-path tests (auth-bypass, malformed-token, expired-token, role-mismatch, etc.).
  - `tests/test_prometheus_metrics_coverage.py` — 38 empty of 74 tests → add failure-path probes (missing labels, NaN values, malformed exposition format).
  - `tests/test_validation_coverage.py` — 35 empty of 72 tests → add schema-rejection, missing-required-field, type-mismatch probes.
  - `tests/unit/test_models_schemas.py` — 33 empty of 56 tests → add Pydantic v2 validator-failure probes.
  - `tests/unit/test_tracing.py` — 21 empty of 48 tests → add OTel exporter-failure, span-drop, batch-overflow probes.
  - Remaining ~237 empty tests across `tests/unit/test_validation.py`, `tests/unit/test_ingestion_validation.py`, `tests/unit/test_security_logging.py`, `tests/scripts/test_data_ingestion.py`, `tests/test_mermaid_renders.py`, `tests/unit/test_websocket_tls_config.py`, and others.

4.3. **Coverage-quality guard test.** Add `tests/unit/test_test_quality.py` that walks `tests/**/*.py`, parses each `def test_*` function, and asserts non-empty body + at least one assertion. CI fails if any empty test is added going forward.

#### Phase 4 — Integration Contract

- **Triggered from**: `pytest` discovery of every empty-test file; `crackerjack run` from CI; the new `tests/unit/test_test_quality.py` self-guard.
- **Returns to / updates**: 382 rewritten test functions across ~12 test files; a CI guard that prevents regression to empty-test anti-pattern; coverage quality measure (now meaningful, not just line-count).
- **Demonstrable by**:
  - `pytest tests/unit/test_test_quality.py::test_no_empty_tests` passes (every test function has ≥1 assertion).
  - `pytest tests/test_security_coverage.py` shows the 18 rewritten tests all pass and exercise failure paths.
  - Total `pytest --collect-only -q | grep "::" | wc -l` ≥ 1,387 (test count preserved or grown).
  - Coverage percentage is unchanged or higher (rewrites don't reduce line coverage).
- **Rollback signal**: `test_no_empty_tests` fails; any rewritten test loses coverage of the line it was supposed to probe.
- **Observability added**: OTel log on each CI guard run with `empty_tests_found` count (must be 0).

### Phase 5 — Live MCP Backend Wiring (Wave 5)

**Goal:** Akosha MCP returns non-empty results from `query_knowledge_graph`, `query_local_traces`, and `search_code_patterns`. Use `mcp-common.health.DependencyConfig` patterns; do not introduce new cross-repo deps.

**Tasks:**

5.1. **Investigate why the backend is empty.** Run the Akosha MCP server in foreground with debug logging and observe whether the data feed tasks (`fitness_analyzer` 60-second polling loop, `code_graph_ingester`, `knowledge_graph` writer) start up. Document findings: which feeds are configured but not running? Which are missing entirely? Output: a 1-page investigation report appended to this spec as `## Appendix A`.

5.2. **Knowledge graph writer wiring.** The Akosha MCP `knowledge_graph` is built but never populated. Investigate which ingestion path should populate it. Most likely candidates: `akosha/processing/knowledge_graph.py` (already 97% covered, suggesting the write side exists), `akosha/ingestion/code_graph_ingester.py`, `akosha/ingestion/bodai_event_subscriber.py`. Add a startup hook in `akosha/main.py:AkoshaApplication.start()` that triggers an initial population, and verify the background task is started.

5.3. **OTel trace ingester wiring.** `akosha/processing/otel_ingester.py` (or similar) needs to be started at boot. The audit identified `mcp__akosha__query_local_traces` returns 0 traces; the ingester likely exists but isn't running. Add boot hook.

5.4. **Code indexer wiring.** `mcp__akosha__search_code_patterns` returns 0 results. The indexer is built (per audit's "0 ERROR problems" finding) but never indexes. Add boot hook that indexes `akosha/**/*.py` on first start, with incremental updates on file-change events.

5.5. **Live verification.** Run Akosha MCP, wait 60 seconds for the fitness analyzer's polling cycle, then call `mcp__akosha__get_graph_statistics` and `mcp__akosha__query_local_traces` and `mcp__akosha__search_code_patterns` from a real client. Capture the before/after numbers in the wave-end commit message.

5.6. **Bump version to 0.15.0.** Update `pyproject.toml` version, `akosha/__init__.py:__version__`, and the ratchet milestone in `.coverage-ratchet.json`. Tag the commit as `v0.15.0` if the project's release policy tags at every merge to main.

#### Phase 5 — Integration Contract

- **Triggered from**: `AkoshaApplication.start()` in `akosha/main.py`; the 60-second `fitness_analyzer` polling loop; `mcp__akosha__*` tool invocations from any Bodai component.
- **Returns to / updates**: Akosha MCP `knowledge_graph` (entities + edges); `HotStore`-backed OTel trace index; `code_patterns` index of `akosha/**/*.py` regex matches; version `0.15.0` in `pyproject.toml` and `akosha/__init__.py`; `.coverage-ratchet.json` milestone entry.
- **Demonstrable by**:
  - `mcp__akosha__get_graph_statistics` returns ≥100 entities after a 60-second warmup.
  - `mcp__akosha__query_local_traces` returns ≥1 trace within 30 seconds of any Akosha tool invocation.
  - `mcp__akosha__search_code_patterns(pattern="TODO")` returns ≥1 match (the audit-found stubs, if not yet removed, plus the legitimate "Implement MinHash" comments left for posterity).
  - `python -c "import akosha; print(akosha.__version__)"` prints `0.15.0`.
- **Rollback signal**: any of the three live queries returns 0 after 60-second warmup; `AkoshaApplication.start()` raises; the ratchet JSON parses invalid.
- **Observability added**: OTel counter `akosha.kg.entities_indexed`; OTel counter `akosha.traces.ingested`; OTel counter `akosha.code_index.files_indexed`; structured log on each boot phase transition.

## 6. Required Code Changes

### Production code

- `akosha/storage/cold_store.py` — replace `_upload_to_storage`, add `storage_backend`, `initialize()`, `close()`.
- `akosha/mcp/server.py` — replace `/health` and `/healthz` handlers, add `_check_dependency_health`.
- `akosha/shell/adapter.py` — replace five stub commands with stub-status responses.
- `akosha/api/middleware.py` — confirm `aiohttp` import is intentional; update to use the pinned version.
- `akosha/cli.py` — narrow `except` to `PackageNotFoundError`; have `_health_probe` exported for `/health` reuse.
- `akosha/ingestion/orchestrator.py` — replace `last_heartbeat` self-attestation with `DependencyConfig` ping.
- `akosha/mcp/tools/code_graph_tools.py` — restructure `_compute_graph_similarity` error handling.
- `akosha/processing/deduplication.py` — replace SHA-256 placeholder with MinHash; add `HasherFallback`.
- `akosha/storage/aging.py` — rewrite `_quantize_embedding` with scale persistence.
- `akosha/main.py` — add boot hooks for knowledge-graph population, OTel ingester, code indexer.
- `pyproject.toml` — add `aiohttp>=3.12.14`, `datasketch>=0.6.0`; bump version to 0.15.0 at end of Wave 5; update `--cov-fail-under` to 90.0 at end of Wave 3.

### Test code (new)

- `tests/unit/test_dependencies.py::test_aiohttp_declared`
- `tests/unit/test_cold_store.py::test_export_batch_writes_to_local` (and ≥9 more)
- `tests/unit/test_main_entrypoints.py` (≥4 tests)
- `tests/unit/mcp/test_client.py` (≥8 tests)
- `tests/unit/cli/commands/test_migrate.py` (≥8 tests)
- `tests/unit/mcp/tools/test_fitness_tools.py` (≥6 tests)
- `tests/unit/processing/test_deduplication.py` (≥8 tests)
- `tests/unit/mcp/tools/test_tool_registry.py` (≥6 tests)
- `tests/unit/observability/test_eventbridge_adapter.py` (≥4 tests)
- `tests/unit/storage/test_aging.py::test_quantize_roundtrip_within_eps` (and ≥5 more)
- `tests/unit/test_test_quality.py::test_no_empty_tests`
- 382 in-place rewrites of existing empty tests across ~12 test files.

### Tracking and metadata

- `docs/feature-tracking/2026-09-05-akosha-hardening.md` (built/wired/adopted lifecycle).
- `docs/superpowers/plans/2026-09-05-akosha-hardening-impl.md` (created by writing-plans skill after this spec is approved).

## 7. Validation Matrix

| Tool / Command | Expected outcome | Evidence location |
|---|---|---|
| `pytest --cov-fail-under=90.0` | exit 0; coverage ≥90% | Wave 3 commit + CI log |
| `pytest tests/unit/test_version_sync.py` | exit 0 (5 tests pass) | Wave 1 commit |
| `pytest tests/unit/test_test_quality.py::test_no_empty_tests` | exit 0; reports 0 empty tests | Wave 4 commit |
| `curl http://localhost:8682/health` | `{"status": "ok", "checks": {...}}` when healthy, `503` + structured error otherwise | Wave 1 + Wave 3 commits |
| `akosha version` on broken install | traceback | Wave 3 commit |
| `mcp__akosha__get_graph_statistics` after 60s warmup | ≥100 entities | Wave 5 commit + 60-second wait |
| `mcp__akosha__query_local_traces` after first tool call | ≥1 trace | Wave 5 commit |
| `mcp__akosha__search_code_patterns(pattern="TODO")` | ≥1 match | Wave 5 commit |
| `grep -rn "TODO: Implement" akosha/storage/ akosha/processing/ akosha/mcp/server.py akosha/shell/adapter.py` | 0 lines | Wave 1 + Wave 2 commits |
| `pytest --collect-only -q \| grep "::" \| wc -l` | ≥1,387 tests | Wave 4 commit |

## 8. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| `oneiric.storage.S3StorageAdapter` interface differs from audit's assumption | Medium | Spike in Wave 1, Task 1.1 — read `oneiric/storage/__init__.py` before committing to interface. Fallback: implement the `_upload_to_storage` body inline against `boto3` if the adapter is missing. |
| `datasketch` import fails on Python 3.14 (project's target) | Low | Datasketch 0.6+ supports 3.12+ but not yet 3.14. Run `python -c "import datasketch"` early in Wave 2; pin to 3.13-compatible version if needed; the `HasherFallback` SHA-256 path is the escape hatch. |
| Live MCP backend requires infra setup we can't control | High | Wave 5 begins with an investigation sub-task (5.1). If infra is blocked, document the finding and convert the wave's "wire it" goal into "document the wiring steps and produce a runbook" — the spec remains valid. |
| 382-test rewrite causes regression | Medium | Rewrite in-place preserves the original function name and module; if `pytest --collect-only` shows test count drops, restore the original before re-attempting. The `test_no_empty_tests` guard catches new empties but not deletes. |
| Coverage threshold raise breaks the ratchet policy | Low | `.coverage-ratchet.json` is hand-maintained; bump `current_minimum` to 90.0 at the same commit that raises `--cov-fail-under`. |
| Pre-existing 5 version-sync test failures are unrelated to recent version bumps | Low | Read `tests/unit/test_version_sync.py` first; the asserted constant may be a feature, not a bug. If the right fix is to update the source constant, do that; if it's to update the test, do that. Either way, add a regression test pinning `pyproject.toml` and `akosha/__init__.py:__version__` in sync. |

## 9. Decision Rule

**This plan is "done enough" when:**

1. All 5 waves have landed on local main, each with its own commit and feature-tracking entry.
2. `pytest --cov-fail-under=90.0` passes.
3. No audit-flagged bug signature remains (the `grep` in §7 returns 0 lines).
4. The 3 live MCP queries return non-empty results after a 60-second warmup.
5. `tests/unit/test_test_quality.py::test_no_empty_tests` passes.
6. The 5 originally-failing version-sync tests pass.

**Scope-cut priority** (what to drop first if forced):

1. Drop `HasherFallback` from Wave 2 (datasketch is fine; fallback is gold-plating).
2. Drop the ratchet milestone ceremony in Wave 3 (the .coverage-ratchet.json update is bookkeeping).
3. Drop the `pytest --collect-only` count check in Wave 4 (it's a sanity check, not a hard gate).
4. **Never** drop: the 10 bug fixes, the coverage threshold raise to 90%, the empty-test rewrites for security/validation/tracing/eventbridge.

**Out-of-scope signals** (when to escalate to a separate plan):

- If Wave 5's investigation (5.1) reveals cross-repo infra changes are required (e.g. Mahavishnu needs to emit a new event type for Akosha to ingest), that's a separate plan.
- If Wave 4's rewrite count exceeds 600 (i.e. more empty tests found than the audit estimated), split into two waves.
- If any single bug fix in Wave 1/2/3 requires touching >5 files, escalate to a sub-plan.

## Appendix A — Live MCP Investigation Report (populated during Wave 5)

**Date:** 2026-09-05. **Investigator:** Wave 5 Task 5.1.

### A.1 — `AkoshaApplication` boot lifecycle (akosha/main.py)

The standalone `AkoshaApplication` class is **not on the live MCP request path**. It wires:

- `cache_client`, `cold_storage` (`_initialize_mode_components` line 275)
- `EventBridge` publisher (`_wire_eventbridge_publisher` line 451)
- `hot_store` + `embedding_service.initialize()` (lines 175-204)
- `DharaHttpClient` + `WebSocketInvocationsSubscriber` + optional `BodaiToolInvocationSubscriber` (lines 211-252)
- Signal handlers + `await shutdown_event.wait()` (lines 254-273)

But it does **not** wire the MCP-tool data feeds (`get_graph_statistics`, `query_local_traces`, `search_code_patterns`). The MCP server uses its own lifespan (`akosha/mcp/server.py::lifespan` lines 271-480) with its own fresh `hot_store`. The standalone `AkoshaApplication` is a separate entry point (used by the lite CLI tests, not by the MCP server).

**Gap A.1:** `ingestion_workers` list at `main.py:118` is always empty. `start()` never appends anything. The `stop()` cleanup loop (lines 504-509) is dead code.

### A.2 — Knowledge graph builder is empty (akosha/processing/knowledge_graph.py)

`KnowledgeGraphBuilder` exposes the right methods (`extract_entities` line 89, `extract_relationships` line 152, `add_to_graph` line 209, `get_statistics` line 519) and an in-memory store (`entities`, `edges` at lines 85-86). But:

- The MCP lifespan (`akosha/mcp/server.py`) does **not** instantiate `KnowledgeGraphBuilder` itself. It is constructed inside `akosha/mcp/tools/group_registers.py:67` per tool-group registration call.
- `group_registers.py:67` creates a fresh empty `KnowledgeGraphBuilder()` and passes it to `register_akosha_tools`. No code path ever calls `extract_entities` → `extract_relationships` → `add_to_graph` on it.
- Therefore `get_graph_statistics` (consumes at `akosha_tools.py:1186`) returns `{total_entities: 0, total_edges: 0, entity_types: {}, edge_types: {}}` forever.

**Gap A.2:** No population driver. There is no `populate_from_indexed_sources`, no `periodic_refresh`, no background coroutine. The graph is read-only at runtime.

### A.3 — Code graph ingester is a complete orphan (akosha/ingestion/code_graph_ingester.py)

`CodeGraphIngester` (lines 23-269) has a fully-working `start()` / `_polling_loop()` / `_discover_code_graphs()` / `_ingest_code_graph()` chain that POSTs to `session-buddy` MCP and writes to `hot_store.store_code_graph()`. **Nothing in the codebase instantiates or starts it.** Grep across `akosha/` finds zero call sites besides the class definition and the re-export in `akosha/ingestion/__init__.py:7`.

Consequence:

- `hot_store.list_code_graphs()` returns `[]` always → `list_ingested_code_graphs`, `find_similar_repositories`, `get_cross_repo_function_usage` (consumers in `akosha/mcp/tools/code_graph_tools.py`) all return empty.
- `search_code_patterns` (registered in `akosha/mcp/tools/pycharm_tools.py:341`) routes to a PyCharm HTTP endpoint, **not** to ingested code graphs. No equivalent search tool over the ingested corpus exists.

**Gap A.3:** `CodeGraphIngester` is fully-built but never started. This is the single largest wire-up gap.

### A.4 — No OTel trace ingester exists (akosha/ingestion/otel_ingester.py)

**The file does not exist.** The `akosha/ingestion/` directory contains only `bodai_event_subscriber.py`, `code_graph_ingester.py`, `orchestrator.py`, `websocket_invocations_subscriber.py`, `worker.py`, `__init__.py`. There is no OTel collector/ingester pipeline.

Trace data only enters the system through:

1. `BodaiToolInvocationSubscriber` (`main.py:223-249`) — Redis XREADGROUP, opt-in via `settings/akosha.yaml::bodai_tool_invocation_subscriber.enabled`. Requires Mahavishnu to publish.
2. Callers writing directly via `hot_store.query_traces` consumers (read-only).

The OTel export path (`setup_telemetry` at `server.py:322`) only emits Akosha's *own* spans — it does not pull other systems' traces. `query_local_traces` is bound entirely to whatever the Redis subscriber happens to receive.

**Gap A.4:** No inbound OTel trace pipeline. Building one from scratch is out of scope for Wave 5 (needs an OTel collector HTTP source contract). Marked as followup.

### A.5 — /health probe misses feed counts (akosha/mcp/server.py:416-452)

The default health probe checks only:

- `hot_store.ping()` (or its absence)
- `embedding_service.is_available()`
- `cold_storage` presence

It does **not** check `knowledge_graph.entities_count`, `hot_store.list_code_graphs().length`, `hot_store.query_traces().length`, `cycles_total`, `errors_total`, or `last_updated_timestamp`. Per `mcp-backend-wiring-discipline.md`, /health must surface per-feed state and return 503 on degraded. Currently a system can register 30 tools, return 200 from /health, and have all three feeds report 0 — exactly the failure mode the audit caught.

**Gap A.5:** /health is not a sufficient wire-up-drift detector.

### A.6 — Tool profile gate hides the empty state further

`akosha/mcp/tools/profiles.py:63-100` shows that `query_local_traces` and `search_code_patterns` are only registered under `FULL_REGISTRATIONS` (the `full` profile). With `AKOSHA_TOOL_PROFILE=standard` (default), these tools are not even registered. So an operator looking at the registered-tool list cannot see the missing feeds unless they explicitly set the profile to `full`.

**Gap A.6:** Default profile hides the empty feeds from the tool surface entirely. This compounds A.1-A.5: an operator on default profile sees a green /health and 19 tools, no empty-feed signals.

### A.7 — Three biggest wire-up gaps (priority order)

1. **`CodeGraphIngester` is fully built but never started.** Single largest gap. Wire it inside the MCP server lifespan (not `main.py` — see A.1). Pass it the lifespan's `hot_store`, start it after hot_store init, store a cancel handle on the lifespan state, await-cancel on shutdown. Tasks 5.2 and 5.4 of the plan cover this.

2. **Knowledge graph has no population driver.** Construct a single `KnowledgeGraphBuilder` in the lifespan, store it on the yielded state, pass it to `register_akosha_group` instead of constructing per-call. Add a periodic-refresh task that scans recent `hot_store.query_traces()` results, calls `extract_entities` / `extract_relationships` / `add_to_graph` for each. Tasks 5.2 of the plan covers this.

3. **`/health` probe must surface per-feed state.** Extend the default probe to check `kg_builder.entities_count`, `hot_store.list_code_graphs().length`, `hot_store.query_traces().length` (or the closest equivalent — `hot_store.query_traces` doesn't return all rows; use a `get_stats()` method if available). Return 503 if any feed is below a low-water mark for too long. This is what closes the `mcp-surface-health-illusion` failure mode.

---

*Spec drafted via brainstorming skill on 2026-09-05. Awaiting user review before transitioning to writing-plans skill.*
