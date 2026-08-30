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
      or pgvector-backed store.
- [ ] Sub-plan B's subscriber polls every 5s by default. Push-based
      Dhara subscriptions would be lower-latency but require new
      infrastructure that doesn't exist yet.
- [ ] The `dhara_handle` argument is currently `None` in `main.py`, so
      the subscriber no-ops in production today. Future task: thread a
      Dhara client into Akosha's bootstrap so the subscriber actually
      fires when the app starts.
- [ ] `search_all_systems` always queries `system_id="mahavishnu"` —
      the user-supplied `system_id` is intentionally ignored. This is
      correct for the websocket-invocations corpus but the contract
      should be documented more visibly in the tool docstring.
