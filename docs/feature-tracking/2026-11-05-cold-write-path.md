---
built: 2026-11-05
wired: 2026-11-05
adopted: null
phase: cold-tier-write-path
topic: cold-storage
plan: docs/plans/2026-11-05-akosha-cold-write-path.md
---

# Cold tier write path (F5)

## What

Akosha's cold tier is Parquet batches in object storage. `ColdStore.export_batch`
is the only producer of a cold object and, until this change, nothing called
it — the tier was reachable but not writable. The configured `cold.prefix` did
not reach an object key either, because no oneiric storage settings object
has a `prefix` field; only `ColdStore` owns the key.

This change gives the tier a write path:

1. **`ColdExporter`** (`akosha/storage/cold_exporter.py`) reads warm-tier rows
   older than the age threshold, groups them into `system_id/YYYY/MM/DD`
   partitions, projects them onto the `ColdRecord` shape, and calls
   `ColdStore.export_batch`. A failed partition keeps its warm rows (export
   first, evict second).
2. **`StandardMode.initialize_cold_storage()` returns a `ColdStore`** instead
   of a bare oneiric adapter. The prefix, the bucket and the credentials now
   reach the one object that assembles the object key, and the duplicated
   adapter-settings construction in the mode is gone.
3. **The `akosha.cold_export` cron** in the MCP server lifespan runs the
   exporter once at startup and then once per `AKOSHA_COLD_EXPORT_INTERVAL_SECONDS`
   (default daily). `AKOSHA_SKIP_COLD_EXPORT=1` turns it off.
4. **`cold.age_threshold_days`** (default 90) configures the threshold in the
   nested `cold:` block; `AKOSHA__STORAGE__COLD__AGE_THRESHOLD_DAYS` overrides
   it per environment.

## Lifecycle

`built` and `wired` landed in the same change.

`built`: `tests/unit/storage/test_cold_exporter.py` (21 tests),
`tests/unit/storage/test_cold_store_prefix.py` (9),
`tests/unit/storage/test_aging_warm_roundtrip.py` (8), plus the mode,
config and lifespan tests.

`wired`: the exporter is constructed by the MCP server lifespan and run by the
`akosha.cold_export` task. `akosha_cold_store_size_bytes` — declared since
the metric was introduced but never emitted — is now set after every cycle,
and `akosha_migration_records_total{source_tier="warm",target_tier="cold"}`
records the migration.

## The blocker underneath

The cold tier's only source is the warm tier, and the warm tier never
received a row. `AgingService._get_eligible_records` handed DuckDB's JSON
column straight to `WarmRecord.metadata`, which is typed `dict[str, Any]` —
but DuckDB returns a JSON column as TEXT. The pydantic `ValidationError` was
swallowed per batch, so the migration reported `errors=1` in a log line and
moved nothing. The existing aging tests missed it because they mock the
connection with a `dict`, the shape DuckDB never returns.

Fixed with `akosha.json_utils.coerce_json_object` — the single decode site
that module was written for — and pinned by a test that uses a real DuckDB
hot store.

## Integration Contract

- **Triggered from**: the MCP server lifespan, on the `akosha.cold_export`
  task.
- **Returns to / updates**: `akosha/storage/cold_exporter.py` (new),
  `akosha/storage/cold_store.py`, `akosha/storage/aging.py`,
  `akosha/modes/standard.py`, `akosha/config.py`, `akosha/mcp/server.py`,
  `akosha/monitoring/metrics.py`, `settings/akosha.yaml`.
- **Demonstrable by**: `pytest tests/unit/storage/test_cold_exporter.py -v`.
- **Rollback signal**: `akosha_cold_store_size_bytes` stays at `0` while
  `akosha_migration_records_total{source_tier="warm"}` climbs.
- **Observability added**: `akosha_cold_store_size_bytes` (now emitted),
  `akosha_migration_records_total` (warm→cold labels).
