---
status: active
role: implementation
kind: plan
date: 2026-11-05
last_reviewed: 2026-11-05
topic: cold-write-path
---

# Cold tier write path — implementation plan

**Goal:** give Akosha's cold tier a write path. Aged warm-tier records
become Parquet batches in the configured cold bucket, under the configured
prefix, partitioned by `system_id/YYYY/MM/DD` — and the documentation stops
claiming a step that does not exist.

**Architecture:** `StandardMode.initialize_cold_storage()` returns a
`ColdStore` (which owns the prefix and the adapter) instead of a bare
oneiric adapter. A new `ColdExporter` (`akosha/storage/cold_exporter.py`)
reads expired rows from the warm tier, groups them into partitions, and
calls `ColdStore.export_batch()`. The MCP server lifespan runs the
exporter on a daily loop, next to the existing hot→warm aging loop.

**Tech stack:** Python 3.14, DuckDB (warm tier), pyarrow (Parquet), oneiric
storage adapters, datasketch (MinHash fingerprint), prometheus-client
(metrics), pytest.

**Spec:** `docs/specs/2026-11-05-akosha-cold-write-path-design.md`

**Defect:** F5 — "the cold tier has no write path".

## Global constraints

- `from __future__ import annotations` is the first non-comment line of
  every new or modified source file.
- Modern syntax: `X | None`, `list[str]`, `pathlib.Path`.
- stdlib `logging.getLogger(__name__)` with %-style formatting — the
  surrounding `akosha/storage/` modules use the stdlib logger, and the
  structlog keyword-argument spelling raises `TypeError` on it.
- No `assert` in production code; no `print()`.
- No new dependencies. `datasketch`, `pyarrow` and `prometheus-client` are
  already declared in `pyproject.toml`.
- No hard-coded paths, buckets or credentials.
- Pytest markers must already be registered in `pyproject.toml`
  (`--strict-markers` is on). This plan uses only `unit` and `integration`.
- Tests run with the main checkout's interpreter and the worktree as CWD:
  `/Users/les/Projects/akosha/.venv/bin/python -m pytest <paths> -q`.
- Line length: keep every line ≤ 88 characters (the repo's `ruff`
  configuration allows 100, but 88 satisfies both the repo and the
  fleet-wide convention).

## Phase 1 — design doc and plan (this commit)

- [x] T1.1 Write `docs/specs/2026-11-05-akosha-cold-write-path-design.md`.
- [x] T1.2 Write this plan.

## Phase 2 — implementation

Each task is TDD: write the failing test first, watch it fail for the
right reason, then make it pass.

### Task 2.1 — `ColdStore` accepts the substrate's backend name and normalises the prefix

**Files:** `akosha/storage/cold_store.py`, `tests/unit/storage/test_cold_store_prefix.py`

- [x] T2.1.1 Failing test: `ColdStore(storage_backend="r2")` resolves to
  the `s3` adapter (the type annotation currently says `StorageBackendName`,
  which does not include `"r2"`, so this is a type error as well as a
  runtime one).
- [x] T2.1.2 Failing test: a prefix without a trailing slash still yields
  a well-formed key (`prefix="conversations"` → `conversations/<partition>/…`,
  not `conversations<partition>/…`).
- [x] T2.1.3 Fix: widen the `storage_backend` annotation to `str` (the
  docstring already promised `"r2"`), and normalise the prefix to end with
  `/` in `__init__`.

### Task 2.2 — `ColdStore` counts the bytes it writes

**Files:** `akosha/storage/cold_store.py`, `tests/unit/storage/test_cold_store_prefix.py`

- [x] T2.2.1 Failing test: after `export_batch()`, `cold_store.bytes_exported`
  equals the size of the uploaded payload and is `0` before any export.
- [x] T2.2.2 Fix: accumulate `len(data)` in `_upload_to_storage` and expose
  it as a read-only property.

### Task 2.3 — the exporter

**Files:** `akosha/storage/cold_exporter.py` (new),
`tests/unit/storage/test_cold_exporter.py` (new)

- [x] T2.3.1 Failing test: an empty warm tier produces an empty result —
  no export call, no error, zero records.
- [x] T2.3.2 Failing test: the age threshold — a row 10 days old is not
  exported with a 90-day threshold; a row 100 days old is.
- [x] T2.3.3 Failing test: the partition path shape is
  `system_id/YYYY/MM/DD`, derived from the record's own timestamp.
- [x] T2.3.4 Failing test: the exported object's key carries the cold
  prefix (`<prefix><system_id>/YYYY/MM/DD/batch_*.parquet`).
- [x] T2.3.5 Failing test: the exported record carries the documented
  `ColdRecord` shape — `fingerprint` is a list of ints, `ultra_summary` is
  the first sentence of the warm summary, `metadata` is a dict (DuckDB
  returns JSON text, so it goes through `coerce_json_object`).
- [x] T2.3.6 Failing test: a failing partition is counted in `errors`, its
  warm rows are kept, and the other partitions still export.
- [x] T2.3.7 Fix: implement `ColdExporter`, `ColdExportStats`,
  `minhash_fingerprint` and the partition builder.
- [x] T2.3.8 Failing test: after a successful export the warm rows are
  evicted; with `evict_after_export=False` they are not.
- [x] T2.3.9 Fix: implement the eviction.

### Task 2.4 — the age threshold is configurable

**Files:** `akosha/config.py`, `settings/akosha.yaml`,
`tests/unit/test_config.py`

- [x] T2.4.1 Failing test: `get_config().cold.age_threshold_days` defaults
  to `90` and honours `AKOSHA__STORAGE__COLD__AGE_THRESHOLD_DAYS`.
- [x] T2.4.2 Fix: add the field to `ColdStorageConfig` (not
  substrate-mapped — it is an Akosha-local policy, not a fleet default)
  and document it in `settings/akosha.yaml`.

### Task 2.5 — the warm tier actually receives rows

**Files:** `akosha/storage/aging.py`,
`tests/unit/storage/test_aging_warm_roundtrip.py` (new)

- [x] T2.5.1 Failing test: with a **real** DuckDB hot store (not a mock),
  `AgingService.migrate_hot_to_warm()` moves the row into the warm store.
  Today it fails with a pydantic `ValidationError` because DuckDB returns
  the JSON `metadata` column as a string.
- [x] T2.5.2 Fix: normalise the column with
  `akosha.json_utils.coerce_json_object` in
  `AgingService._get_eligible_records()`.

### Task 2.6 — wire the exporter into the server

**Files:** `akosha/mcp/server.py`

- [x] T2.6.1 The lifespan creates a `ColdExporter` when both a warm store
  and a cold store exist, and runs it on the `akosha.cold_export` task.
- [x] T2.6.2 `AKOSHA_SKIP_COLD_EXPORT=1` suppresses the task.
- [x] T2.6.3 The task is cancelled on shutdown, like `akosha.aging`.
- [x] T2.6.4 `StandardMode.initialize_cold_storage()` returns a
  `ColdStore` (Option A). The mode no longer builds oneiric settings
  itself; the duplicate `local`/`s3` settings construction is deleted.

### Task 2.7 — observability

**Files:** `akosha/storage/cold_exporter.py`, `akosha/monitoring/metrics.py`

- [x] T2.7.1 Failing test: a completed cycle sets
  `akosha_cold_store_size_bytes` and increments
  `akosha_migration_records_total{source_tier="warm",target_tier="cold"}`.
- [x] T2.7.2 Fix: emit them from the exporter; correct the gauge's help
  text to describe what it actually measures.

### Task 2.8 — the documentation tells the truth

**Files:** `docs/architecture/MEMORY_ARCHITECTURE.md`, `ARCHITECTURE.md`

- [x] T2.8.1 `MEMORY_ARCHITECTURE.md`: the per-tier ownership map no
  longer says "(planned) warm→cold exporter" for the cold tier, and the
  warm tier's retention cell no longer says "warm→cold not implemented".
- [x] T2.8.2 `ARCHITECTURE.md`: the ingestion-flow mermaid no longer draws
  "Aging: Warm→Cold after 90 days" as if it were operational, and the
  feature checklist no longer claims a shipped tier-aging service.

### Task 2.9 — verification

- [x] T2.9.1 `pytest tests/unit/storage/ tests/unit/test_cold_store.py
  tests/unit/test_cold_store_security.py tests/unit/test_modes/
  tests/unit/test_aging.py tests/unit/test_config.py` passes.
- [x] T2.9.2 `python scripts/audit_orphans.py --root <akosha>` reports no
  orphan symbols from this work.
- [x] T2.9.3 `ruff check` and `ruff format --check` are clean on the
  changed files; `ty` reports no new diagnostics.

## Integration Contract

- **Triggered from**: the MCP server lifespan (`akosha/mcp/server.py`),
  on the `akosha.cold_export` task — one cycle at startup, then once per
  `AKOSHA_COLD_EXPORT_INTERVAL_SECONDS` (default 86400). Suppressed by
  `AKOSHA_SKIP_COLD_EXPORT=1`. The exporter runs only when a warm store
  **and** a cold store both exist.
- **Returns to / updates**: `akosha/storage/cold_exporter.py` (new),
  `akosha/storage/cold_store.py`,
  `akosha/storage/aging.py`, `akosha/modes/standard.py`,
  `akosha/config.py`, `akosha/mcp/server.py`,
  `akosha/monitoring/metrics.py`, `settings/akosha.yaml`,
  `docs/architecture/MEMORY_ARCHITECTURE.md`, `ARCHITECTURE.md`.
- **Demonstrable by**:
  `pytest tests/unit/storage/test_cold_exporter.py -v` — the exporter
  moves a 100-day-old warm row into a Parquet batch whose object key is
  `<prefix><system_id>/YYYY/MM/DD/batch_*.parquet`, and the warm row is
  gone; `pytest tests/unit/storage/test_cold_store_prefix.py -v` — the
  prefix reaches the key; `pytest tests/unit/storage/test_aging_warm_roundtrip.py -v`
  — the warm tier receives rows.
- **Rollback signal**: `akosha_cold_store_size_bytes` stays at `0` while
  `akosha_migration_records_total{source_tier="warm"}` climbs — the rows
  are being counted but nothing is being written, i.e. the export path is
  broken while the loop still runs. A second signal: the warm tier's row
  count does not fall after an export cycle, which means eviction is
  failing while the write path claims success.
- **Observability added**: `akosha_cold_store_size_bytes` (gauge, now
  actually emitted) and `akosha_migration_records_total`
  (counter, `source_tier="warm"`, `target_tier="cold"`, `status`).

## Acceptance criteria

- [x] A warm-tier row older than the threshold reaches the cold backend as
      a Parquet batch under the configured prefix.
- [x] The partition path is `system_id/YYYY/MM/DD`, taken from the
      record's own timestamp.
- [x] The threshold is `cold.age_threshold_days`, default 90, overridable
      per environment.
- [x] The hot→warm migration moves rows (the DuckDB JSON-column coercion
      is fixed and covered by a real-DuckDB test).
- [x] `akosha_cold_store_size_bytes` is emitted, not just declared.
- [x] The documentation no longer describes the cold tier as unwritten.
- [x] The storage, mode and config test suites pass.
- [x] `audit_orphans` reports no orphan symbols from this work.

## Deviations from the plan (found while implementing)

1. **The flat `cold_prefix` key was silently dropped.** `_resolve_cold_settings`
   mapped `backend` / `bucket` / `region` / `endpoint_url` / credentials from
   the caller's flat config, but not `prefix` — so an explicit
   `--config` file's `cold_prefix` stopped applying when the nested-`cold:`-block
   fix landed on this branch. The prefix decides which key space in the
   bucket Akosha writes to, so it was restored to the flat map (and pinned
   by a test).
2. **The age cutoff was comparing a tz-aware value against naive UTC
   timestamps.** DuckDB `TIMESTAMP` columns store naive wall-clock values
   and the substrate's insert path normalises to naive UTC
   (`_to_naive_utc`), but a tz-aware value bound as a query PARAMETER is
   shifted to the session's local time instead. The hot→warm cutoff was
   therefore off by the local UTC offset (eight hours in Pacific time) —
   exactly the size of the boundary the cutoff draws. Both the aging
   service and the exporter now compute a naive-UTC cutoff.
3. **`ColdStore.__init__` now normalises the prefix** to end with `"/"`.
   A prefix written without its slash produced
   `conversationssystem-001/...` — the partition concatenated straight into
   the prefix.
4. **`_resolve_cold_settings` gained the flat `cold_prefix` key** — see 1.
5. The gauge's help text changed from "Total size of cold store (Parquet
   files) in bytes" to "Bytes of Parquet data written to the cold store by
   this process". The metric NAME is unchanged; the old text described a
   total the process cannot know.
