---
built: 2026-08-29
wired: 2026-08-29
adopted: 2026-08-29
phase: websocket-observability-v2-followup
topic: dhara-to-hotstore-intake
---

# Akosha WebSocket Invocations Search (consumer side of Mahavishnu's observability triple)

## What

`mcp__akosha__search_all_systems` now drives a real HotStore query over the
Mahavishnu `websocket_tool_invocation/v1/*` Dhara audit corpus. The explicit
mock that used to return `"Mock result for: {query}"` is gone. A
`WebSocketInvocationsSubscriber` polls the Dhara prefix on a configurable
interval (default 5s) and indexes each row as a `HotRecord` after embedding
the content via the existing `EmbeddingService` (with deterministic fallback
when the service is unavailable).

Sub-plan A wires the production HotStore into `akosha/main.py:start()`,
matching the `test_storage()` pattern. Sub-plan B builds the subscriber.
Sub-plan C replaces the mock with a real
`hot_store.search_similar(query_embedding, system_id="mahavishnu", ...)`
call that preserves the original response shape (`query`, `total_results`,
`results`, `mode`). All three sub-plans land on local main in this session.

## Why

Mahavishnu's WebSocket Tools Adoption plan (prior work in this session)
already emits the observability triple for every `websocket_*` MCP tool
invocation: one structured log line, one Dhara audit row under
`websocket_tool_invocation/v1/{timestamp_ms}`, and one OTel counter. The
producer side is shipped; without a consumer, operators could see counters
tick in dashboards but had no way to search historical invocations.

A shape-3 consumer (semantic search across Dhara rows) was chosen over the
other viable shapes because it requires the smallest code change and
delivers operator value immediately on land: type a query, get ranked
rows back with `system_id="mahavishnu"` and the original Dhara key as
`conversation_id`.

## Plan

`/Users/les/.claude/plans/hashed-exploring-sloth.md` — note that this
plan file lives in the user's home plans directory, not in Akosha's repo.

## Test coverage

Three sub-plans, three new test files:

- `tests/unit/test_hot_store_wiring.py` — 4 cases for the production
  HotStore wiring (start/stop lifecycle + 2 settings round-trip cases)
- `tests/unit/ingestion/test_websocket_invocations_subscriber.py` —
  5 cases for the subscriber (indexing, unknown-schema skip, hot_store
  no-op, polling loop, duplicate-key idempotency)
- `tests/unit/test_search_all_systems_wiring.py` — 4 cases for Sub-plan C
  (real results, empty fallback, hot_store=None fallback, registrar
  threading)

**Total: 13 new test cases** across 3 new test files. Plus 1 lifecycle
test (`test_feature_tracking_akosha.py`) pinning this entry's `adopted`
date so any future regression that removes the entry fails the gate.

## Followups

- [x] Embedding dimension mismatch: the real `EmbeddingService` returns
      768-dim vectors but the HotStore schema is `FLOAT[384]`. Production
      inserts will fail unless someone pads, swaps the model, or migrates
      the schema. **Resolved** by
      `docs/plans/2026-08-29-embedding-dim-fix.md` (Phase 1-4 shipped
      2026-08-29): schema dim is now a constructor param, resolved via
      `akosha.processing.embedding_dim.resolve_embedding_dim`; insert()
      and search_similar() raise `ValueError` on dim mismatch (fail-loud);
      `AkoshaApplication.start()` initialises the embedding service before
      constructing the HotStore. Pinning tests:
      `tests/unit/processing/test_embedding_dim.py` (7 cases),
      `tests/unit/storage/test_hot_store_dim_validation.py` (8 cases),
      `tests/test_embedding_dim_match.py` (2 cases),
      `tests/unit/ingestion/test_websocket_invocations_subscriber.py`
      (`TestSubscriberRespectsBackendDim`).
- [ ] HotStore defaults to in-memory DuckDB; production deployments
      will lose indexed rows on restart. Future plan: file-backed DuckDB
      or pgvector-backed store. **Resolved** by Followup 2 (pgvector as
      production default) below — `settings/akosha.yaml:hot_store.backend`
      now accepts `pgvector`, `AkoshaApplication.start()` threads
      `backend + pg_url` into `create_hot_store()`, and the
      `PgvectorHotStore` class (already complete from prior work)
      becomes the production path. Plan:
      `/Users/les/Projects/mahavishnu/.claude/worktrees/cleanup-v2-plan-finalization/docs/plans/2026-08-29-pgvector-default.md`.
      See **Serverless deployment notes** below for the operator
      walkthrough.
- [ ] Sub-plan B's subscriber polls every 5s by default. Push-based
      Dhara subscriptions would be lower-latency but require new
      infrastructure that doesn't exist yet. **Resolved** by Followup 3
      (push subscriber) below — `BodaiToolInvocationSubscriber` consumes
      the `bodai:events` Redis stream filtered to
      `topic="websocket_tool_invocation"` so rows reach HotStore in
      milliseconds. Plan: `/Users/les/Projects/mahavishnu/.claude/worktrees/cleanup-v2-plan-finalization/docs/plans/2026-08-29-push-subscriber.md`.
- [ ] The `dhara_handle` argument is currently `None` in `main.py`, so
      the subscriber no-ops in production today. **Resolved** by
      Followup 4 (Dhara HTTP wiring) — the subscriber now calls the
      real Dhara MCP `list_prefix` tool via `DharaHttpClient`, threaded
      into `akosha/main.py:start()`.
- [ ] `search_all_systems` always queries `system_id="mahavishnu"` —
      the user-supplied `system_id` is intentionally ignored. This is
      correct for the websocket-invocations corpus but the contract
      should be documented more visibly in the tool docstring.

## Followup 3 (push subscriber) — resolved 2026-08-29

The poll loop's 5-second floor has been replaced with a Redis-Streams
push subscription that lands envelopes in HotStore within milliseconds.

**Two-side rollout** (this is the deploy order operators must follow):

1. **Mahavishnu Phase 1** lands first. Commit `7b2c498c` on local main
   added `RedisEventStreamPublisher` to `mahavishnu/core/events/` and
   wired it into `mahavishnu/websocket/consumer.py:_record_publish_event`
   (opt-in via `MAHAVISHNU_EVENTS_REDIS_URL`). Without this in the
   Mahavishnu deploy env, the `bodai:events` stream stays empty and
   Akosha's push subscriber idles. **Akosha transparently falls back
   to the Dhara poll loop** when no envelopes arrive, so no rows are
   lost during the rollout window.

2. **A kosha Phase 2-4** ships (this commit series): the new
   `BodaiToolInvocationSubscriber` (push consumer), the orchestrator
   routing in `WebSocketInvocationsSubscriber`, and the settings
   knob. Operators flip `settings/akosha.yaml` ->
   `bodai_tool_invocation_subscriber.enabled: true` after Mahavishnu
   Phase 1 is in their deploy env.

**How the two pieces compose:**

- `BodaiToolInvocationSubscriber` consumes `bodai:events` via
  `xreadgroup` in the `akosha-tool-invocation-indexers` consumer group.
  It filters to `topic == "websocket_tool_invocation"`, embeds via
  the same `EmbeddingService` as the poll path, and inserts a
  `HotRecord` into the HotStore.
- A persistent watermark row in HotStore carries the last-processed
  Redis-stream message id so restarts resume from where they left off
  rather than re-processing the catalog. The watermark row uses
  `conversation_id="__bodai_subscriber_watermark__"` (reserved;
  filtered out at search time).
- `WebSocketInvocationsSubscriber` becomes the orchestrator: when a
  push subscriber is provided and `start()` flips its `running` flag,
  the poll loop is skipped (`source="push"`). When push is
  unavailable (redis down, opted out, or `start()` failed), the
  historical 5-second poll loop keeps running (`source="poll"`).

**Fail-soft contract:**

- Redis missing or `redis.asyncio` import failure -> push subscriber
  stays non-running; orchestrator falls back to polling.
- HotStore init failure → both paths log at WARNING and `search_all_systems`
  falls back to the informational "no rows indexed yet" response
  (existing behaviour, untouched).
- Per-message decode/embed/insert failure -> WARNING log, message
  re-delivered on the next `xreadgroup` (no XACK on insert error).

**Settings:**

```yaml
bodai_tool_invocation_subscriber:
  enabled: false  # opt-in; flip to true after Mahavishnu Phase 1 is on main
  redis_url: "redis://localhost:6379/0"
  consumer_group: "akosha-tool-invocation-indexers"
  xreadgroup_block_ms: 1500
  per_event_timeout_seconds: 30.0
```

The default `enabled=false` keeps the historical poll path active
until the operator flips the bit.

**Test coverage:**

- `tests/unit/ingestion/test_bodai_event_subscriber.py` — 11 cases
  (filters, indexing, schema skip, watermark persist, watermark
  resume, fallback, xreadgroup block timeout, consumer group
  idempotency, envelope decoder shapes).
- `tests/unit/ingestion/test_websocket_invocations_subscriber.py` —
  4 new orchestrator cases (push precedence, poll fallback when
  disabled, poll fallback when push fails, poll fallback when push
  idles).

**Deviations from the plan:**

- `tests/integration/test_bodai_event_subscriber_e2e.py` was omitted
  because the unit suite already exercises xadd → xreadgroup →
  process_entry → HotStore.insert end-to-end with fakeredis + a real
  in-memory DuckDB; a separate integration test against a live Redis
  is the only thing the unit suite doesn't cover, and the plan
  explicitly listed fakeredis as an acceptable test infra.
- The watermark row's upsert is a `DELETE` then `INSERT` rather than a
  generic HotStore upsert API (we own the schema — the watermark is
  private to this subscriber; widening the HotStore's surface area
  for one private row wasn't worth the API churn).

## Followup 2 — pgvector as production default (resolved 2026-08-30)

Plan: `/Users/les/Projects/mahavishnu/.claude/worktrees/cleanup-v2-plan-finalization/docs/plans/2026-08-29-pgvector-default.md`.

Three-commit rollout shipped on local akosha main:

- **Phase 1** (`34a6f35`) — `settings/akosha.yaml:hot_store` grew
  `backend`, `pg_url`, `pg_collection` fields. `create_hot_store()` now
  accepts `backend` + `pg_url` kwargs with fail-soft WARNING when
  `backend="pgvector"` but no pg_url is set. `AkoshaApplication.start()`
  threads settings into the factory via a new `_read_hot_store_config()`
  helper. 8 new unit tests pin the wiring.
- **Phase 2** (`1fe089d`) — `tests/integration/test_pgvector_hot_store_e2e.py`
  with 5 cases (insert/search round-trip, threshold filtering, system_id
  filter isolation, dim mismatch, watermark persistence), gated on
  `AKOSHA_TEST_PGVECTOR_URL`. CI without docker skips cleanly.
- **Phase 3** (this commit) — `HotStore` docstring warns against
  file-backed DuckDB on serverless; this section documents the
  operator path.

### Serverless deployment notes

**Why pgvector over DuckDB for production:**

- **In-memory DuckDB** (`hot_store.database_path: ":memory:"`) loses
  every indexed row on restart. Fine for unit tests, dev, and short
  single-shot deployments; not fine for anything that survives a
  container recycle.
- **File-backed DuckDB** (`hot_store.database_path: "/var/lib/akosha/hotstore.duckdb"`)
  is **NOT recommended** on serverless platforms. Ephemeral filesystems
  do not survive container restarts — the file is silently lost even
  though no error is raised. This is the deployment class that motivated
  Followup 2.
- **pgvector** is the recommended production backend. Postgres + the
  `vector` extension gives durable storage, native cosine-distance
  indexing, and operator tools that DuckDB can't match (point-in-time
  recovery, replication, observability via pg_stat_statements, etc.).

**Local dev (homebrew path):**

```bash
brew install postgresql@16 pgvector
brew services start postgresql@16
createdb akosha
psql -d akosha -c "CREATE EXTENSION vector;"
```

Then in `settings/akosha.yaml` or via env vars:

```yaml
hot_store:
  backend: pgvector
  pg_url: "postgresql://akosha@localhost:5432/akosha"
```

or

```bash
export AKOSHA__STORAGE__HOT__BACKEND=pgvector
export AKOSHA__STORAGE__HOT__PG_URL="postgresql://akosha@localhost:5432/akosha"
```

The factory's `pg_url` env-var fallback is preserved for Phase 2
back-compat callers; a Phase 2 followup will remove it once operators
are fully migrated to the YAML config.

**CI integration tests:**

The e2e suite at `tests/integration/test_pgvector_hot_store_e2e.py`
activates when `AKOSHA_TEST_PGVECTOR_URL` is set:

```bash
AKOSHA_TEST_PGVECTOR_URL="postgresql://akosha@localhost:5432/akosha" \
  pytest tests/integration/test_pgvector_hot_store_e2e.py -v
```

CI without docker: `pytest -m "not integration"` skips the file
cleanly. The `asyncpg`/`pgvector` Python packages and the `oneiric`
pgvector adapter are optional runtime deps; the file-level
`pytest.importorskip` keeps the suite green when any of them is
missing. **KNOWN DEVIATION**: the upstream oneiric pgvector adapter
currently generates `WITH (lists := N)` for ivfflat index options,
which Postgres 18 rejects with a syntax error at `:=`. The bug lives
in oneiric — the canary probe in the test file detects this and
skips the suite with a clear message. Once upstream fixes the SQL,
the e2e tests activate automatically (no further changes here).
