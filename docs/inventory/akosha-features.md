---
status: draft
role: implementation
kind: inventory
date: 2026-11-20
last_reviewed: 2026-11-20
topic: akosha-feature-inventory
---

# Akosha — feature and tool inventory

> **Lane owner:** Flint Compass (`conv:d5e0fe5d3a843d8762bd18884140767c`).
> **Scope:** the advertised surface of Akosha — MCP tools, CLI commands,
> health feeds — with the state of each, the evidence for that state, the
> plan that owns it, and the disposition.
> **Method:** every state below is read from the running service or from
> the code, never from documentation. A "working" claim without a command
> and its output is not a claim.
> **Fleet-wide scope and lane split:** Kit memory `m#10119`.

---

## 1. Health feeds — the fastest read on the whole component

Read live from `GET http://127.0.0.1:8682/health` on 2026-11-20, against a
server process started **2026-11-16 09:57:07** (so: running pre-fix code).

| Feed | ok | status | entities | cycles | Verdict |
|---|---|---|---|---|---|
| `code_graphs_feed` | true | healthy | 24 | 1 | **WORKING** |
| `metrics_feed` | true | healthy | 1000 | 1 | **WORKING** |
| `skills_signer` | true | healthy | 1 | 4 | **WORKING** |
| `cold_storage` | true | — | — | — | **WORKING** |
| `embeddings` | true | — | — | — | **WORKING** (mock chain) |
| `hot_store` | true | — | — | — | **WORKING** |
| `knowledge_graph_feed` | false | `warming_up` | **0** | **3,133** | **NOT WORKING** |
| `local_traces_feed` | false | `warming_up` | **0** | **40,824** | **NOT WORKING** |
| `mcp_tool_call_feed` | false | `warming_up` | **0** | **37,691** | **NOT WORKING** |
| `aging_feed` | false | `warming_up` | **0** | 53 | **NOT WORKING** (downstream) |
| `_aggregate` | — | `warming_up` | — | — | overall `status: warming_up` |

**The central finding of this inventory.** Three feeds have cycled
**40,824**, **37,691** and **3,133** times and produced **zero** entities.
Every one of them reports `status: warming_up`. A warm-up is a bounded
transient; 40,824 cycles is a permanent stall. The shared aggregator in
`mcp-common` derives `warming_up` from "cycled at least once, nothing yet"
and has no notion of age, so a severed six-week-old ingestion pipeline read
as `warming_up` in every health check while it was broken.

**Fix landed on this branch** (`cb6a057`): `akosha/mcp/server.py` gains
`feed_is_stalled(cycles_total, entities_count)` and a
`knowledge_graph_feed["stalled"]` boolean, with the threshold at 100 cycles.
The signal is Akosha-local on purpose — changing the shared aggregator
would alter the health contract of every other MCP server in the fleet.

**Still open:** the same `stalled` signal is not yet on `local_traces_feed`,
`mcp_tool_call_feed` or `aging_feed`, and the shared aggregator still
returns `warming_up` as the `status` string. The fleet-level fix belongs in
`mcp-common`; that is not this lane.

---

## 2. The event-ingestion pipeline — NOT WORKING, now fixed

**Advertised as:** the sole producer of the `conversations` table, and
therefore the sole source of rows for the knowledge graph
(`akosha/mcp/server.py`, `source_producer` field on the health payload).

**Evidence of failure (2026-11-20, live):**

| Measure | Value |
|---|---|
| `vectors_conversations` rows in the warm pgvector store | **0** (downstream of the break — see the note below) |
| 16 × `vectors_akosha_warm_shard_*` rows | 0, except shard_001 = **1** |
| `knowledge_graph_feed.entities_count` / `edges_count` | **0** / **0** |
| `knowledge_graph_feed.source_producer_running` | **false** |
| Redis `bodai:events` stream length | 13,857 |
| Consumer group `akosha-tool-invocation-indexers`, consumers before | **446,049** |
| `XINFO CONSUMERS` latency on that group before | **158–203 s** |
| `XINFO CONSUMERS` latency after cleanup | **0.1 s** |

**Which store each number belongs to.** The `conversations` table lives in
the **hot** tier (DuckDB). The subscriber writes to it, and the
knowledge-graph refresh reads it from there. The warm pgvector tier's
`vectors_conversations` table has exactly **one** writer:
`AgingService._migrate_batch` (`akosha/storage/aging.py:125,238`), the
hot→warm migration. So the ingestion fix's observable effect is on the hot
tier and the knowledge graph (`knowledge_graph_feed.entities_count` goes
above 0). `vectors_conversations` stays at 0 until aging is enabled, which
is Phase 4 of the warm-tier plan — it is a downstream symptom, not the
success criterion for this fix.

**Seven independent defects, all now fixed on this branch:**

1. **Consumer leak.** `_auto_consumer_name()` returned a fresh
   `uuid4` per call and was called *inside* the read loop
   (`akosha/ingestion/bodai_event_subscriber.py:365`), so every
   `XREADGROUP` created a consumer. Nothing ever called
   `XGROUP DELCONSUMER`. At `xreadgroup_block_ms: 1500` that is ~50,000
   consumers per day. **Fix** (`a26ee1e`): resolve the name once in
   `start()`, use it for the life of the process, delete it in `stop()`.

2. **Wire-shape drift.** `_decode_envelope` accepted `envelope=<JSON>` and
   `{topic, payload_json, headers_json}` — **neither of which any producer
   emits**. The only writer to `bodai:events` is
   `oneiric/adapters/queue/redis_streams.py:publish`, which writes exactly
   three stream fields: `channel`, `payload` (JSON string), `headers`
   (JSON string). The live channel is `bodai.hooks.post-tool-use`. Every
   real message therefore decoded to `None` and was **XACKed and dropped
   with no warning** — which is exactly how a retired producer survives
   unnoticed. **Fix** (`a26ee1e`, `4ae9ef6`): decode the live shape, index
   `bodai.hooks.post-tool-use` alongside `websocket_tool_invocation`, accept
   unversioned payloads (the live producer emits no `version`), render
   content from the field names the live payload actually carries, and log
   decode failures at WARNING.

3. **The resume read could never deliver.** The read asked for
   `last_message_id + 1` from the watermark, which Redis documents as a
   **pending-entries** read: it can only replay the calling consumer's own
   pending entries, so a restart delivered nothing at all. **Fix**
   (`f8f5d07`): read with `>` always. The consumer group's own
   `last-delivered-id` is the position that survives a restart; the
   watermark is where a **newly created** group starts, so it does not
   replay the back-catalogue.

4. **The reclaim sweep could not run.** It ran only after a *non-empty*
   read, so a quiet stream never ran it — and the sweep is the only path
   that can retry a pending entry (`>` never returns one). A message whose
   indexing failed sat in the pending list for ever: never retried, never
   dropped, and the retry cap never reached. **Fix** (`e1f0eab`): the sweep
   runs on both paths, rate-limited to once per claim idle time.

5. **The sweep threw away the cursor.** Redis replies to `XAUTOCLAIM` with
   the id the next sweep should resume from; the code discarded it and
   started at `"0-0"` every time. Redis examines at most `count * 10`
   entries per call and an entry skipped for insufficient idle time still
   consumes that budget, so every sweep re-examined the same low-id
   prefix — and a prefix entirely inside the idle window claims nothing
   while the entries past it are unreachable. **Fix** (`8332b90`): carry
   the cursor forward.

6. **The claim idle time equalled the processing timeout.** `XAUTOCLAIM`
   reclaims across the whole consumer group and transfers ownership, so an
   entry idle for exactly as long as the per-event timeout was eligible
   for a *peer's* claim while this process was still working on it.
   **Fix** (`8332b90`): the idle time is the larger of the floor and twice
   the configured timeout.

   Findings 4–6 came from the ensemble review's redis specialist, which
   reviewed the branch as it stood three rounds earlier — so it reviewed
   a tree that had not yet been changed by its own findings. The full
   analysis, and the two findings it raised that are **not** fixed here,
   are in the recovery plan's "Round four" section.

7. **The watermark row leaked into the search results.** The plan
   excluded it from `query_traces` and missed `search_similar`, where it
   leaks *first*: its embedding is the zero vector, so its similarity is
   `NaN`, and DuckDB orders `NaN` above every number — the threshold
   cannot filter it. `akosha/ingestion/worker.py` asks for `limit=1` to
   test whether a row already exists, so the watermark takes the one
   slot and duplicate detection silently stops working. **Fix**
   (`d8b92f6`): exclude it in both paths of the store.

**Verified against the live stream after the fix:** messages decode, the
topic filter admits them, and the rendered content carries the real tool
name (`tool=mcp__bodai__correct`).

**Plan:** `docs/plans/2026-11-20-akosha-event-pipeline-recovery.md`
(this branch) and `docs/plans/2026-11-17-unified-pgvector-warm-tier.md`
task 3.2.

**Disposition:** fix landed, **not yet merged to `main` and not yet
verified end-to-end** (no restart, no backlog backfill). The recovery
plan's Task 6 holds the verification runbook.

---

## 3. MCP tool surface

~33 Akosha tools are registered on the live server in this session's
profile. Grouped by whether they have a working data feed behind them:

| Group | Tools | Feed | State |
|---|---|---|---|
| Knowledge graph | `akosha_get_graph_statistics`, `akosha_query_knowledge_graph` | `knowledge_graph_feed` | **NOT WORKING** — 0 entities. The tools answer; the answer is always empty. |
| Cross-system search | `akosha_search_all_systems` | `hot_store` | **PARTIAL** — the hot tier is a working store, but the hook events that should populate it were being dropped. |
| Pattern / anomaly | `akosha_detect_anomalies`, `akosha_analyze_trends`, `akosha_correlate_systems` | `local_traces_feed` | **NOT WORKING** — 0 entities after 40,824 cycles. |
| Tool-call analytics | `akosha_get_system_metrics`, `akosha_get_code_problems` | `mcp_tool_call_feed` | **NOT WORKING** — 0 entities after 37,691 cycles. |
| Code graph | `akosha_search_code_patterns`, `akosha_find_function_usage`, `akosha_find_path` | `code_graphs_feed` | **WORKING** — 24 entities, healthy. |
| Memory write | `akosha_store_memory`, `akosha_batch_store_memories`, `akosha_generate_embedding` | `embeddings` | **WORKING** for writes. The mock embedding chain is the risk flagged in the warm-tier spec: a mock must never reach a tier that holds semantic vectors. |
| Skills / agents | `akosha_list_skills`, `akosha_get_skill`, `akosha_list_agents`, `akosha_get_agent` | `skills_signer` | **WORKING** — 1 entity, healthy. |
| Lifecycle | `akosha_get_health`, `akosha_get_liveness`, `akosha_get_readiness` | — | **WORKING**, but see §1: the aggregate verdict says `warming_up` when the truth is "stalled". |
| Retrieval-augmented | `akosha_search_spans`, `akosha_spans_summary` | `local_traces_feed` | **NOT WORKING** — 0 entities. |

**The shape of the problem:** the tools are all registered and all
answerable. The defect is never "the tool is missing" — it is "the tool
returns an empty set forever, and the health payload says that is normal".
That is the class the fleet's `mcp-backend-wiring-discipline.md` is meant
to catch, and the class the `warming_up` verdict hid.

---

## 4. CLI surface

| Command group | State | Note |
|---|---|---|
| `akosha mcp start/status/health/stop` | **WORKING** | the only path that actually runs; the plan's R3 defect was that the subscriber was wired only into the standalone FastAPI app, not this entry point |
| `akosha doctor` | **WORKING** | shadows `warm.db` — fixed by `test/shared-dummy-fastmcp` |
| `akosha config` / `validate` | **WORKING** | |
| Vector / embedding commands | **PARTIAL** | the chain resolves to a mock in this environment |
| Warm-tier / cold-tier commands | **PARTIAL** | the DuckDB warm store is scheduled for deletion by the warm-tier plan; the pgvector backend landed on this branch |

---

## 5. Plans that own these findings

| Finding | Owning plan | Plan state |
|---|---|---|
| Event pipeline severed | `2026-11-20-akosha-event-pipeline-recovery.md` | **this branch, in review** |
| Event pipeline severed (original write-up) | `2026-11-17-unified-pgvector-warm-tier.md` task 3.2 | code half **done**; XAUTOCLAIM + PEL-read still open |
| Two warm stores; warm tier write-only | `2026-11-17-unified-pgvector-warm-tier.md` | draft; Phases 0–2 partially landed (this branch) |
| `conversations` table empty | `2026-11-05-akosha-cold-write-path.md` | active |
| OTel feed empty | `2026-09-09-akosha-otel-feed-recovery.md` | **draft since 2026-09-09 — 6 weeks stale** |
| Hardening sweep | `2026-09-05-akosha-hardening-impl.md` | **draft since 2026-09-05 — 6.5 weeks stale** |

**Coverage gaps (broken, advertised, and owned by no live plan):**

- `mcp_tool_call_feed` — 37,691 cycles, 0 entities. **No plan in Akosha's
  index owns this feed's producer.**
- `local_traces_feed` — 40,824 cycles, 0 entities. The OTel feed-recovery
  plan exists but is a 6-week-old draft and addresses the *ingester*, not
  the feed's emptiness.
- `aging_feed` — depends on the conversations table, so it is downstream of
  the ingestion fix rather than independently broken.

---

## 6. Disposition summary

| Feature | State | Disposition |
|---|---|---|
| Event ingestion (`bodai:events` → HotStore) | was NOT WORKING | **FIXED** on this branch, pending merge + end-to-end verification |
| Redis consumer hygiene | was NOT WORKING | **FIXED** on this branch |
| Feed staleness reporting | was NOT WORKING (reported `warming_up`) | **FIXED** on this branch for `knowledge_graph_feed`; **OPEN** for the other three feeds and for the shared aggregator |
| Knowledge graph | NOT WORKING | downstream of ingestion; recovers once the fix is merged, the server is restarted and the backlog is backfilled |
| Cross-system search | PARTIAL | recovers with the ingestion fix |
| Pattern/anomaly detection | NOT WORKING | **OPEN** — 40,824 empty cycles, no owning plan |
| Tool-call analytics | NOT WORKING | **OPEN** — 37,691 empty cycles, no owning plan |
| Code graph | WORKING | keep |
| Metrics, skills signer, hot store, cold storage | WORKING | keep |
| DuckDB warm store | superseded | **CONSOLIDATE** — delete per the warm-tier plan; the pgvector backend is the single warm tier |
| `enabled: false` in `settings/akosha.yaml` vs `true` in `~/.config/akosha/local.yaml` | config drift | **OPEN** — this is the same class as the fleet's `config-that-nobody-reads` plan (REQ-DEAD-002): a YAML key and the code that implements it disagree, and the operator overlay is invisible in the repo |

---

## 7. Plan-index consolidation — Akosha lane, verified against the code

Fleet-wide analysis is Cedar Well's lane (`m#10127`); the master plan index is
not regenerated from here. What follows is the Akosha-lane evidence, each row
checked against the code rather than against the plan's own status column.

The cross-repo evidence gathered alongside it — the mahavishnu tier-2
follow-up family, the mahavishnu OTel spec family's dangling edges, and the
crackerjack AI-fix cluster — is recorded in Session-Buddy as reflection
`01a1fbfd8c3071588e47bd66bca0eb4e`, because those rows belong to the lanes
that own those repositories.

Akosha's index carries **11 rows, 9 of them open** (6 plans, 3 specs, 2
decisions). The open-plan count is a backlog, not an index — but the useful
question is not how many are open. It is which of them are open for a reason
that no longer exists.

### Should be closed — implemented, never closed

| Plan | Index says | The code says |
|---|---|---|
| `2026-09-28-cold-store-gcs-endpoint-url-wiring.md` | `draft`, 8 weeks | **Complete.** `akosha/config.py:484` declares `endpoint_url`; `akosha/config.py:322` maps `AKOSHA__STORAGE__COLD__ENDPOINT_URL` onto it; `akosha/modes/standard.py:236` passes `endpoint_url=cold["endpoint_url"]` into `ColdStore`; `akosha/storage/cold_store.py:114,158,414,426` accepts and uses it. The ticket describes work that is done. |
| `2026-11-05-akosha-cold-write-path.md` (and its spec) | `active` | **Complete.** The plan's title is "the write path that does not exist". It exists: `akosha/storage/cold_exporter.py:331` calls `ColdStore.export_batch`, and `akosha/mcp/server.py:1262` (`_lifespan_init_cold_exporter`) builds the exporter. The server's own comment at `akosha/mcp/server.py:371-375` records the old state in the past tense — "`ColdStore.export_batch` had no production caller at all". |
| `2026-09-05-akosha-hardening-impl.md` | `draft`, 6.5 weeks | **Blocked on nothing.** Its `blocks_on:` points at `docs/specs/2026-09-05-akosha-hardening-design.md`, which is already `complete`. The blocker is a finished document. Either the implementation is done and the status is stale, or the plan needs re-scoping — but it is not waiting for its spec. |

### Genuinely open, and now quantified

| Plan | Index says | What it costs to leave open |
|---|---|---|
| `2026-10-05-lazy-warm-shard-init.md` | `draft`, 7 weeks | Still unimplemented: `PgvectorWarmStore.initialize()` eagerly creates every shard (`akosha/storage/pgvector_warm_store.py:167`). The plan's own framing understated the bill — it assumed one connection per shard. With a DSN, asyncpg's defaults apply (`min_size=10`, `max_size=10`), so the default `shard_count: 256` is **2,560 connections**, not 256: far past `max_connections=100` and pgbouncer's `default_pool_size=20`. The arithmetic is corrected in the docstring and in `settings/akosha.yaml` on this branch; the plan should be updated to carry it. **Conditionally stale, not unconditionally open:** Plan M (`docs/plans/2026-11-17-unified-pgvector-warm-tier.md`) §2.4 deletes `akosha/storage/pgvector_warm_store.py` once its §2.2 lands, and that file IS this plan's subject — `PgvectorWarmStore.initialize()` is the thing being made lazy. So the honest disposition is *fold it into Plan M*, or state on the plan that it is superseded when §2.2 lands. The bill stays real until then. |

#### Its documented remedy is a no-op

The plan says: *"Reducing `warm.num_partitions` to 4 unblocks both backends."*

**Setting it does nothing.** `num_partitions` is declared on
`WarmStorageConfig` (`akosha/config.py:274`, default 256) and read by
**no production code** — the only other reference in the repo is a
config test asserting the default. The warm store sizes its shards from
`akosha.config.config.shard_count` (a different field, also defaulting
to 256), via `PgvectorWarmStore._build_default_router`
(`akosha/storage/pgvector_warm_store.py:129`).

The two defaults coincidentally match at 256, which is why the mistake
was never visible: the plan's arithmetic is right and its remedy is
wrong. An operator who follows it — set `warm.num_partitions: 4`,
restart, expect the connection storm to stop — sees no change at all,
and the shard count stays at 256.

This branch removes the dead key from `settings/akosha.yaml`, which
makes the advice *worse*: it now names a key that is not in the
committed settings at all. The plan must be corrected to name
`shard_count` (the knob that is actually read) — and `shard_count` has
no `settings/` entry either, so the fix is a real one: either the
knob gets a `settings/` home, or the operator is told to set it in the
environment.

This is the `config-that-nobody-reads` pattern (the fleet's
`docs/plans/2026-11-05-config-that-nobody-reads.md`), and it is worth
one row in that plan's sweep rather than a fix here.

### Stale — the subject has been overtaken

| Plan | Index says | What the live service shows |
|---|---|---|
| `2026-09-09-akosha-otel-feed-recovery.md` | `draft`, 6 weeks | `local_traces_feed` has cycled **46,571 times with zero entities** while its OTLP receiver ingested **42,985 batches**. The receiver is working; the feed's query path returns nothing. The plan addresses the *ingester*, so it is not addressing the observed symptom. On this branch the feed now reports `degraded` instead of `warming_up`, so the symptom is visible — but the plan itself needs re-scoping, not re-dating. |

### Coverage gaps — advertised, broken, and owned by no live plan

- **`mcp_tool_call_feed`** — 42,986 cycles, zero entities, and a populated
  `last_updated_timestamp` while its source (the OTLP receiver) has ingested
  42,985 batches. No plan in Akosha's index owns this feed's producer. It is
  deliberately left out of the stall signal on this branch: the feed counts
  spans carrying a `task_class` that no instrumented tool call may set, so
  its emptiness can be legitimate and a false `degraded` would be worse than
  a missing signal. That is a decision to confirm, not a conclusion.

### The class that matters

Three of the four rows above are open plans whose SUBJECT has been resolved
in the code, and one is open whose symptom is now visible for the first
time. A status column cannot tell those apart from a plan that is genuinely
mid-flight — which is the argument for the inventory rather than the index:
the index says what a document claims, and the inventory says whether the
claim is true.
