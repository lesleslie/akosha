---
status: active
role: implementation
topic: cold-write-path
date: 2026-11-05
last_reviewed: 2026-11-05
---

# Akosha cold tier — the write path that does not exist

## Context

Akosha stores memory in three tiers.

| Tier | Age | Substrate | Written by |
|---|---|---|---|
| Hot | 0–7 days | DuckDB (memory or on-disk) + knowledge graph | `store_memory`, `batch_store_memories`, ingestion worker, `system_endpoint/` uploads |
| Warm | 7–90 days | DuckDB on-disk (`warm.db`), INT8 embeddings | `AgingService._migrate_batch` (hot→warm only) |
| Cold | 90+ days | Parquet batches in S3 / R2 / GCS / Azure | **nothing** |

The cold tier is *reachable* but not *writable*. `ColdStore.export_batch()`
is implemented, unit-tested and audited as a real upload (the Wave 1 fix
replaced a "write a temp file and log the key" stub with a real
`_upload_to_storage`), and it is the only method in the codebase that
produces a cold object. Nothing calls it.

`AKOSHA_ARCHITECTURE_AUDIT_2026-09-09.md:44` records the finding:

> **DOC LIES.** ARCHITECTURE.md does not flag warm→cold as un-implemented;
> the Ingestion Flow mermaid (line 333-346) shows "Aging: Warm→Cold after
> 90 days" as if operational.

and `git grep "ColdStore("` over production source returns **nothing** — the
class is exported from `akosha/storage/__init__.py` and instantiated only
by tests.

### The three stacked defects

**D-1 — config shape (FIXED on this branch, commits `a5c6215`, `0a07461`).**
`akosha/modes/standard.py` used to read flat keys (`cold_storage_backend`,
`cold_bucket`, `cold_prefix`) out of `self.config`. Every YAML in the
ecosystem defines the *nested* `cold:` block, and `self.config` was `{}`
by default, so the cold tier silently resolved to `backend="local"`,
`bucket="akosha-cold-data"` and never reached R2. The branch fixes the
read (`_resolve_cold_settings(self.config)`) and translates the substrate's
`"r2"` backend name onto `"s3"` at the adapter boundary
(`resolve_storage_backend`, `akosha/storage/cold_store.py:44`) because R2
speaks the S3 API with a custom `endpoint_url` and `region="auto"`.

**D-2 — the prefix never reaches an object key (OPEN).**
The branch now *reads* `cold["prefix"]` and logs it, but
`S3StorageSettings` has no `prefix` field (oneiric
`adapters/storage/s3.py:54` — `bucket`, `region`, `endpoint_url`,
credentials, `session_token`, `healthcheck_key`, `use_accelerate_endpoint`).
The only class that applies a prefix is `ColdStore` itself:
`self.prefix` (`cold_store.py:125`) and
`parquet_key = f"{self.prefix}{partition_path}/batch_{...}.parquet"`
(`cold_store.py:179`). No adapter-level prefix exists, so the prefix must
live on the object that owns the key.

**D-3 — nothing calls the write path (OPEN).**
`ColdStore.export_batch(records, partition_path=...)` is the only producer
of a cold object and it has no production caller. There is no warm→cold
aging service, no scheduled export, no operator command.

### A fourth defect the cold path depends on (found while designing this)

The warm tier — the cold exporter's only source — never receives a row.

`AgingService._get_eligible_records()` reads the hot store's `metadata`
column and hands it to `WarmRecord(metadata=...)`, which is typed
`dict[str, Any]`. But that column is declared `JSON`
(oneiric `adapters/vector/duckdb_hot_store.py`), and DuckDB returns a JSON
column as a **string**:

```
raw metadata type: <class 'str'> '{"task_class":"mcp_tool_call"}'
Batch migration failed for records 0-1: 1 validation error for WarmRecord
metadata  Input should be a valid dictionary [type=dict_type, input_value='{"task_class":"mcp_tool_call"}', input_type=str]
stats: MigrationStats(records_migrated=0, bytes_freed=0, errors=1, ...)
warm rows: (0,)
```

The exception is swallowed per batch by `_migrate_batch`, so the hot→warm
migration reports `errors=1` in a log line and moves nothing. The existing
tests miss it because `tests/unit/test_aging.py` mocks the DuckDB
connection and hands the mock a `dict` for `metadata` — the shape DuckDB
never returns. `akosha/json_utils.py::coerce_json_object` already exists
for exactly this normalisation and is used on the hot store's other read
path (`hot_store.py:346`).

A cold exporter wired to an always-empty warm tier would be "wired" in the
audit sense and dead in the operational sense. Fixing the coercion is
therefore part of this work, not an adjacent cleanup.

## Goals

1. One production path that moves aged records from the warm tier into the
   cold tier as Parquet batches, partitioned by `system_id/YYYY/MM/DD`.
2. The configured cold prefix appears in the object key that lands in the
   bucket.
3. The age threshold is operator-configurable in the established nested
   `cold:` block, with a 90-day default matching the documentation.
4. The cold tier reports its size and its migration counts as Prometheus
   metrics that are actually emitted.
5. The documentation tells the truth: `MEMORY_ARCHITECTURE.md` stops
   saying "(planned)", and `ARCHITECTURE.md` stops drawing an aging step
   that does not exist.

## Non-goals

- A cold **read** path (bulk recall via a DuckDB external scan of the
  bucket). The cold tier is write-only for now; the read side is a
  separate piece of work.
- Multi-bucket cold storage, tiering *within* the cold tier, or
  lifecycle policies on the bucket.
- Reworking the hot→warm path beyond the metadata coercion needed to make
  it move rows at all.
- Changing the substrate's ownership of `backend` / `bucket` / `prefix` /
  `compression` (`akosha/config.py::_SUBSTRATE_MAPPED_NESTED`).

## Decisions

### D1. `initialize_cold_storage` returns a `ColdStore` (Option A)

`initialize_cold_storage` currently returns a bare oneiric storage
*adapter*. The alternative is to keep returning the adapter and let
`ColdStore` accept an injected adapter (Option B).

**Option A is chosen.** The evidence:

1. **`ColdStore.initialize()` already owns the settings construction.**
   `cold_store.py:349-404` builds `LocalStorageSettings`,
   `S3StorageSettings`, `GCSStorageSettings` and `AzureBlobStorageSettings`
   from the resolved `bucket`, `prefix`, `region`, `endpoint_url`,
   `access_key_id`, `secret_access_key`, `project`, `container`, … The
   mode's current implementation builds a *second, narrower* copy of that
   logic (`local` + `s3` only, with a different local root —
   `./data/cold`, relative to the process CWD — and no GCS/Azure support).
   Option A deletes that duplicate; Option B keeps it and adds a second
   entry point (`ColdStore(adapter=...)`) that bypasses
   `ColdStore.initialize()`'s construction logic — the duplication the
   brief asks to avoid.

2. **The prefix can only live on the object that builds the key.**
   No oneiric storage settings object has a `prefix` field; the key is
   assembled inside `ColdStore.export_batch`. Returning a `ColdStore` is
   the only shape in which the prefix, the bucket and the credentials all
   reach the same object that writes the key. Option B would have the mode
   build the adapter and a separate object hold the prefix — two objects
   that must agree.

3. **`ColdStore` already exposes the lifecycle the mode wants.**
   `initialize()` / `close()` / `health()` exist and `export_batch()`
   lazy-initialises the adapter on first use, so a caller that forgot to
   await `initialize()` still works.

**Contract impact of the three call sites** — all preserved:

| Call site | Existing contract | Under Option A |
|---|---|---|
| `akosha/main.py:330` | `if cold_storage:` — truthiness | A `ColdStore` defines no `__bool__`/`__len__`, so it stays truthy. Unchanged. |
| `akosha/mcp/server.py:1625` | `cold_storage is not None` | Unchanged — `None` still means "the mode disabled it". |
| `akosha/mcp/server.py:1709` | published on the lifespan state dict | Unchanged — the object is published either way. |

**Health.** The `/health` path tests truthiness, not `ColdStore.health()`.
That stays: `ColdStore.health()` forwards to the adapter's `health()`, and
the S3 adapter's `health()` issues a `head_bucket` network call. Putting
that on the `/health` route would add a network round-trip to every probe
and turn a bucket outage into a health-check timeout. `ColdStore.health()`
remains available for a deeper, explicitly-invoked probe; `/health`
continues to report "the cold tier is configured", not "the cold tier is
reachable".

**Consequence:** the mode no longer imports `oneiric.adapters.storage` at
all. `resolve_storage_backend` is still used, but only to translate the
configured name before handing it to `ColdStore` (which resolves it again
— idempotently — at its own adapter boundary).

### D2. Export record shape and partition convention

The exporter reads warm-tier rows and produces `ColdRecord` objects
(`akosha/models/__init__.py:45`).

| `ColdRecord` field | Source (warm `conversations` row) | Note |
|---|---|---|
| `system_id` | `system_id` | verbatim |
| `conversation_id` | `conversation_id` | verbatim; also the eviction key |
| `fingerprint` | derived from `summary` | MinHash signature, `list[int]` |
| `ultra_summary` | first sentence of `summary` | "summaries only, no embeddings" — the cold tier's documented characteristic |
| `timestamp` | `timestamp` | verbatim; also picks the partition day |
| `metadata` | `metadata` | via `coerce_json_object` (DuckDB returns JSON text) |

**Fingerprint.** `ColdRecord.fingerprint` is `list[int]` and the Parquet
schema is `pa.list_(pa.int32())`. datasketch's `MinHash.hashvalues` are
`uint64`, so the top bit is masked off: every value stays a non-negative
`int32` while the per-permutation equality semantics Jaccard similarity
depends on are preserved. The construction matches
`DeduplicationService.compute_fingerprint` (same library, same
`num_perm=128`, same word-tokenisation); only the serialisation differs,
because the two consumers need different shapes — bytes for the live
fuzzy-dedup path, `list[int]` for the cold schema. When `datasketch` is
unavailable the fingerprint degrades to a deterministic SHA-256 expansion,
mirroring `DeduplicationService`'s existing fallback.

**Partition convention.** `system_id/YYYY/MM/DD`, where the date is the
**record's own** timestamp, not the export date. Two consequences worth
stating: a partition is stable (the same record always lands in the same
partition, whatever day it is exported), and one partition maps to one
Parquet object, so a re-export of the same partition produces a second
object rather than corrupting the first. `system_id` is sanitised to a
single path component (`/` and `\` → `_`; empty / `.` / `..` → `_`) so a
hostile or malformed identifier cannot escape the partition.

### D3. The age threshold lives in the nested `cold:` block

The threshold is `cold.age_threshold_days`, default **90** — the number
the documentation already uses ("Cold Tier (90+ days)").

It goes on `ColdStorageConfig` (`akosha/config.py`), the model behind the
nested `cold:` block. That block is the established convention for cold
settings, it is already layered (repo YAML → XDG overlay → env vars), and
the field is deliberately **not** substrate-mapped: `backend`, `bucket`,
`prefix` and `compression` belong to the fleet-wide substrate
(`_SUBSTRATE_MAPPED_NESTED`), but an age threshold is an Akosha-local
policy decision and must not be settable fleet-wide. Because the field is
not substrate-mapped it survives `_strip_substrate_mapped` and is
therefore settable in `settings/akosha.yaml`, in the operator's
`~/.config/akosha/local.yaml`, and via
`AKOSHA__STORAGE__COLD__AGE_THRESHOLD_DAYS`.

The exporter takes the threshold as a constructor argument (default 90);
the MCP server passes the configured value. The exporter never reads the
config itself, so it stays testable without a settings layer.

### D4. Export first, evict second

A tier migration in this repo moves data: `AgingService.migrate_hot_to_warm`
inserts into the destination and only then deletes from the source. The
cold exporter follows the same order, for the same reason — a failed
upload must never destroy the source row.

Eviction is on by default and can be disabled per exporter
(`evict_after_export=False`). The rationale for having it on: the warm
tier is documented as "7–90 days", so a warm tier that never shrinks
contradicts its own retention contract and grows without bound. The
rationale for having the switch: an operator pointing the cold tier at a
new bucket wants to see the objects land before the warm rows disappear.

A failed partition leaves its warm rows in place and is counted in
`errors`; the next cycle retries it.

### D5. Where the exporter runs

The exporter is a service (`akosha/storage/cold_exporter.py`) with no
scheduler of its own. The scheduler is a background task in the MCP
server lifespan (`akosha/mcp/server.py`), mirroring the existing
`akosha.aging` task:

- **Interval**: `AKOSHA_COLD_EXPORT_INTERVAL_SECONDS`, default 86400
  (daily). The cycle runs once at startup and then once per interval.
- **Opt-out**: `AKOSHA_SKIP_COLD_EXPORT=1`.
- **Precondition**: both a warm store and a cold store must exist. In lite
  mode there is no cold store; with `AKOSHA_SKIP_AGING=1` there is no warm
  store. Either way the exporter is not created and the tier chain stops
  where the operator asked it to.
- The exporter reuses the **same** `WarmStore` instance the hot→warm
  migration writes to. A second `duckdb.connect()` on the same file from
  the same process would fail on the file lock.

### D6. Observability

`akosha_cold_store_size_bytes` already exists as a gauge
(`akosha/monitoring/metrics.py:106`) and is **never emitted** — the only
reference in the tree is the metric's own definition and a test that
asserts its name. It is wired here:

- `ColdStore` counts the bytes it uploads (`bytes_exported`), because it is
  the component that knows the payload size.
- The exporter sets `akosha_cold_store_size_bytes` to the cumulative
  exported byte count after every cycle.
- `akosha_migration_records_total` — an existing counter with
  `source_tier` / `target_tier` / `status` labels — records the warm→cold
  migration, which is exactly what it was defined for.

The gauge's help text changes from "Total size of cold store (Parquet
files) in bytes" to a description of what is actually measured: bytes
written by this process's exporter. The metric *name* does not change.

## Error handling

| Failure | Behaviour |
|---|---|
| Warm store not initialised | `RuntimeError` — the exporter is mis-wired, not degraded |
| Cold store upload fails for one partition | The partition is logged with `logger.exception`, counted in `errors`, its warm rows are **kept**, and the cycle continues with the next partition |
| A single partition fails | Does not abort the cycle |
| The whole cycle raises (e.g. the warm query fails) | The MCP-server loop logs a warning and retries at the next interval; the server stays up |
| `datasketch` not installed | The fingerprint degrades to a SHA-256 expansion and logs a warning — the write path stays alive |
| An unknown backend name | `resolve_storage_backend` raises; the mode falls back to local storage with a warning naming the bad value (existing behaviour, preserved) |

## Alternatives considered

**Option B — inject the adapter into `ColdStore`.** Rejected: it keeps the
duplicated settings construction in the mode, splits `ColdStore` into
"build it" and "accept it" modes, and leaves the prefix on a different
object from the credentials.

**Keep the adapter return type and add a separate `get_cold_store()`.**
Rejected: two ways to build the same thing, and the mode's adapter would
still not carry the prefix.

**Schedule the export from Mahavishnu.** Rejected: the repo's boundary is
explicit — "Workflow scheduling belongs outside Akosha" (AGENTS.md,
Ecosystem Notes) — and the hot→warm aging loop already lives inside
Akosha. A Mahavishnu-driven exporter would also need the warm store
reachable from outside the process.

**Delete the cold tier.** Rejected: the substrate, the config block, the
adapters and the audit trail all point at a cold tier as a goal. The defect
is that it has no write path, not that it should not exist.

**Evict the warm rows only after a configurable delay.** Rejected as
premature: it adds a second watermark and a second failure mode (rows
eligible for export twice) for no benefit the operator asked for.

## Rollback

- `AKOSHA_SKIP_COLD_EXPORT=1` stops the exporter without a code change.
- The `cold.age_threshold_days` block can be raised to a very large value
  to make the exporter a no-op.
- Reverting this change restores the previous behaviour exactly: the mode
  returns an adapter, the prefix is not applied to any key, and no cold
  objects are written.
