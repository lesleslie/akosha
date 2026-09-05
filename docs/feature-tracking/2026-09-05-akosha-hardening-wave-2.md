---
built: 2026-09-05
wired: null
adopted: null
phase: akosha-hardening-wave-2
topic: high-bugs-and-zero-coverage
---

# Akosha Hardening — Wave 2 (P1 high bugs + zero-coverage tests)

## What

Four commits that close the P1 audit findings plus seven new test
files that bring nine previously-zero-coverage modules under test:

1. **H1 — Similarity error propagation**
   (`akosha/mcp/tools/code_graph_tools.py`):
   bare `except Exception: return 0.0` made every error path
   indistinguishable from "two graphs with zero overlap". Replaced
   with explicit empty-types guard returning 0.0; all other
   failures now propagate. One pre-existing test
   (`test_exception_returns_zero`) was flipped to
   `test_exception_propagates_when_graph_is_none` to assert the
   new contract.

2. **H2 — Real deduplication with MinHash**
   (`akosha/processing/deduplication.py`):
   `compute_fingerprint` was returning a SHA-256 digest regardless
   of caller intent; `find_similar` always returned `[]`. Both
   made dedup "wired up but functionally empty" — the canonical
   wire-up-drift pattern this discipline was written to catch.
   Replaced with a real two-backend service:
   `DeduplicationService(backend='minhash', num_perm=128,
   threshold=0.5)`. MinHash path uses datasketch.MinHash with
   whitespace-shingled tokens; serialization is
   `hashvalues.tobytes()` (datasketch ≥2.0 dropped `.hashbytes`).
   SHA-256 path is the explicit HasherFallback. `datasketch>=0.6.0`
   pinned in pyproject + uv.lock.

3. **H3 — INT8 quantization correctness**
   (`akosha/storage/aging.py`):
   `_quantize_embedding` had three compounding bugs: no clipping,
   fixed scale of 127 ignoring input magnitude, no scale
   persistence. New module-level `quantize_embedding(emb) ->
   QuantizedVector` normalizes by max-abs, clips to [-127, 127],
   returns a NamedTuple carrying the persisted scale. New
   `dequantize` divides values by scale. `_quantize_embedding`
   method preserved as a shim returning `list[int]` for
   `WarmRecord.embedding` backward compat. Migration to the
   NamedTuple shape is Wave 3.

4. **Test files for 9 zero-coverage modules** (Task 2.4a–2.4g):
   seven new test files covering nine modules that were at 0%
   coverage in the audit:

   | Module | New tests |
   |---|---|
   | `akosha/processing/fitness_analyzer.py` | 30 |
   | `akosha/mcp/client.py` | 30 |
   | `akosha/cli/commands/migrate.py` | 21 |
   | `akosha/mcp/tools/fitness_tools.py` | 9 |
   | `akosha/mcp/tools/tool_registry.py` | 12 |
   | `akosha/observability/eventbridge_adapter.py` | 6 (was 1-test no-op) |
   | Entry points `python -m akosha{,/mcp}` | 4 |

## Why

Wave 1 closed the P0 critical bugs and locked in the
`/health` aggregator + dep-drift guard infrastructure. Wave 2
moves up the severity ladder to P1 (high) bugs and adds test
coverage for the modules that the audit flagged as "wired up but
not exercised."

The high bugs all share the same data-semantics failure mode:
they make "no data" indistinguishable from "all failures." An
operator looking at similarity=0.0, dedup-misses=∞, or
quantization-ok could not tell whether the underlying pipeline was
running or not. Wave 2 turns each of those into either an explicit
exception (H1), a real similarity computation (H2), or a reversible
quantization (H3).

## Plan

`/Users/les/Projects/akosha/docs/superpowers/plans/2026-09-05-akosha-hardening-impl.md`
(Task 2.1–2.4; Wave 2 of 5)

## Test coverage

| File | Cases | Notes |
|---|---|---|
| `tests/unit/test_code_graph_tools.py` (existing) | +2 cases, 1 flipped | New: error propagation tests; flipped: `test_exception_returns_zero` → `test_exception_propagates_when_graph_is_none` |
| `tests/unit/processing/test_deduplication.py` (new) | 12 | determinism, divergence, near-match, dissimilar, indexed ordering, empty list, per-call threshold, SHA-256 byte-exact, SHA-256 no-match, is_duplicate positive + negative |
| `tests/unit/storage/test_aging.py` (new) | 18 | round-trip within 1/127, INT8 clipping, zero preservation, scale correctness, empty input, single-value extremes, 384-dim realistic embeddings, NamedTuple surface, far-out-of-range clipping, hand-computed pins, property-style random round-trip, parametrized edge cases, field pinning |
| `tests/unit/processing/test_fitness_analyzer.py` (new) | 30 | construction + clamping, add_component + dedupe, _sanitize_key_component parametrized over path/SQL injection, _compute_signal with all-success/all-failure/mixed outcomes, run_fitness_analysis lifecycle, start/stop, buffer+DLQ invariants, FitnessSignal default |
| `tests/unit/mcp/test_client.py` (new) | 30 | BodaiComponentMCPClient SSRF + response-shape coercion; DharaServiceRegistryClient list/get/list_prefix + aclose |
| `tests/unit/cli/commands/test_migrate.py` (new) | 21 | Click group, `data` (dry-run, missing source, empty, populated, confirm), `status` (env/base/project-local), `version`, internal helpers (_discover_subdirs, _copy_dir_contents, _migrate_subdirs, _resolve_destination), decorator-pinning tests |
| `tests/unit/mcp/tools/test_fitness_tools.py` (new) | 9 | init_fitness_analyzer, register_fitness_tools, run_fitness_analysis (3 paths), get_fitness_analyzer_status (2 states) |
| `tests/unit/mcp/tools/test_tool_registry.py` (new) | 12 | ToolCategory enum, FastMCPToolRegistry register/overwrite/copy/sync-rejection, ToolMetadata defaults, ToolRegistration fields |
| `tests/unit/observability/test_eventbridge_adapter.py` (new) | 6 | constructor stores bridge, publish forwards 3 attrs, RuntimeError propagation, empty payload/headers, multiple publishes, non-dict payload |
| `tests/unit/test_main_entrypoints.py` (new) | 4 | subprocess smoke tests for `python -m akosha` and `python -m akosha.mcp` |

**Net Wave 2: 142 new test cases across 7 new test files +
2 existing-file updates.** Combined with Wave 1, this repo has
gained 192 tests targeting the audit's most pressing gaps.

## Commits (Wave 2)

Per task, all on local main (no PRs per bodai-pre-1.0-merge-policy):

| Commit | Task |
|---|---|
| `5e085f6` | H1 — similarity error propagation |
| `daaa7de` | H2 — MinHash dedup + datasketch pin |
| `1e3f49b` | H3 — INT8 quantization + scale persistence |
| `52e006a` | Task 2.4a — fitness_analyzer tests |
| `ffddc9c` | Task 2.4b — mcp/client tests |
| `c37b9e4` | Task 2.4c — migrate CLI tests |
| `db3f6f8` | Task 2.4d — fitness_tools tests |
| `0bbe809` | Task 2.4e — tool_registry tests |
| `252fbdd` | Task 2.4f — eventbridge_adapter tests |
| `cd9d8a7` | Task 2.4g — main entrypoint smoke tests |

## Deviations from the plan

- **FitnessAnalyzer `compute_failure_rate` / `compute_p99_latency`** —
  the plan called for these as direct methods; the actual API is
  `run_fitness_analysis() -> dict[task_class, dict[selector,
  FitnessSignal]]`. Tests pin the real surface.
- **dedup `find_similar` return shape** — the plan wrote it as
  `list[(bytes, float)]`; the actual `bytes` is the fingerprint
  payload which the new design returns as the candidate's *index*
  `int` instead (`list[(int, float)]`). The semantic is "match this
  candidate at index N with similarity S" — index is more useful
  to callers than the raw bytes.
- **datasketch 2.0 API drift** — the plan referenced
  `MinHash.hashbytes`; datasketch 2.0 dropped that in favor of
  `hashvalues.tobytes()`. Serialization uses the new API.
- **migrate is orphan code** — `akosha/cli/commands/migrate.py` is
  never imported anywhere (parent `akosha/cli` exists as a module
  file `akosha/cli.py`, blocking the directory-as-package import
  path). Loaded via `importlib.util.spec_from_file_location`, same
  trick the existing `tests/unit/test_migrate_cli.py` uses.
- **`acosha.cli.commands` namespace doesn't exist** — `akosha/cli.py`
  (module) coexists with `akosha/cli/` (directory); the import path
  the plan used is unreachable from standard Python. The tests
  use the file-path loader to work around this without touching
  the source tree.
- **`akosha.mcp --help` actually starts uvicorn** — the module
  doesn't parse CLI args; `python -m akosha.mcp --help` binds to
  port 8682 unconditionally. The entrypoint test asserts on the
  tool-registration marker rather than a clean exit code.
- **`FitnessSignal` default `score=0.0` is pessimistic** — known
  issue surfaced by tests; should default to 1.0 when
  `samples == 0`. Pinned as a followup; not fixed in this wave to
  avoid changing dataclass semantics outside the plan's TDD scope.

## Followups

- [ ] **datasketch dep floor** — currently `>=0.6.0`; tested at
      2.0.0. Consider tightening to `>=2.0.0` since the serialization
      API change is breaking.
- [ ] **`asyncio.iscoroutinefunction` deprecation** — Python 3.16
      will remove this; `akosha/mcp/tools/tool_registry.py:54` and
      `akosha/mcp/tools/profiles.py` should switch to
      `inspect.iscoroutinefunction()`.
- [ ] **`FitnessSignal` default `score=0.0`** — flip to 1.0 when
      `samples == 0` so an empty corpus reads as "neutral" not
      "100% failed." Two test files (this wave's `test_aging` and
      `test_fitness_analyzer`) have explicit `known-issue`
      comments that need updating once the default flips.
- [ ] **WarmRecord migration to NamedTuple scale persistence**
      — `_quantize_embedding` still returns `list[int]` for
      backward compat; once Wave 3 lands, add a `scale: float`
      field to `WarmRecord` so round-trip dequantization is
      possible without recomputing the scale.
- [ ] **Coverage gate ratchet** — Wave 3 should raise the
      `--cov-fail-under` from 87.62% to 90% (the next ratchet
      milestone). The 142 new tests should give us a comfortable
      margin.
