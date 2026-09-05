---
built: 2026-09-05
wired: null
adopted: null
phase: akosha-hardening-wave-1
topic: critical-bugs-and-dependency-hygiene
---

# Akosha Hardening — Wave 1 (P0 critical bugs + dependency hygiene)

## What

Six changes that close the audit's most severe findings and lock in
the MCP backend wiring discipline in Akosha itself:

1. **C1 — ColdStore real upload** (`akosha/storage/cold_store.py`):
   placeholder `_upload_to_storage` that logged "Would upload ..." and
   silently dropped Parquet exports is replaced with real oneiric
   storage adapters. ColdStore now supports four backends — local,
   S3, GCS, Azure Blob — selected via a new `storage_backend`
   keyword-only argument (R2 / Cloudflare is S3-compatible via a
   custom `endpoint_url`). `initialize()` / `close()` / `health()`
   are now real lifecycle methods wrapping the adapter's async
   `init()` / `cleanup()` / `health()`. `export_batch()` lazy-
   initializes so callers don't have to remember to await
   `initialize()` in the common case.

2. **C2 — `/health` aggregator** (`akosha/mcp/server.py`):
   hardcoded `/health -> ok` replaced with a probe-driven
   aggregator that returns **503 + per-feed breakdown** when any
   data feed is unhealthy or when no probe is registered. New
   module-level `set_health_probe(probe)` lets lifespan and tests
   inject the probe. `/healthz` stays process-liveness only (K8s
   convention: liveness should not depend on dependency state).

3. **C3 — IPython shell stub surface** (`akosha/shell/adapter.py`):
   five commands (`_aggregate`, `_search`, `_detect`, `_graph`,
   `_trends`) used to return `{"status": "success", ...}` with TODO
   comments — operators couldn't distinguish "no results" from
   "command not implemented". Replaced with explicit
   `{"status": "stub", "command": "<name>", "message": "...tracked
   in feature-tracking..."}` envelopes.

4. **T1-T5 — version-sync test fix**: pyproject.toml was at
   `0.14.3` but five canonical version stamps (and six test
   assertions checking those stamps) still declared `0.14.2`. The
   `test_version_sync` guard correctly surfaced the drift; this
   wave reconciles all locations and closes the loop.

5. **C5 — aiohttp dep declaration**: `akosha/api/middleware.py:116`
   imports `aiohttp` for an optional auth-service round-trip but
   the dep was never declared in pyproject.toml. A fresh `uv sync`
   would silently drop it. Pin `aiohttp>=3.12.14` (the audit-
   specified CVE floor) and add a guard test that catches drift
   in either direction.

6. **Guard test for dep drift** (`tests/unit/test_dependencies.py`):
   five cases pin the new discipline — aiohttp is declared,
   version floor is at or above 3.12.14, every aiohttp import site
   has a corresponding pyproject entry, the installed package
   exposes `ClientSession`, and every dep string is well-formed.

## Why

The 2026-09-05 audit found that Akosha — like every Bodai MCP
server we'd inspected — passed all tests, returned 200 on
`/health`, and registered 30 tools, yet every "intelligence" query
returned silence. The five changes above close the highest-severity
subset of that failure mode (P0 critical bugs that caused silent
data loss or false-positive health) and lay the runtime foundation
for the discipline that all subsequent waves will build on.

Wave 1 is **built** but not yet **wired**: the four shell commands
still return stub envelopes (Wave 2/5 will replace their bodies
with real data-feed wiring), and the `/health` aggregator defaults
to no-probe until Wave 5 registers probes for the knowledge-graph,
OTel, and code-index feeds.

## Plan

`/Users/les/Projects/akosha/docs/superpowers/plans/2026-09-05-akosha-hardening-impl.md`
(5-wave plan, 33 tasks; Wave 1 = Tasks 1.1-1.6)

Spec:
`/Users/les/Projects/akosha/docs/superpowers/specs/2026-09-05-akosha-hardening-design.md`

Cross-component discipline:
`/Users/les/Projects/mahavishnu/.claude/decisions/mcp-backend-wiring-discipline.md`

## Test coverage

Four new test files + two updates to existing files. Net delta:

| File | Cases | Notes |
|---|---|---|
| `tests/unit/test_cold_store.py` (existing) | +12 cases | `TestColdStoreBackends` class — 4-backend construction, lazy-init, required-identifier validation, close→cleanup, health pass-through, unknown-backend rejection. |
| `tests/unit/test_cold_store_security.py` (existing) | unchanged | fixture switched to local backend so security tests run without AWS creds. |
| `tests/unit/test_mcp_health_endpoint.py` (new) | 11 cases | 200 / 503 / no-probe / raising-probe / empty-probe / parametrized unhealthy shapes / `/healthz` independence / `/metrics` unchanged / probe override semantics. |
| `tests/unit/test_mcp_server_lifespan.py` (existing) | +1 case | pre-lifespan `/health` must be 503 (the audit C2 failure mode); post-lifespan `/health` must be 200 once the default probe is registered. |
| `tests/unit/test_shell_adapter_stubs.py` (new) | 21 cases | per-command parametrized over all 5 stubs × 4 assertions + 1 shape-consistency case. |
| `tests/unit/test_dependencies.py` (new) | 5 cases | declared + version-floor + bidirectional + importable + well-formed dep strings. |
| `tests/unit/test_version_sync.py` (existing) | unchanged | was failing 5/6; now passes 6/6. |

**Net Wave 1: 50 new test cases across 2 new test files; 6
previously-failing tests now pass; 1 fixture update; 1 lifespan
test updated.**

## Commits (Wave 1)

Per task, all on local main (no PRs per bodai-pre-1.0-merge-policy):

| Commit | Task |
|---|---|
| `f7adb79` | C1 — ColdStore real upload (4 backends via oneiric) |
| `b2baf27` | C2 — /health aggregator (503 on degraded) |
| `5eb704e` | C3 — IPython shell stub envelopes |
| `a2a62d0` | T1-T5 — version stamps 0.14.2 → 0.14.3 |
| `f2e23ba` | C5 — aiohttp pin + dep guard |

## Deviations from the plan

- **oneiric import path**: plan assumed
  `from oneiric.storage import S3StorageAdapter`; reality
  (`oneiric>=0.21.1`) is
  `from oneiric.adapters.storage.s3 import S3StorageAdapter`.
  Tests for backend construction target
  `akosha.storage.cold_store.S3StorageAdapter` (the rebound
  symbol), not the source module.
- **adapter methods are async**: plan assumed
  `asyncio.to_thread(adapter.save)`; reality is all adapter
  methods are `async def`. CI runs with
  `-W error::RuntimeWarning` so a future regression to the
  silent-coroutine-swallow pattern fails the test suite.
- **4 backends instead of 2**: plan specified "memory"/"s3"/"r2";
  expanded to `local`/`s3`/`gcs`/`azure` per user request, matching
  the full set of oneiric storage adapters. R2 supported via
  `endpoint_url` on the S3 backend.
- **lifespan probe is the only consumer of `_check_dependency_health`**:
  plan called for an `AkoshaApplication._check_dependency_health`
  method; instead the probe is built inline inside the
  `lifespan` closure (no separate method needed since the probe
  closure already captures `hot_store`, `embedding_service`,
  `cold_storage` from the lifespan scope). The `/health` route
  calls whatever probe is registered — `set_health_probe(None)`
  triggers the fail-loud 503 default.
- **`tests/unit/cli/test_health.py` does not exist**: plan called
  for extending a CLI health test file; akosha has no
  `tests/unit/cli/` directory. The new `/health` behaviour is
  fully exercised by `tests/unit/test_mcp_health_endpoint.py` +
  the existing `tests/unit/test_mcp_server_lifespan.py` (which
  was updated in place).

## Followups

- [ ] Wave 5 (`tasks: 5.1-5.5` in the impl plan) — wire the live
      MCP backend: knowledge-graph population, OTel ingester,
      code indexer, version bump to 0.15.0. Each new tool
      registration must include the four per-feed observability
      metrics from `mcp-backend-wiring-discipline.md` §3, and
      the lifespan must register probes so `/health` returns
      200 once feeds are actually producing.
- [ ] The 382 empty no-assert tests across ~12 files (the other
      audit deliverable) are Wave 4 territory — rewritten
      in-place with `pytest.raises` / failure-path probes and
      pinned by a self-guard (`test_no_empty_tests`).
- [ ] Apply the same discipline to `akosha/mcp/tools/` — every
      currently-registered tool should have a corresponding
      `tests/integration/test_<tool>_e2e.py` that asserts
      non-empty results, per discipline §2. (Deferred until
      Wave 5 lands so the tool list stabilises.)
- [ ] The 4-backends-in-1 design means a new `storage_backend` is
      an additive change to the `Literal[...]` union. If we add
      a fifth backend (e.g. S3-compatible MinIO), it should land
      in one PR with one new test case in
      `test_initialize_constructs_<backend>_adapter` to keep
      the per-backend coverage pattern.
