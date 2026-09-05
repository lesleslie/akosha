---
built: 2026-09-05
wired: null
adopted: null
phase: akosha-hardening-wave-3
topic: medium-bugs-and-coverage-ratchet
---

# Akosha Hardening — Wave 3 (P2 medium bugs + coverage ratchet)

## What

Three P2 audit findings closed, a coverage gate ratcheted, and a
pre-existing wiring bug surfaced and fixed:

1. **M2 — CLI version surfaces PackageNotFoundError** (`akosha/cli.py`):
   `except Exception: typer.echo("unknown")` swallowed every error
   in `akosha version`. Narrowed to `PackageNotFoundError` and
   raised `typer.BadParameter` (exit code 2) with a hint to
   `uv pip install -e .`. Any other exception propagates naturally.
   3 tests in `tests/unit/test_cli_version.py`.

2. **M3 — BootstrapOrchestrator.report_health does active ping**
   (`akosha/ingestion/orchestrator.py`):
   `report_health` returned the cached `self.last_heartbeat` without
   verifying reachability. Now actively pings the injected
   `mahavishnu_client.ping()` and surfaces the failure as
   `status="degraded"` + `ping_error`. Sets `last_actual_ping` on
   success; `last_mahavishnu_contact` preserved for backward
   compat. 8 tests in `tests/unit/ingestion/test_orchestrator_health.py`.

3. **M4 — CLI + HTTP /health status parity** (`tests/unit/test_health_consistency.py`):
   Pin the consistency invariant: both `akosha health` (CLI) and
   `GET /health` (HTTP) carry the `status` field and agree on its
   value. The CLI's `_health_probe` (snapshot) and the HTTP route
   (per-feed checks) intentionally have different shapes —
   unification would conflate two distinct consumers. 5 parity tests.

4. **Pre-existing wiring bug fix** (`akosha/storage/aging.py`):
   Five methods on `AgingService` (`_compute_checksum`,
   `_verify_checksum_compatibility`, `_delete_from_hot_store`,
   `_delete_batch_from_hot_store`, `get_migration_stats`) had drifted
   out of the class body to module level — Python parsed them as
   ordinary functions with `self` as a parameter name. Restored
   them to class methods (4-space indent inside the class). 41
   tests in `tests/test_aging_service_coverage.py` that had been
   failing were unblocked by this fix.

5. **Coverage ratchet** (`pyproject.toml` + `.coverage-ratchet.json`):
   `--cov-fail-under` raised from 87.62% (audit baseline) to 89.0%.
   Coverage went 87.62% → 89.10% (+1.48 points). The plan's 90%
   target is the next ratchet milestone.

## Why

Wave 3 closes the P2 medium-severity bugs (M2/M3/M4). The CLI
version silent-swallow bug was particularly harmful: a broken
install was indistinguishable from a healthy one. The orchestrator
self-attestation was the canonical "happy path is also the broken
path" pattern the discipline was written to catch.

The coverage ratchet is the operational enforcement: 89.0% is the
new floor, and any future change that drops below it fails the
quality gate. The plan's 90% target stays documented in
`.coverage-ratchet.json` as the next milestone — closing that gap
is Wave 5 followup work (the remaining 0.90% lives in modules that
require live postgres/redis infrastructure or full
`AkoshaApplication.start()` boot).

## Plan

`/Users/les/Projects/akosha/docs/superpowers/plans/2026-09-05-akosha-hardening-impl.md`
(Task 3.1–3.4; Wave 3 of 5)

## Test coverage

New + modified test files:

| File | Cases | Notes |
|---|---|---|
| `tests/unit/test_cli_version.py` (new) | 3 | happy path, PackageNotFoundError → BadParameter, RuntimeError propagates |
| `tests/unit/ingestion/test_orchestrator_health.py` (new) | 8 | ping success, ping ConnectionError → degraded, KeyboardInterrupt propagates, no client, missing ping method, fallback mode, timestamp, last_mahavishnu_contact backwards compat |
| `tests/unit/test_health_consistency.py` (new) | 5 | CLI degraded on config failure, HTTP 503 on probe failure, status field parity, no-probe default 503, no CLI module dependency |
| `tests/unit/test_cold_store.py` (existing) | +5 tests | close() no-cleanup-attr path, upload failure path, write_parquet failure path |
| `tests/unit/storage/test_dhara_http_client.py` (existing) | +12 tests | lifecycle (_ensure_client reuse), parse tolerance (empty content, malformed JSON, non-list parsed, missing items), HTTP 500 paths |
| `tests/unit/processing/test_fitness_analyzer.py` (existing) | +10 tests | trace fetch fail-soft, Dhara write, DLQ happy/requeue/DLQ-after-3/CB, _run_loop with sub-second poll |
| `tests/unit/processing/test_embedding_dim.py` (existing) | +5 tests | dimension() raises → attribute fallback; non-int return; backend_name() raises |
| `tests/test_aging_service_coverage.py` (existing) | flipped 1 | test_basic_quantization now asserts max-abs-normalized output |

Three pre-existing tests were updated to match the post-Wave-2
contract (the original assertions locked in the old audit bugs):
- `tests/unit/test_cli.py::test_version_unknown_when_metadata_lookup_fails`
  → flipped to assert non-zero exit (M2 bug)
- `tests/test_code_graph_tools_coverage.py::test_exception_returns_zero`
  → flipped to assert propagation (H1 bug)
- `tests/test_aging_service_coverage.py::test_basic_quantization`
  → flipped to assert max-abs-normalized output (H3 bug)

**Net Wave 3: 43 new test cases across 3 new test files +
3 existing-file updates + 1 flipping + 5 orphan-method-fix
unblocks.**

## Coverage delta

- Before Wave 3: **87.62%** (audit baseline)
- After Wave 3: **89.10%** (committed)
- Delta: **+1.48 points**
- Coverage gate ratcheted from 87.62% to 89.0%

The remaining 0.90% to reach the plan's 90% target lives in:

| Module | Coverage | Why hard |
|---|---|---|
| `akosha/storage/pgvector_hot_store.py` | 33% | Requires live postgres connection |
| `akosha/ingestion/bodai_event_subscriber.py` | 68% | Requires Redis xreadgroup mock or live Redis |
| `akosha/main.py` | 67% | Requires full `AkoshaApplication.start()` lifecycle |
| `akosha/mcp/tools/group_registers.py` | 42% | Large registration module — needs more careful mocks |

These are all documented in `.coverage-ratchet.json` as the
next_milestone (90) — closing that gap is Wave 5 followup work.

## Commits (Wave 3)

Per task, all on local main (no PRs per bodai-pre-1.0-merge-policy):

| Commit | Task |
|---|---|
| `8f2ce17` | M2 — CLI version surfaces PackageNotFoundError |
| `da6fbe8` | M3 — Orchestrator active ping |
| `4d9ac1d` | M4 — CLI + HTTP /health parity |
| `aa65d2e` | Pre-existing orphan-method bug fix + 3 test flips |
| `93c364e` | embedding_dim exception branches (+5 tests) |
| `194cd18` | dhara_http_client lifecycle + parse tolerance (+12 tests) |
| `32400c0` | fitness_analyzer DLQ + Dhara write paths (+10 tests) |
| `83b7b96` | cold_store cleanup + error paths (+5 tests) |
| `d2f3082` | Coverage ratchet 87.62% → 89.0% |

## Deviations from the plan

- **Plan's 90% target ratcheted to 89.0%** — achieving 90% in
  this wave would require integration tests (live postgres for
  pgvector_hot_store) that are out of scope. The plan's 90%
  target stays pinned as `next_milestone` in the ratchet file.
- **Plan called for 6-8 new test files in Task 3.4**; Wave 3 added
  to 6 existing test files instead (cleaner diff, less churn) +
  3 new files for the audit-finding fixes. Total 7 files touched.
- **`_health_probe` unification deferred** — the plan called for
  promoting `_health_probe` to a public alias and having the HTTP
  route consume it. The two surfaces have intentionally
  different shapes (CLI snapshot vs HTTP per-feed checks), so the
  parity test pins consistency on the `status` field without
  unifying the implementations.
- **`datasketch` dep floor tightening deferred** — Task 2.4 noted
  that `datasketch>=0.6.0` is locked at 2.0.0. The tightening
  to `>=2.0.0` is deferred to a future commit because (a) it's
  not a security issue, and (b) it would break any consumer still
  on 1.x.

## Followups

- [ ] **90% coverage milestone** (per `.coverage-ratchet.json
      next_milestone`) — close the remaining 0.90% gap:
      - `pgvector_hot_store.py` needs integration test infra
      - `bodai_event_subscriber.py` needs fakeredis or live redis
      - `main.py` needs AkoshaApplication lifecycle test
      - `group_registers.py` needs careful mock setup
- [ ] **datasketch>=2.0.0 floor** — break the 1.x compat (not a
      blocker since this repo only ships 2.0).
- [ ] **`asyncio.iscoroutinefunction` deprecation** —
      `akosha/mcp/tools/tool_registry.py:54` should switch to
      `inspect.iscoroutinefunction()` before Python 3.16.
- [ ] **`FitnessSignal` default `score=0.0`** — flip to 1.0 when
      `samples == 0` so an empty corpus reads as "neutral" not
      "100% failed."
- [ ] **Wire live MCP backend** (Wave 5) — knowledge-graph
      population, OTel ingester, code indexer. Each new tool
      registration must include the four per-feed observability
      metrics from `mcp-backend-wiring-discipline.md` §3.
