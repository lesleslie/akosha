# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.19.0] - 2026-09-26

### Added

- Migrate launcher to mcp_common.server.launcher.launch()

### Fixed

- akosha: Clean up ruff-check blockers before publish
- HotStore.search_similar returns results for zero-vector queries

### Documentation

- akosha: Align launcher-discovery frontmatter role with Phase 4 peers
- akosha: Phase 4b launcher mode-dispatch discovery note (REQ-013)

### Internal

- akosha: Add GitHub FUNDING.yml sponsor declaration
- akosha: Drop unused deps + Python 3.16 ty deprecations
- akosha: Remove Dhara imports (decommissioning)
- deps: Bump mcp-common floor to >=0.28.0 for launcher migration (Phase 4)

## [0.18.0] - 2026-09-21

### Added

- akosha: Wire pgvector hot-store + Postgres 16/17/18 CI matrix

### Changed

- akosha: Re-export canonical AgentMetadata/SkillMetadata aliases (Phase 10 task 4)

### Fixed

- akosha: Align mock ecosystem tests with streamable-HTTP rewrite
- akosha: Keep B-6 body-integrity via strict AgentMetadata subclass (Phase 10 task 4)
- akosha: Make SignerFeedState.signer optional for feed-state tests
- akosha: Regenerate full golden fixture for renamed ecosystem_skills tool
- akosha: Restore zero-vector fallback in HotStore.search_similar
- akosha: Sync version stamps to pyproject 0.17.5
- tests: Align /health body status assertion with Phase 4 verdict enum
- tests: Sync \_DOC_TOOLS + expected count to Phase 3/4 31-tool inventory
- tests: Sync profile counts to Phase 3/4 31-tool registry

### Build

- deps: Drop dhara dep from runtime + creosote exclude (Phase 8 T12)

## [0.17.5] - 2026-09-16

### Changed

- akosha: HotStore subclasses Oneiric DuckdbHotStore

## [0.17.4] - 2026-09-15

### Added

- akosha: Emit health-aggregator metrics from /health probe

### Fixed

- akosha: Remove always-failing HNSW index attempt on DuckDB
- akosha: Wire cycles_total tracking in CodeGraphIngester polling loop

## [0.17.3] - 2026-09-15

### Added

- akosha: Unify /health aggregator to mcp-common contract

## [Unreleased]

### Changed

- akosha: Wire `mcp_common.health.metrics.update_health_metrics` into the `/health` probe body. The aggregator's `HealthSnapshot` is now published to the existing prometheus_client CollectorRegistry (the one the `/metrics` endpoint already exposes) so the four canonical health metrics fire live: `health_feed_status{repo, feed, status}`, `health_feed_errors_within_window{repo, feed}`, `mcp_common_health_halflife_seconds{repo}`, `mcp_common_health_aggregate_duration_ms{repo}` (histogram). These are exactly the names referenced by the PromQL alert rules at `mahavishnu/config/prometheus/health_aggregator_alerts.yml`. Phase 4 Observability + §11.4. Forward-compat: missing mcp_common.metrics module is silently no-op'd via `ImportError` catch.

### Fixed

- akosha: Remove the always-failing `CREATE INDEX ... USING HNSW (embedding)` attempt from `HotStore.initialize()`. DuckDB has no native HNSW support (its ANN path is the `vss` extension using ART); the prior code caught the resulting `Binder Error: Unknown index type: HNSW` on every poll cycle (5x per akosha cycle, 5x per kg_refresh, 5x per OTel ingester). The vestigial index was never functional — vector similarity queries use `array_cosine_similarity(...)` (brute-force scan, no index needed). Replace with an INFO log pointing operators at `INSTALL vss; LOAD vss;` for ANN acceleration. Also removes the dead `SET hnsw_ef_search = 100` call from `search_similar()` (Phase 5, tracked separately at `docs/followups/2026-09-14-akosha-hnsw-on-duckdb.md`).

- akosha: Wire `cycles_total` / `errors_total` / `last_poll_at` / `last_error_at` tracking in CodeGraphIngester's polling loop (plan §5 Phase 4 task 3 followup). Previously the ingester bumped nothing, so the aggregator's HNSW hardening always flagged `code_graphs_feed` as `degraded` with `feed_never_populated` (cycles_total == 0 + ingester_running). After this fix the first cycle flips the feed to `warming_up`; subsequent cycles with successful ingests land at `healthy`. Mirrors the existing OtelTraceIngester contract.

### Changed

- akosha: Replace `_default_health_probe` body with `mcp_common.health.aggregator.aggregate_feed_states` (plan §5 Phase 4 task 2). Each data feed (code_graphs, knowledge_graph, local_traces, skills_signer) becomes a HealthFeedState; the aggregator rolls them up into a worst-case `status` + per-feed verdicts. `/health` body now mirrors the aggregator's enum (`healthy` / `warming_up` / `degraded` / `failed`) instead of the legacy binary `ok` / `degraded`. HTTP code stays 200 for healthy + warming_up, 503 for degraded + failed.

### Fixed

- akosha: Wire `last_error_at` tracking in OtelTraceIngester and kg_refresh so the aggregator's time-bounded decay predicate can escalate DEGRADED for fresh errors (plan §5 Phase 4 task 3).
- akosha: Surfaces broken-before-first-success producers (cycles_total == 0 + ingester_running == True) as DEGRADED with `feed_never_populated` via HNSW hardening, replacing the previous warm-up masking.

### Added

- akosha: tests/integration/test_health_aggregator.py — end-to-end pin of the aggregator contract via the `/health` HTTP route (plan §5 Phase 4 task 1). Covers time-bounded decay, warming_up 200 contract, HNSW hardening, reason_codes population, and worst-case roll-up across mixed states.

## [0.17.2] - 2026-09-14

### Fixed

- akosha: HotStore.query_traces DuckDB OR-of-JSON-paths optimiser crash
- hot_store: Normalise tz-aware timestamps at insert and strip tz suffix from query strings

## [0.17.1] - 2026-09-14

### Added

- akosha: Migrate to CommonMCPClient + delete DharaServiceRegistryClient (Phase 3 REQ-004)
- akosha: Migrate to mcp-common CommonMCPClient SDK

### Fixed

- akosha: Append /mcp to DHARA_DEFAULT_URL defaults (Phase 2 REQ-005)
- Keep query_local_traces as Bodai-specific helper in akosha

### Documentation

- akosha: Document Phase 3 transport-unification changes in CHANGELOG

### Testing

- akosha: Rewrite MockSessionBuddyMCP for streamable-HTTP transport (Phase 3 REQ-009)

### Internal

- deps: Bump mcp-common floor to >=0.26.0,\<0.27.0 (Phase 2.5)

## [Unreleased]

### Changed

- **BREAKING (consumer-only, no published-API surface change): `_register_to_dhara_once` retry semantics.**
  4xx responses now reach the generic `except Exception` branch (categorized as `"retry"`); only 5xx responses trigger the explicit `"give_up"` branch.
  The previous `httpx.HTTPStatusError` catch (covering both 4xx and 5xx) was replaced by `mcp_common.clients.common_mcp_client.MCPClientHTTPError`, which is 5xx-only.
  Operational impact is rare — Dhara's `put` should not return 4xx in normal operation — but operators relying on 4xx-as-give-up behavior will see a categorization change. `MCPClientHTTPError` continues to be raised on 5xx with the original HTTP `status_code` attached.

- **BREAKING (consumer-only): `DharaServiceRegistryClient` class deleted.**
  Akosha no longer instantiates the legacy REST-style client against Dhara.
  The single production caller (mcp/tools/__init__.py:241) was migrated to `CommonMCPClient.call_tool("...")`.
  As a side effect, `_populate_async` no longer auto-wraps bare-string `get()` responses as `{"url": "..."}` — the new `mcp_common` transport returns strings as-is per the MCP spec.
  Operators with bare-string records stored in Dhara's KV (no documented use case but possible historical data) should migrate those records to dict form before upgrading, or update consumers to handle string returns.

- **Tests:** `tests/fixtures/mock_bodai_mcp.py:MockSessionBuddyMCP` rewritten to speak the streamable-HTTP transport that `CommonMCPClient` produces (POST `/mcp` JSON-RPC envelopes, GET `/mcp` SSE preamble, DELETE `/mcp` 204). Enables the REQ-009 cross-repo smoke test in `tests/integration/test_live_mcp_smoke.py` to run with the new transport.

### Known limitations (out of scope for this release)

- `tests/integration/test_live_mcp_smoke.py::test_ecosystem_runs_both_ingesters_concurrently` surfaces a `RuntimeError: Attempted to exit cancel scope in a different task` warning when two `CommonMCPClient` instances are torn down inside `asyncio.gather`. Root cause is anyio TaskGroup task affinity in `mcp.client.streamable_http.streamable_http_client` clashing with asyncio's task model. Tracked separately.

## [0.17.0] - 2026-09-13

### Added

- mcp: Phase 1 server-published skills (list_skills + get_skill)
- mcp: Phase 3 server-published agents (list_agents + get_agent + AgentMetadata schema)
- mcp: Phase 4 federation - list_ecosystem_skills aggregator with circuit breaker + cache

### Changed

- mcp: Align init_signer_feed_state to parameterless signature
- skills_signer: Reformat manifest missing-fields raise to 2-line

### Fixed

- mcp: Drop str_strip_whitespace from AgentMetadata config (B-6 hash-pin)
- mcp: Prefix all 27 user-facing tool names with akosha\_

### Documentation

- Add docs/assets/images/ + .scratch/ convention

## [0.16.0] - 2026-09-10

### Added

- skills_signer: Phase 1.5 cryptographic core + /health wiring

### Fixed

- mcp-server: Tolerate running-but-empty kg_refresh producer
- mcp-server: Tolerate running-but-empty OTel producer
- mcp-server: Tolerate running-but-empty OTel producer
- otel-ingester: Use OTLP/HTTP POST + structured logging

### Documentation

- 5-agent parallel audit pass + 91 drift fixes (2026-09-09)
- ops+plan: OTel→Tempo hand-off + akosha-otel-feed-recovery plan

### Testing

- mcp-phase0: Expect canonical /mcp-suffixed Dhara registration URL
- mcp-server: Add parity tests for local_traces_ok
- otel-ingester: Migrate coverage tests to POST contract

### Internal

- gitignore: Apply Bodai canonical snippet

## [Unreleased] - 2026-09-09

Documentation audit pass — 5-agent parallel audit (MCP surface, ecosystem,
API endpoints, architecture, runbooks) found 91 drift items (31 HIGH, 34
MED, 26 LOW). Reports at `AKOSHA_DOCS_AUDIT_2026-09-09.md` and
`AKOSHA_ARCHITECTURE_AUDIT_2026-09-09.md`.

### Removed

- akosha: Drop Kubernetes deployment support ecosystem-wide — remove both
  `k8s/` (legacy pre-split layout, 21 files) and `kubernetes/` (split-component
  layout, 13 files). Supersedes the 2026-09-09 audit recommendation to archive
  one layout. Future deployments use environment-based config (Oneiric
  layered settings + `MAHAVISHNU_*` env vars). `akosha/scripts/generate_secrets.py`
  marked `.. deprecated::`; ruff `pyproject.toml` `k8s/**` exclude removed.

### Fixed

- akosha: Standardize Akosha port to **8682** across all docs and config
  (was 8000/3002 in 9 sites) — `settings/akosha.yaml`, `QUICKSTART.md`,
  `DEPLOYMENT_GUIDE.md`, `PHASE_3_PRODUCTION_HARDENING.md`, `README.md`,
  `akosha/CLAUDE.md`, `prometheus.yml`, `tests/performance/test_ingestion_load.py`
- akosha: Update `akosha/mcp/tools/profiles.py:REGISTRATION_TOOLS` to actual
  tool names (`store_memory`, `batch_store_memories`, `search_code_patterns`,
  `get_code_problems`, `find_function_usage`, `analyze_imports`) — previously
  listed 6 hallucinated names in both README and profile registry
- akosha: Update test assertions in `test_mcp_tools_profiles.py` and
  `test_mcp_tool_inventory.py` to match corrected tool names
- akosha: Update `akosha/scripts/generate_secrets.py` defaults from `k8s/`
  paths to `kubernetes/` (canonical deployment layout)
- akosha: Update `MEMORY_ARCHITECTURE.md` references — removed `find_usages`
  (hallucinated PyCharm tool name; real name is `find_function_usage`)

### Documentation

- akosha: Rewrite `docs/ARCHITECTURE.md` — 9 drift points fixed (version
  stamp 0.3.0→0.15.1, hot tier production guidance, removed
  `akosha/cache/layered_cache.py` reference for deleted module, MCP tool
  count 9/11→32–34, real `/health` response shape with 503 contract, env
  var bindings, configuration paths, production readiness score 100/100→95/100)
- akosha: Add new "API Contract (Current)" section to
  `docs/IMPLEMENTATION_GUIDE.md` — canonical `/health` shape and `/api/v1`
  versioning decision (no prefix, intentional)
- akosha: Tombstone `docs/runbooks/MILVUS_FAILURE.md` — entire file references
  non-existent Milvus infrastructure (`grep` confirms 0 matches)
- akosha: Tombstone `docs/IMPLEMENTATION_GUIDE.md` Week 4 API Layer section
  (references non-existent `akosha/api/routes.py`)
- akosha: Tombstone `docs/WARM_TIER_STORAGE_STRATEGY.md` Health Check
  Endpoints section (references non-existent `akosha/api/health.py`)
- akosha: Add "STUB NOTICE" banner to `docs/ADMIN_SHELL.md` — 5 intelligence
  commands (`aggregate`, `search`, `detect`, `graph`, `trends`) return
  `{"status": "stub", ...}` envelopes
- akosha: Mark `docs/QUICK_REFERENCE.md` as superseded — describes Oneiric
  adapter pattern that was never implemented
- akosha: Add "Recent Hardening (2026-09-05)" section to `README.md` —
  5-wave hardening effort that closed audit findings; previously unmentioned
  in user-facing docs
- akosha: Add hardening note to `docs/USER_GUIDE.md`
- akosha: Add stale notice to `docs/CURRENT_STATUS.md` (claimed v0.3.0;
  actual is v0.15.1)
- akosha: Create `config/README.md` tombstone — `config/` directory is
  legacy (pre-Oneiric configs use obsolete ports 8000/3001); canonical is
  `settings/akosha.yaml`
- akosha: Add legacy banner to `k8s/README.md` — `k8s/` is the pre-split
  deployment layout (port 3002, akosha-mcp); canonical is `kubernetes/` (port
  8682, split components)

## [0.15.1] - 2026-09-06

### Fixed

- akosha: Sync 5 version stamps + README to 0.15.0 (T1-T5)

### Build

- deps+fix: FastMCP v4 migration (akosha Phase 2 — pin lift + client.py transport fix)

## [0.15.0] - 2026-09-06

### Added

- akosha: OTel span fetch, normalize, and ingest pipeline
- akosha: OtelTraceIngester skeleton with start/stop lifecycle
- akosha: Wire CodeGraphIngester + kg population in MCP lifespan (Wave 5)
- akosha: Wire OtelTraceIngester into MCP lifespan (Wave 6)
- otel: Add per-feed counters + fix /health wiring (C2/C3/C4/M13/M14)
- otel: E2e test with mock OTLP collector + service.name extraction
- Populate metadata.otel.span_id + improve embedding format

### Changed

- akosha: Drop dead register_otel_query_tools from legacy path (M16)
- akosha: Extract \_env_truthy() helper to deduplicate opt-out env reads

### Fixed

- akosha: BootstrapOrchestrator.report_health does active ping (M3)
- akosha: Honor /health probe contract + delete Wave 3 dead code
- akosha: Make discover_tools hint profile-aware
- akosha: Restore 5 orphan methods to AgingService + flip 3 tests locked in audit bugs
- akosha: Wire documented env vars + sync README tool count (M10/M11/M12)
- audit: Scanner now recognizes 'with pytest.raises' as an assertion pattern
- otel: Harden service.name extraction + unwrap AnyValue variants (M3/M4)
- otel: Surface shutdown exceptions; drop redundant CancelledError wrapper

### Documentation

- akosha: Live MCP smoke test design spec
- akosha: Live MCP smoke test implementation plan
- akosha: Mark Wave 5 followups closed; OTel + smoke test deferred
- akosha: OTel trace ingester design spec
- akosha: OTel trace ingester implementation plan
- akosha: Populate Wave-5 investigation report in spec appendix A
- akosha: Reconcile Scope Notes drift — OTel half has shipped
- akosha: Wave 3 hardening feature-tracking entry
- akosha: Wave 4 hardening feature-tracking entry + bump to 0.14.7
- akosha: Wave 5 feature-tracking + spec marked complete
- otel: Align docstrings with global-watermark reality (C1/M1/M2)

### Testing

- akosha: Cover cold_store cleanup + adapter error paths (+5 tests)
- akosha: Cover dhara_http_client lifecycle + parse tolerance (+12 tests)
- akosha: Cover embedding_dim exception branches (+5 tests)
- akosha: Cover fitness_analyzer trace fetch + Dhara write + DLQ paths (+10 tests)
- akosha: Dedupe mock + cancel \_poll_task + close CodeGraphIngester (M5/M6/M7)
- akosha: Drift test recognizes Wave 5 W0 group registration path
- akosha: Exact-path match in mock fixtures; update unit tests to match
- akosha: Guard against empty no-assert tests (Task 4.3)
- akosha: Live MCP smoke test against MockBodaiEcosystem
- akosha: MockBodaiEcosystem ephemeral-port fixture
- akosha: MockSessionBuddyMCP + MockOtelCollector ASGI apps
- akosha: Pin CLI + HTTP /health status parity (M4)
- akosha: Replace 59 vacuous test assertions with side-effect checks
- akosha: Replace 7 vacuous assert True with file-specific assertions
- akosha: Replace 8 remaining vacuous assert True with file-specific assertions
- akosha: Rewrite 38 empty prometheus_metrics tests with assertions (Task 4.2)
- akosha: Rewrite 38 empty tests with assertions across 14 files (Task 4.2)
- akosha: Tighten empty-test scanner + add regression coverage (M8/M9)
- akosha: Tool-level e2e for query_local_traces (C5)

### Internal

- akosha: AST scanner for empty no-assert tests (Task 4.1)
- akosha: Raise --cov-fail-under to 89.0 (Wave 3 ratchet)
- akosha: Sync 4 version stamps + 6 test constants to 0.14.7

## [0.14.0] - 2026-08-30

### Added

- akosha: Add cross_repo_capability_search MCP tool (Phase 1)
- akosha: Add embedding_dim resolver (Phase 1)
- akosha: Add WebSocketInvocationsSubscriber (Sub-plan B)
- akosha: BodaiToolInvocationSubscriber (Phase 2 push subscriber)
- akosha: HotStore dim validation + schema-size config (Phase 2)
- akosha: Initialise embedding service before HotStore (Phase 3)
- akosha: Orchestrator routes push vs poll (Phase 3 push subscriber)
- akosha: Subscriber comment + settings knob + feature-tracking (Phase 4)
- akosha: Thread backend+pg_url from settings into create_hot_store (Phase 1)
- akosha: Wire DharaHttpClient into subscriber (Followup 4)
- akosha: Wire HotStore into production bootstrap (Sub-plan A)
- akosha: Wire search_all_systems to hot_store (Sub-plan C)

### Fixed

- akosha: Silence ty/refurb warnings across six files

### Documentation

- akosha: Warn against file-backed DuckDB on serverless (Phase 3)
- feature-tracking: Add Akosha websocket-search adoption entry
- feature-tracking: Mark push-subscriber Followup 3 resolved

### Testing

- Pgvector e2e integration tests gated on AKOSHA_TEST_PGVECTOR_URL

### Internal

- akosha: Pin asyncpg + pgvector in vector-pg dependency group
- akosha: Silence ruff SIM105/ERA001/TC003 in three files

## [0.13.0] - 2026-08-29

### Added

- **BREAKING:** akosha: Rename BodaiCLIBase to OneiricCLIBase
- akosha: Gate 5 alpha shell commands behind config flag (Plan Task 3.2.1)

### Changed

- akosha: Bump oneiric floor to >=0.20 for OneiricCLIBase

### Fixed

- akosha: Add ipython direct dep (Plan Task 3.2.2)
- akosha: Resolve ty comprehensive-hook failures

## [Unreleased]

### Changed

- akosha: Rename `BodaiCLIBase` to `OneiricCLIBase` (hard cutover, no deprecation alias). Bumps `oneiric` floor to >=0.20.

## [0.12.2] - 2026-08-28

### Changed

- akosha: Use project_root= instead of path= in load_settings()

## [0.12.1] - 2026-08-28

### Added

- akosha: Adopt BodaiCLIBase + real doctor/health (Phase 3 Task 4.4)
- akosha: Bodai.apps entry-point (Phase 5.1)

### Fixed

- akosha: Anchor load_settings() path at package install location
- akosha: Anchor load_settings() project_root at package install location

### Documentation

- readme: Bump Python badge from 3.13+ to 3.14+

### Internal

- build: Standardize akosha on hatchling backend
- deps: Bump oneiric floor to >=0.19.1

## [0.12.0] - 2026-08-24

### Internal

- akosha: Uv python pin 3.14
- Bump requires-python to >=3.14
- claude-md: Add oneiric action-kit discovery breadcrumb

## [0.11.0] - 2026-08-22

### Added

- w3: Adopt workflow.audit + data.sanitize action kits

### Fixed

- akosha: Migrate embeddings.py to delegate to oneiric
- mcp: Wire services through register_akosha_group + initialize hot store
- w3: Close case-sensitive redaction gap for HTTP headers

## [0.10.0] - 2026-08-21

### Added

- akosha: Apply ToolProfile dispatch via mcp-common 0.18.0

### Fixed

- akosha: Mcp_server_lifespan + changepoint_analytics + graceful_shutdown — 22 tests
- akosha: Sync port defaults (2026-08-19)
- akosha: Sync version stamps (2026-08-19)
- akosha: Test_mcp_tool_inventory.py — 3 tests
- akosha: Wire fitness Dhara populate + profile subsets + W0 schema
- docs+code(akosha): fix MCP-tool-hallucination audit findings (2026-08-19)

### Documentation

- akosha: Fix documented-but-not-wired audit findings (2026-08-19)

### Testing

- akosha: Add doc-drift CI guard (2026-08-19)

## [0.9.5] - 2026-08-17

### Added

- akosha: Mirror wave-11 mermaid CI guard from crackerjack

### Fixed

- akosha: Align CLAUDE.md Core Components with actual files
- akosha: Annotate L1 cache as in-process DuckDB in query flow
- akosha: Delete dead sentence-transformers runtime from EmbeddingService
- akosha: Mark real-embedding row as not-used in this process
- akosha: Remove dead embeddings install path from README
- akosha: Remove Redis L2 sidecar from K8s deployment diagram
- akosha: Rename EMB participant to EmbeddingService (mock)
- akosha: Update embedding table to reflect mock-only EmbeddingService
- Rename wave-11 mirror references from 'crackerjack' to 'akosha'

### Documentation

- Add SB push path to Session-Buddy → Akosha ASCII flow
- akosha: Add Contract 5.2 note to Phase 0 sequence diagram
- akosha: Fix 3 README contradictions from wave-1 verifier
- audit: Apply 2026-08-12 drift fixes

### Internal

- gitignore: Add .coverage\* + untrack .coverage-ratchet.json (bodai 2026-08-17)
- gitignore: Ignore docs/archive/test-artifacts/, untrack coverage dumps

## [0.9.4] - 2026-08-12

### Internal

- Adopt coverage-ratchet at 87.62% (baseline)

## [0.9.3] - 2026-08-11

### Fixed

- akosha: Unblock test suite + layered config precedence

## [0.9.2] - 2026-07-27

### Fixed

- storage: Add missing datetime import for pydantic annotations

### Documentation

- readme: Add Bodai Ecosystem Role section

### Internal

- Bump oneiric dep to >=0.16.0
- deps: Bump crackerjack>=0.70.0; remove duplicated validator script
- Normalize LICENSE attribution to Robert Leslie and Wedgwood Web Works
- pyproject: Add [project.scripts] entry for akosha CLI

## [0.9.1] - 2026-07-21

### Added

- Initial akosha plugin manifest + starter commands

### Fixed

- akosha: Move datetime import out of TYPE_CHECKING (Pydantic v2 forward-ref resolution)

### Documentation

- akosha: Apply plan-lifecycle-unification playbook (P7.B)
- plans: Reconcile stale-done items and module-rename drift
- plans: Reconcile stale-done items and module-rename drift
- plans: Reconcile stale-done items and module-rename drift
- plans: Reconcile stale-done items and module-rename drift
- plans: Reconcile stale-done items and module-rename drift
- plans: Tick shipped checkboxes in akosha eventbridge-publisher

### Internal

- akosha: Remove LICENSE (consolidated to root-level LICENSE)
- akosha: Sync uv.lock to pyproject.toml (0.9.0)

## [0.9.0] - 2026-07-14

### Added

- Add EventBridgeConfig Pydantic model
- Add EventBridgePublisher adapter
- eventbridge: Add Akosha analytics-event publisher
- Expose publish_to_eventbridge MCP tool
- settings: Add eventbridge block to akosha.yaml
- Wire EventBridgePublisher at akosha app startup
- Wire publish\_\* into AkoshaWebSocketServer.broadcast\_\*

### Changed

- settings: Migrate AkoshaConfig to OneiricMCPConfig

### Fixed

- mcp: Re-read eventbridge.enabled per call instead of closure capture
- mcp: Return no_publisher status when publisher unwired

### Testing

- eventbridge: Add end-to-end round-trip integration tests
- eventbridge: Drop brittle private-attr assertion in Akosha adapter test
- eventbridge: Fix mid-flight coroutine test to actually drive failure path
- eventbridge: Real Oneiric transport round-trip integration tests
- eventbridge: Resolve ty complaints in Akosha unit tests

### Internal

- lint: Fix ruff complaints introduced by eventbridge module

## [0.8.4] - 2026-07-05

### Fixed

- mcp: Gate analytics tools when service is None + add changepoint analytics

### Internal

- akosha: Migrate [project.optional-dependencies] → [dependency-groups]
- gitignore: Untrack .lycheecache + add \*.backup.json rule

## [0.8.3] - 2026-06-15

### Internal

- gitignore: Add backup file patterns to silence checkpoint tool artifacts
- Untrack and delete 62 historical *.backup/*.bak files

## [0.7.0] - 2026-05-31

### Changed

- Akosha (quality: 66/100) - 2026-05-31 03:53:44

## [0.4.2] - 2026-05-02

### Added

- Delegate MCP auth to mcp_common.auth, keep MCPAuthError backward compat

### Fixed

- Address code quality issues in Akosha MCP auth wrapper
- auth: Remove \_reset_config from __all__ — private helpers not exported

## [0.4.1] - 2026-04-14

### Internal

- repo: Ignore coverage artifacts

## [0.4.0] - 2026-04-03

### Changed

- Update config, core, deps, docs
- Update configuration

### Internal

- Bump version to 0.3.2
- Bump version to 0.3.3

## [0.3.2] - 2026-04-03

### Added

- Add health check tools using mcp-common
- Add PyCharm MCP tools for cross-repo code analysis

### Changed

- Update core, deps

### Internal

- Add archive/backup directories to gitignore
- Update LICENSE copyright to 2026
- Update mcp-common to 0.9.5

## [0.3.1] - 2026-02-17

### Fixed

- **BREAKING:** Default AUTH_ENABLED to false and clean git cache

### Internal

- Remove remaining oneiric_cache file from git

## [0.3.0] - 2026-02-12

### Added

- Add JWT authentication to Akosha WebSocket
- Add TLS/WSS support to Akosha WebSocket server

### Changed

- Update config, core, deps, docs
