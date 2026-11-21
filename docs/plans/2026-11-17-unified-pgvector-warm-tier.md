---
status: draft
role: implementation
kind: plan
date: 2026-11-17
last_reviewed: 2026-11-17
topic: unified-pgvector-warm-tier
plan_kind: implementation
spec: docs/specs/2026-11-17-unified-pgvector-warm-tier-design.md
related_plans:
  - docs/plans/2026-10-05-lazy-warm-shard-init.md
  - docs/plans/2026-09-09-akosha-otel-feed-recovery.md
  - docs/plans/2026-11-05-akosha-cold-write-path.md
cross_repo_plans:
  - session-buddy/docs/plans/2026-11-06-fleet-embedding-width-768.md
  - mahavishnu/docs/plans/2026-10-14-decision-adapter-category.md
  - mahavishnu/docs/plans/2026-11-19-fleet-standardisation.md
---

# Plan M — Fleet-standard embeddings, unified pgvector warm tier

> **This plan is Akosha's share of the fleet spine.** The cross-repo
> sequencing — which repo does what, in what order, and where the release
> gate sits — is in
> `mahavishnu/docs/plans/2026-11-19-fleet-standardisation.md`.
> Two items live there rather than here because they are not Akosha's:
> the **eight-line change in `mcp_common/server/telemetry.py`** that makes
> every MCP server emit `task_class`/`selector`/`outcome` (the vocabulary
> Akosha's receiver already promotes and its feed already filters on), and
> the **Oneiric release** that carries the OpenAI legs this plan's Phase 0
> depends on.

**Goal:** one embedding model, one dimension, one client, one vector store
technology across the fleet; and a warm tier that is written by something,
read by something, and indexed for approximate search.

**This plan supersedes Plan A (`2026-11-17-unified-pgvector-warm-tier`,
architecture-first) and Plan B (`2026-11-17-warm-tier-feed-first`,
feed-first).** Both were written before three findings changed the shape of
the work:

1. The irreversibility constraint that separated A from B is **void** — the
   operator does not retain legacy records and does not require backwards
   compatibility. Records may be lost.
2. **The aging path has no input.** `conversations`, the only table
   `AgingService` reads, holds 0 rows, and its only writer is disabled by
   configuration (`settings/akosha.yaml:223`,
   `otel.receiver.trace_ingester_poller: false`). The poller could not have
   worked as written: it POSTs an empty `resourceSpans` envelope and expects
   the receiver to return the spans it holds
   (`akosha/ingestion/otel_ingester.py:258-265`), pointed at Akosha's own
   export-only receiver. **A warm tier with no writer is not a warm tier.**
3. **The embedding contract is not a dimension, and there is no contract.**
   The substrate's five `embedding__*` knobs have **one** production reader
   across the whole fleet. There are four widths in use. The chain cannot
   reach Bifrost, and its mock fallback is the same width as a stale schema
   — which is why the original defect was invisible.

**Ordering:** embedding contract → adapter consolidation → unified warm
store → feed repair and the missing writer → enable aging.

**Why this order:** every fixed-width vector column in the fleet is frozen
at creation, so the width must be decided before any column exists. The
adapter consolidation must precede the warm tier because Akosha must not own
SQL. The feed repair runs in parallel with the first two — it is a
producer-side and wire-shape problem with no dependency on a dimension —
but it must land before aging is enabled, because enabling aging on an
empty source table is a no-op and enabling it on a mock-embedded source
table is a silent corruption.

**Trade-off accepted:** the fleet rebuilds its vector columns once, during
Phase 0, rather than incrementally. In exchange, no component ever holds
vectors from two different models or two different widths at the same time.

## Global constraints

- `from __future__ import annotations` is the first non-comment line of every
  new or modified source file.
- stdlib `logging.getLogger(__name__)` with %-style formatting in
  `akosha/storage/`; the structlog keyword-argument spelling raises
  `TypeError` on a stdlib logger.
- No `assert` in production code; no `print()`.
- Line length ≤ 88. Lint with `ruff`, type-check with `ty`. Both must pass.
- Tests: `/Users/les/Projects/akosha/.venv/bin/python -m pytest <paths> -q`.
  Any test opening the hot store fails while the MCP service runs (DuckDB
  file lock) — a known pre-existing condition.
- **Never bump a `pyproject.toml` version.** The operator owns version bumps
  and publishing.
- **A wrong-width vector is rejected, never coerced.** Zero-padding a
  narrower vector to the canonical width dilutes cosine by
  `sqrt(384/768) ≈ 0.707` — a 29% systematic score deflation that would
  silently break every absolute similarity threshold ever set. Re-embed
  from source text.
- **Compatibility is checked on `(model, dimensions, space_version)`, not on
  width.** A dimension check cannot distinguish two vector spaces.
- **The mock must never reach a tier that holds semantic vectors.** It is a
  startup failure, not a per-request fallback.
- No DSN, key or credential is written into a committed file. Keys are read
  from the environment at call time.
- Cross-repo tasks are marked **[oneiric]**, **[mahavishnu]**,
  **[session-buddy]** or **[crackerjack]**. A task that changes another
  repo's public surface needs that repo's release before the consumer's pin
  can move, and the pin bump is the operator's.

---

## Phase 0 — The embedding contract

*Blocks every vector-shaped task in every repo. Nothing else can start.*

### 0.1 — [oneiric] The fleet constants

Add to `oneiric/adapters/observability/embeddings.py`:

```python
FLEET_EMBEDDING_MODEL = "text-embedding-3-small"
FLEET_EMBEDDING_DIM   = 1536      # native width; no `dimensions` parameter is sent
FLEET_EMBEDDING_SPACE = "openai/text-embedding-3-small/v1"
```

- [ ] Add the three constants.
- [ ] `FLEET_EMBEDDING_DIM` is the model's **native** width (1536). The
      client therefore never sends `dimensions` — do not add a
      `FLEET_EMBEDDING_REQUESTS_DIMENSIONS` knob unless the width is
      changed, and if it ever is, the space id changes with it.
- [ ] Export them from the module's `__all__`.
- [ ] Unit test: the constants are consistent (dim is a positive int; the
      space id contains the model name).

### 0.2 — [oneiric] The OpenAI legs

- [ ] Add an `openai_gateway` leg (Bifrost) and an `openai_direct` leg
      (api.openai.com) to the probe chain, **ahead of** `llama_cpp`, so the
      order becomes
      `openai_gateway → openai_direct → llama_cpp → minimax → model2vec`.
- [ ] **Reuse the existing `oneiric/adapters/embedding/openai.py`
      `OpenAIEmbeddingAdapter`.** Do not write a parallel HTTP client — that
      adapter already handles `dimensions`, credential resolution, batching,
      retries and rate limiting. This is the central design decision of the
      fleet-768 plan and it is correct.
- [ ] The gateway leg authenticates with the **virtual key** in the
      `x-bf-vk` header, read from `BIFROST_VIRTUAL_KEY`. A provider key is a
      hard `401 virtual_key_required`.
- [ ] The direct leg reads `OPENAI_API_KEY` from the environment at call
      time. Never inline a literal.
- [ ] Base-URL convention: the base URL **includes** the API prefix
      (`http://127.0.0.1:8471/v1`, `https://api.openai.com/v1`) and the
      endpoint is `{base}/embeddings`. The existing `llama_cpp` leg is the
      one exception (bare root + `/v1/embeddings`) — leave it, but do not
      copy the convention anywhere else. A base that already ends in `/v1`
      plus a `/v1/embeddings` suffix produces `/v1/v1/embeddings` and a
      guaranteed 404.
- [ ] `backend_name()` returns `"openai"` for both OpenAI legs, so
      `BACKEND_DEFAULT_DIMS["openai"]` resolves. Expose the specific leg
      separately as `EmbeddingService.backend_leg()`.
- [ ] **Fail loud on a wrong virtual key.** Bifrost returns a misleading
      `400 "could not auto resolve a provider"` for a bad VK on an
      embeddings request, not a 401. Surface the gateway's error body
      verbatim rather than collapsing it to "embedding failed".
- [ ] **The gateway is already proven to serve this.** Verified live
      2026-11-17 after the OpenAI credits were restored: a request through
      Bifrost for `text-embedding-3-small` with **no** `dimensions`
      parameter returned HTTP 200 and a 1536-wide unit-norm vector; the same
      request with `dimensions: 1536` also returned 1536. This phase is
      plumbing, not provider access.

### 0.3 — [oneiric] The mock becomes a startup failure

- [ ] Remove the `mock` leg from any code path that feeds a semantic
      vector store. If a semantic tier cannot reach a real provider,
      **startup must fail** — not fall through.
- [ ] Keep the mock available for tests, behind an explicit opt-in, and
      label every vector it produces so a test cannot mistake it for a real
      one.
- [ ] **Do not widen the mock to the fleet width and keep writing.** That
      is the fleet-768 plan's position and this plan rejects it: the mock
      is precisely what hid the original dimension defect (a 384-wide mock
      matched a stale 384-wide schema), and a width-compatible mock is a
      silent corruption that no dimension check can detect.
- [ ] Expose the active leg and the space id on `/health`, so a tier can
      assert it is holding real vectors.

### 0.4 — [oneiric] The dimension bounds and the shape gate

Six Pydantic fields cap the dimension at 1024 and would reject 1536 **at
construction time**:

- [ ] `oneiric/core/substrate_settings.py:132` (`warm__hnsw_ef_construction`)
- [ ] `oneiric/core/substrate_settings.py:141` (`warm__hnsw_ef_search`)
- [ ] `oneiric/core/substrate_settings.py:230` (`embedding__dim`)
- [ ] `oneiric/adapters/observability/settings.py:36` (`default=384, le=1024`)
- [ ] `mahavishnu/core/config.py:1799` and `:3060` **[mahavishnu]**
- [ ] `mahavishnu/core/repositories/embeddings.py:79` **[mahavishnu]**
- [ ] `crackerjack/models/semantic_models.py:171` **[crackerjack]**

Raise each bound to admit the fleet width, and add a **cross-check** that
the configured width equals `FLEET_EMBEDDING_DIM` — raising at
construction rather than at first insert.

- [ ] `oneiric/adapters/observability/queries.py:55` —
      `if embedding.shape != (384,):` — replace the hardcoded tuple with the
      configured width. This gate currently rejects the 768 vectors
      Session-Buddy already produces.
- [ ] `oneiric/adapters/vector/vector_types.py:37` —
      `VectorBaseSettings.default_dimension = 1536` — align it with
      `FLEET_EMBEDDING_DIM` (it happens to agree today; it must not
      drift).
- [ ] `oneiric/adapters/vector/duckdb_hot_store.py:343`
      (`DEFAULT_EMBEDDING_DIM = 384`) — derive from the contract.

### 0.5 — [oneiric] The substrate contract is wired or deleted

- [ ] Decide per field: `embedding__backend`, `embedding__model`,
      `embedding__dim`, `embedding__normalize`, `warm__embedding_dim`.
- [ ] If a field stays, **wire it**: `embedding__backend` must actually
      select the chain's first leg, `embedding__dim` must reach the store
      constructors. Today all five have at most one production reader
      across the whole fleet.
- [ ] If a field cannot be wired, **delete it**. A knob that resolves but
      is never read is worse than no knob, because it looks like one in
      every review.
- [ ] If it stays, add the validator the docstring already promises:
      "mismatches trigger a hard substrate init error (REQ-WARM-DIM-001)".
      `substrate_settings.py` currently has no `model_validator` or
      `field_validator` at all.
- [ ] Fix the `Literal["openai", "fastembed", "llama_server"]` vocabulary so
      it matches the legs that actually run — it omits `minimax` and
      `model2vec`, which *are* live legs.
- [ ] Resolve the internal inconsistency: `embedding__model` defaults to
      `nomic-embed-text` (768-wide) while `embedding__dim` and
      `warm__embedding_dim` default to `384`.

### 0.6 — [akosha] Delete the dimension table

- [ ] `akosha/processing/embedding_dim.py:63-67` — delete
      `BACKEND_DEFAULT_DIMS` (`mock 384, model2vec 256, llama_cpp 768,
      minimax 1024`). The **probed** dimension is the only source of truth.
      The table is what let the mock's 384 and the live 768 coexist without
      either being wrong on its own terms.
- [ ] `akosha/processing/embedding_dim.py` `DEFAULT_DIMENSION = 384` — derive
      from the contract, or fail rather than guess.
- [ ] `akosha/processing/embeddings.py` — expose the active leg, the model
      id and the space id, so `/health` can assert
      `embeddings.mode == "real"` and `embeddings.space == <expected>`.

### 0.7 — [akosha] Rebuild the hot store's `conversations` table

- [ ] `conversations` holds **0 rows** and is the only embedding-bearing
      table in the hot store (`spans_substrate` and `metrics_substrate` have
      no embedding column). Rebuilding it is close to free.
- [ ] DuckDB cannot change a fixed-size ARRAY's width in place
      (`Conversion Error: Cannot cast array of size 384 to array of size
      1536`). Use the `EXPORT DATABASE` / `IMPORT DATABASE` route, as
      `session-buddy/scripts/migrate_embedding_dim.py` already does — that
      script takes `--to-dim` and is the fleet's reference implementation.
- [ ] **Back up `hot.db` *and* `hot.db.wal` together.** The file is 853 MB
      and carries `spans_substrate`, `metrics_substrate`,
      `_akosha_otel_receiver_state` (the receiver's resume watermark),
      `_dlq_rows` and Akosha's `code_graphs`. A copy of the `.db` without
      the `.wal` is not a copy. Deleting the db but leaving the WAL yields
      "WAL file exists but the database file is missing" and a restart
      storm.
- [ ] Stop the Akosha server first — DuckDB is single-writer and the file
      must not be held open.
- [ ] After the rebuild, verify the live table's column type is
      `FLOAT[<fleet_dim>]`.

### 0.8 — Phase 0 verification gate

- [ ] A live `/v1/embeddings` round trip through Bifrost returns vectors of
      exactly `FLEET_EMBEDDING_DIM` and a unit L2 norm.
- [ ] The direct-OpenAI leg is proven to work when Bifrost is unreachable
      (failover within the same vector space).
- [ ] The mock is **not** reachable in any semantic tier — attempting it
      fails at startup, not at first insert.
- [ ] `/health` reports the active leg, the model id and the space id.
- [ ] A vector of the wrong width is **rejected**, and a vector of the right
      width but the wrong **model** is also rejected (the space check).

---

## Phase 1 — The adapter consolidation

*Needs a Oneiric release before Akosha can use it. The version pin bump is
the operator's.*

### 1.1 — [mahavishnu] Make the fork a true subclass

`mahavishnu/adapters/pgvector_adapter.py` is a standalone 716-line class
(`:143`) with zero `oneiric` imports. Its docstring at `:3` claims it
"Extends Oneiric's PgvectorAdapter to support HNSW". It does not.

- [ ] `class PgvectorAdapter(OneiricPgvectorAdapter)`, importing from
      `oneiric.adapters.vector.pgvector`.
- [ ] `PgvectorSettings(OneiricPgvectorSettings)` adding `index_type`,
      `hnsw_config` (m / ef_construction / ef_search), `ivfflat_config`
      (lists) and `min_connections`.
- [ ] Override only: `create_collection` (port the fork's HNSW/IVFFlat
      branch, GIN metadata index and `created_at`/`updated_at` DDL
      **verbatim**, so the emitted SQL is byte-identical to today),
      `search`, `_write_documents`, `health`, `_connection_kwargs`.
- [ ] Oneiric needs **no** change in this step. `oneiric>=0.39.0` already
      provides the ACL, the JSONB decode, the `VectorBase` base class, the
      registry entry and the injectable pool factory.

### 1.2 — [mahavishnu] Fix `ef_search`

- [ ] Replace `SET LOCAL hnsw.ef_search = $1` (`:238`) with
      `SELECT set_config('hnsw.ef_search', $1, true)`. PostgreSQL's `SET` is
      a utility statement and **does not accept bind parameters**; the
      current code raises `PostgresSyntaxError` when the override is used.
- [ ] Run it on the **same pooled connection** as the query (`:236` and
      `:259` currently take two different connections) and **inside an
      explicit transaction** — `SET LOCAL` outside one is a no-op.
- [ ] Apply the **configured** `hnsw_config.ef_search` to every query. It
      is currently never applied to any query at all; pgvector's built-in
      default of 40 applies instead, coincidentally the same number, which
      is why the bug is invisible.
- [ ] Add a **real-Postgres integration test** for the `ef_search` path.
      The existing `AsyncMock` harness structurally cannot catch a SQL
      syntax error — that is the whole reason the bug survived.

### 1.3 — [mahavishnu] Fix the metadata decode and the health contract

- [ ] `pgvector_adapter.py:265` and `:353` — route metadata through
      Oneiric's `_decode_metadata` (`:487-502`). asyncpg returns JSONB as a
      **`str`** by default, and `register_vector` registers no jsonb codec.
      This is a **live bug**: `mahavishnu/ingesters/otel_ingester.py:986`
      does `result["metadata"].get(...)` on a `str` and raises
      `AttributeError`, which the MCP tool's broad `except Exception`
      swallows into an empty result list. The GET path at `:1073-1078`
      already works around it.
- [ ] `health()` — catch `asyncpg.PostgresError` as well, so an auth
      failure returns `False` rather than propagating out of `/health`.
      Oneiric's version catches only `(OSError, RuntimeError)`.

### 1.4 — [oneiric] Upstream HNSW and `halfvec`

- [ ] Add `halfvec` support and HNSW index creation to Oneiric's
      `PgvectorAdapter`.
- [ ] **Keep IVFFlat the default** so Akosha's existing collections are
      unaffected by the release. Mahavishnu passes `index_type="hnsw"`
      explicitly.
- [ ] **Derive the index name from the index method.** The fork names its
      index `{table}_embedding_hnsw_idx` (`:635`); Oneiric names its IVFFlat
      `{table}_embedding_idx` (`:273`). They never collide — which means
      switching a table from Oneiric to the fork silently leaves **two** ANN
      indexes on one column, and PostgreSQL's planner then picks one
      arbitrarily.
- [ ] **Detect and log a build-parameter mismatch.** `CREATE INDEX IF NOT
      EXISTS` matches on name only, so changing `m`, `ef_construction` or
      `lists` in config has **no effect** on an existing table and the old
      definition silently wins.
- [ ] Read `warm__hnsw_m` / `warm__hnsw_ef_construction` /
      `warm__hnsw_ef_search` from the substrate. They are declared today and
      read by nothing.
- [ ] Oneiric release → the operator bumps the pin.

### 1.5 — [mahavishnu] Delete the fork and correct the records

- [ ] Delete `mahavishnu/adapters/pgvector_adapter.py` once 1.4 has shipped
      and the pin has moved.
- [ ] `mahavishnu/ingesters/otel_ingester.py:638-647` — import Oneiric's
      adapter.
- [ ] `otel_ingester.py:821-830` — build `VectorDocument` objects
      (Oneiric reads `doc.id` / `doc.vector` / `doc.metadata`; a plain dict
      raises `AttributeError`).
- [ ] `otel_ingester.py:969-991` and `:1065-1085` — `result["score"]` →
      `result.score`; Oneiric returns `VectorSearchResult`.
- [ ] Delete the dead `mahavishnu/core/config.py:989-1032`
      `HNSWIndexConfig` (never read) and the name-colliding
      `HNSWConfig` in `mahavishnu/core/vector_search.py:53-72` (a third,
      separately dead pgvector implementation).
- [ ] **Correct the false claims:**
      `pgvector_adapter.py:3` ("Extends Oneiric's PgvectorAdapter"),
      `docs/specs/2026-10-14-decision-adapter-category-design.md:596-598`
      ("Oneiric's class supports HNSW per the verified source"),
      `docs/decisions/2026-10-14-decision-adapter-category.md:81-88`, and
      `docs/specs/2026-09-27-shared-bodai-substrate-design.md:479` (a
      `get_feed_state` that does not exist anywhere in Oneiric).
      **Deleting the fork on the stated rationale would silently drop HNSW
      indexing.**
- [ ] `mahavishnu/scripts/setup_otel_storage.sh` is an **empty file**
      (0 bytes) and is referenced by three docs — either fill it in or
      remove the references.

---

## Phase 2 — The unified pgvector warm tier

### 2.1 — The table

- [ ] Create `akosha_warm` with `embedding halfvec(<FLEET_EMBEDDING_DIM>)`,
      `system_id`, `summary`, `timestamp`, `metadata JSONB`.
- [ ] Real columns, not JSONB extraction: the cold exporter's
      `ORDER BY timestamp ASC LIMIT ?` needs an indexable timestamp, and the
      per-tenant filter needs an indexable `system_id`. Oneiric's
      `metadata @> $1::jsonb` has **no index at all**.
- [ ] HNSW index with `m = 16, ef_construction = 200` (the substrate's
      value, not the fork's 64), plus indexes on `timestamp` and
      `system_id`.
- [ ] **One table, not 256 collections.** Shard routing leaves the warm
      path entirely.

### 2.2 — The store-level API

The aging service and cold exporter currently speak DuckDB SQL against
`warm_store.conn`. They call store methods instead:

- [ ] `insert_batch(records)` — via asyncpg's `copy_records_to_table`.
      Oneiric's current `insert` is **one round trip per document**
      (`oneiric/adapters/vector/pgvector.py:524-533`), so a 1000-record
      aging batch would be 1000 round trips through pgbouncer.
- [ ] `count()` — replaces `SELECT COUNT(*) FROM conversations`.
- [ ] `fetch_older_than(cutoff, limit)` — replaces the cold exporter's
      `SELECT ... WHERE timestamp < ? ORDER BY timestamp LIMIT ?`
      (`akosha/storage/cold_exporter.py:373-381`).
- [ ] `delete_by_ids(ids)` — replaces `DELETE ... WHERE conversation_id IN
      (...)` (`cold_exporter.py:449`). **Note this is lossy**: the cold
      record keeps only a MinHash fingerprint, a one-sentence summary and
      metadata — **no embedding** (`cold_exporter.py:434`).
- [ ] `search_similar()` — note it currently has **no caller anywhere**
      (`akosha/storage/pgvector_warm_store.py`). Either wire it or drop it.

### 2.3 — Fail-loud backend selection

- [ ] `create_warm_store` currently fails soft to DuckDB when the pgvector
      backend is unavailable. Under a unified tier that is a split brain:
      aging writes DuckDB, queries read pgvector, nothing errors.
- [ ] The backend is selected **once, at startup**, and the selection is
      logged.
- [ ] If the selected backend cannot initialise, **startup fails loudly** —
      or the whole tier is disabled, consistently. No mixed state.

### 2.4 — The DuckDB warm store is deleted

- [ ] `akosha/storage/warm_store.py` (`embedding INT8[384]`) and
      `akosha/storage/pgvector_warm_store.py` (one asyncpg pool per shard,
      no `insert_batch`, no `.conn`) are removed once 2.2 is live.
- [ ] `akosha/mcp/server.py:1833`'s `create_warm_store(backend="duckdb-ssd")`
      hardcode is removed.
- [ ] `akosha/storage/aging.py:461-462` (`get_migration_stats` reads
      `warm_store.conn`) uses the store API.
- [ ] `akosha/storage/aging.py:204-242` (`_migrate_batch`) hands the raw
      float32 to the backend. The inline numpy quantisation that **drops
      the scale** is deleted; `quantize_embedding`/`dequantize`
      (`aging.py:498-519`) go with it.

---

## Phase 3 — The feed repair and the missing writer

*Independent of Phases 0–2. Runs in parallel. Must land before aging is
enabled.*

### 3.1 — The producer

- [ ] Get `task_class` onto arriving spans, or change the feed's query.
      Every one of the ~362k rows in `spans_substrate` has
      `task_class IS NULL`, and the 34,400 `tools/call` spans arriving are
      FastMCP transport-seam spans carrying only `{fastmcp.span.seam,
      mcp.method.name, fastmcp.server.name, mcp.protocol.version}`.
- [ ] `mahavishnu/mcp/tool_call_enricher.py:97-100` sets
      `task_class`/`selector`/`outcome`/`duration_ms`, but it is **not in
      the path of the spans that arrive**. **[mahavishnu]**
- [ ] Cross-repo: this is the one task that cannot be done inside Akosha.
- [ ] The subscriber indexes only `topic == "websocket_tool_invocation"`
      and XACKs everything else
      (`akosha/ingestion/bodai_event_subscriber.py:462-469`). Its own
      module docstring records that the publisher **"was removed in
      2026-09-26"**. Either re-create the producer or make the OTLP
      receiver persist `task_class` into `spans_substrate`. Until one
      exists, `mcp_tool_call_feed.entities_count` cannot be non-zero.

### 3.2 — The Redis subscriber

> **Status 2026-11-20: the code half of this task is DONE**, by
> `docs/plans/2026-11-20-akosha-event-pipeline-recovery.md` (commits
> `a26ee1e`, `4ae9ef6`). The live consumer name is now per-process and
> removed on stop; `_decode_envelope` accepts the live `{channel, headers,
> payload}` shape; `bodai.hooks.post-tool-use` is indexed alongside
> `websocket_tool_invocation`; decode failures are logged instead of
> silently dropped. The consumer group was cleaned from 446,049 consumers
> to ~200 in the live environment, and `XINFO CONSUMERS` on it went from
> 158-203 s to 0.1 s. The counts quoted below are the 2026-11-17
> measurements and are now stale. What remains open here is the
> **XAUTOCLAIM** step for the pending entries and the **PEL read** fix
> (the loop's read id is still fixed for the life of the process), both
> recorded as open items in the 2026-11-20 plan.

- [ ] `_auto_consumer_name()` (`akosha/ingestion/bodai_event_subscriber.py:406-411`)
      returns a **fresh uuid per call**, and it is called on every
      `xreadgroup` (`:365`). Make it per-process.
- [ ] **XAUTOCLAIM the 659 pending entries before deleting any consumer.**
      `XGROUP DELCONSUMER` does not return PEL entries to the group — it
      strands them under the deleted consumer, and `XPENDING` then reads
      **0**, so the loss looks like a clean drain. Order: fix the leak →
      `XAUTOCLAIM bodai:events <group> <consumer> <min-idle-ms> 0-0` → then
      delete the dead consumers in pages. **Do not `XGROUP DESTROY`** — that
      loses the pending entries and the group's position outright.
- [ ] There are **405,790 consumers** on the group
      `akosha-tool-invocation-indexers` and **660 pending entries**, against
      a stream of 13,506 entries (live count, 2026-11-17). That is why
      `XGROUP CREATE` times out and the subscriber cannot start at all.
      `akosha.err` holds **3,021** `xreadgroup error: Timeout reading from
      localhost:6379` lines.
- [ ] Teach `_decode_envelope` (`:526-587`) the live wire shape
      `{channel, headers, payload}` (documented by
      `session_buddy/mcp/events/bodai_events_publisher.py:65-67`). It
      currently understands only `envelope=<JSON>` and
      `{topic, payload_json, headers_json}` — so it returns `None` and the
      message is XACKed and dropped **with no warning**.
      **Confirmed against the live stream** — the newest entry is
      `channel = bodai.hooks.post-tool-use`, `payload` a JSON object with
      `event`/`harness`/`session_id`/`tool_name`/`tool_input`, plus
      `headers`. The group *is* advancing (`last-delivered-id` current,
      `lag: 0`, `entries-read: 17310`), so messages are being read and then
      discarded.
- [ ] **Log decode failures.** A silent drop is how a retired producer
      survived unnoticed.
- [ ] Read the **PEL**, not `>`. The loop's read ID is fixed for the life
      of the process (`:351`, `:366`), so the documented redelivery
      contract ("the same message is re-delivered on next xreadgroup",
      `:28-32`) is unimplementable as written.

### 3.3 — Observability accounting

- [ ] The aging loop logs `stats.errors` but never adds them to
      `_aging_errors` (`akosha/mcp/server.py:1859-1884`), so a per-batch
      insert failure is invisible in `/health`.
- [ ] `/health` returns HTTP 200 with four empty feeds
      (`http_ok = worst_severity < 2`, `server.py:192-207`). A feed with
      zero entities is not healthy.

### 3.4 — The aging filter reads the wrong shape

- [ ] `AgingService._get_eligible_records` filters
      `WHERE timestamp < ? AND metadata->>'task_class' = ?` — **top-level
      only** (`akosha/storage/aging.py:299-318`). Neither producer writes
      `task_class` at the top level: the OTel ingester nests it under
      `attributes` (`akosha/ingestion/otel_ingester.py:405-412`) and the
      Redis subscriber stores the raw wire payload
      (`akosha/ingestion/bodai_event_subscriber.py:610`).
- [ ] The **read** path was already fixed for this and the aging path was
      not — `HotStore.query_traces` filters on top-level **or**
      `attributes.task_class` (`akosha/storage/hot_store.py:316-317`).
      Match it.
- [ ] This is independent of 3.1. Even with `task_class` arriving, aging
      would still miss every row nested under `attributes`.

### 3.5 — **The missing writer** — the decision this plan exists to surface

`conversations` holds 0 rows, and aging reads only `conversations`. Its
only writer, the `OtelTraceIngester` poller, is disabled by configuration
(`settings/akosha.yaml:223`) and could not have worked as written.

- [ ] **Decide** which of these is the source of truth for aging:

  **(a) The OTLP receiver writes `conversations` as well as
  `spans_substrate`.** One ingestion path, one set of rows. The receiver
  already receives every span. Cost: one embedding per span at ingest.

  **(b) Aging embeds lazily at age time.** The metadata retains
  `otel.trace_id`/`span_id` (`akosha/ingestion/otel_ingester.py:409-414`),
  so the span can be re-fetched and re-embedded when it ages. Cost: one
  embedding per **aged** row rather than per ingested row, and no embedding
  cost at all for rows that never age. The window between ingest and age is
  the natural place to batch this.

  **(c) Re-enable the poller.** **Not recommended.** Its design — POST an
  empty `resourceSpans` envelope and expect the receiver to return the
  spans it holds — does not match OTLP/HTTP, and it was pointed at an
  export-only receiver.

  **(d) Decode the `bodai.hooks.*` events already on the stream.** The
  corpus is already flowing: `bodai.hooks.user-prompt-submit` (1,218),
  `post-tool-use` (483), `session-start` (274), `session-end` (20) in the
  first 2,000 entries — pushed by Mahavishnu's hook bridge. The payload
  carries **`transcript_path` and `session_id`**, the exact two fields the
  unbuilt November transcript ingester was designed to consume. This needs
  no new transport and no new producer — only a decoder for the shape that
  is already on the wire, plus removing the topic filter that discards
  every message. **This is the option that restores the operator's original
  design intent** (every machine's session content, pushed centrally, for
  collective processing) rather than working around its absence.

- [ ] Whichever is chosen, the verification gate must assert on
  **`conversations` row count**, not on `mcp_tool_call_feed.entities_count`.
  The feeds read `spans_substrate`; aging reads `conversations`. A green
  feed does not mean aging has data — that mistake is baked into both
  superseded plans.

### 3.6 — The `/health` flags that lie

- [ ] `akosha/ingestion/subscriber_wiring.py:128` reads `.running` off the
      **orchestrator** `WebSocketInvocationsSubscriber`, which has no such
      property — it exists only on the inner
      `BodaiToolInvocationSubscriber` (`bodai_event_subscriber.py:154-155`).
      So `start_event_subscriber()` always returns False and
      `server.py:1101` sets `_event_subscriber_running = False`
      unconditionally. **The consumer is alive** (Redis: `lag: 0`,
      `entries-read: 17311`). This is why
      `knowledge_graph_feed.source_producer_running` and
      `local_traces_feed.event_subscriber_running` are both false
      negatives — a feed that lies about its own producer.
- [ ] `local_traces_feed`'s declared source claims `query_traces` **plus**
      `spans_substrate`, but `entities_count` is computed **only** from
      `query_traces` (`server.py:655-658`), which reads `conversations`.
      Either count both or stop claiming to.
- [ ] The count is wrapped in `with suppress(Exception)`, so a raised query
      reads as zero rather than as an error. Surface the exception.

### 3.7 — The analytics instance that is never initialised

- [ ] `akosha/mcp/tools/group_registers.py:73` creates a **second**
      `TimeSeriesAnalytics()` that is never `initialize()`d, while the
      properly-initialised instance (`akosha/mcp/server.py:931-934`) is
      only used at shutdown (`:1372-1374`).
- [ ] The durable SQLite `~/.akosha/state/metrics.db` holds **47,386
      rows** that the tool-side instance never loads — so
      `akosha_get_system_metrics` returns `total_metrics: 0` and
      `akosha_detect_anomalies`, `akosha_analyze_trends` and
      `akosha_correlate_systems` all return "Insufficient data".
- [ ] Fix by registering the tools against the **initialised** instance.
      This is a one-line wiring fix that turns four permanently-empty
      advertised tools into working ones.

### 3.8 — The metadata conventions must match

- [ ] `_build_memory_metadata` (`akosha/mcp/tools/session_buddy_tools.py:26-42`)
      keeps only `source`, `original_id`, `type` and `ingestion_method`,
      discarding `category`, `subcategory`, `importance_score`,
      `memory_tier`, `source_type`, `project`, `namespace`, `session_id` and
      `user_id`. Preserve the lineage.
- [ ] It sets **no `task_class`**, so even a successful push would be
      invisible to `query_traces(task_class='mcp_tool_call')` and would
      never be aged by `AgingService`'s
      `metadata->>'task_class' = ?` filter (`akosha/storage/aging.py:289-320`).
- [ ] **The vocabulary must be settled before the writer is built.**
      `task_class = 'mcp_tool_call'` is a *span* concept, applied over a
      table that holds both spans and memories. Either the memory path
      gets its own `task_class` values, or the aging filter stops
      filtering on `task_class` at all. A writer built against the wrong
      vocabulary produces rows that no consumer can see — which is what
      happened to the span path.
- [ ] `akosha_search_all_systems` hard-filters `system_id="mahavishnu"`
      (`akosha_tools.py:377-381`) while `akosha_store_memory` writes
      `system_id = metadata["source"]`. Pushed memories from another host
      are invisible to search unless the caller passes
      `source: "mahavishnu"`. Make the filter the caller's choice.

### 3.9 — The Session-Buddy push client, if it is kept

Three independent defects, each of which alone stops every row:

- [ ] **Wrong URL.** `akosha_url` is `"http://localhost:8682/mcp"`
      (`session_buddy/storage/akosha_sync.py:77`) and the client appends
      `"/mcp/v1"` (`:1068`) → `http://localhost:8682/mcp/mcp/v1`. Probed
      live: **404**. Only `/mcp` is mounted.
- [ ] **Wrong tool name.** The client calls `batch_store_memories`;
      Akosha exposes `akosha_batch_store_memories`.
- [ ] **Wrong width.** Session-Buddy sends 768-dim; Akosha's hot store
      bakes `FLOAT[384]`. Resolved by Phase 0's dimension change.

And a structural blocker that needs a config change, not a code change:

- [ ] `akosha_cloud_endpoint` is **overloaded** — simultaneously the S3/R2
      endpoint and the Akosha MCP base URL. There is no setting for "the
      central Akosha's MCP URL", so pointing a second machine at a central
      Akosha is **not expressible in the current config**. Split them.

### 3.10 — The other unwired producers

- [ ] `IngestionWorker` (`akosha/ingestion/worker.py`) — the S3/R2 pull
      path, and the **surviving relative of the operator's original
      multi-machine design** (Session-Buddy uploads its whole
      `reflection.duckdb` to `systems/{hostname}/uploads/{upload_id}/` and
      Akosha pulls it) — **is never instantiated**.
      `akosha/main.py:118` initialises `self.ingestion_workers: list[Any] = []`
      and nothing appends to it. Either wire it or delete it.
- [ ] `spans_substrate.declared_pairs` is not set in any settings file, so
      the fitness analyzer has no `(task_class, selector)` pairs and
      `akosha_run_fitness_analysis` returns `no_data`.
- [ ] `metrics_substrate` holds 1,000 rows and its feed reports healthy,
      but **no MCP tool reads it** — the only reader is the health counter.
      It is also excluded from `_aggregate.all_data_feeds_ok`.
- [ ] `_index_envelope` (`bodai_event_subscriber.py:589`) additionally
      requires `payload["version"] == "1.0.0"`. The live hook payloads have
      no `version` field at all, so this check rejects them independently
      of the topic filter. Both must be relaxed.
- [ ] `discover_tools` on the live server is mcp-common's flat catalog, not
      Akosha's profile-aware one: `register_all_tools`
      (`akosha/mcp/tools/__init__.py:84`) is never called by the server. The
      profile gating is invisible at runtime.

---

## Phase 4 — Enable aging

### 4.1 — The hold-off knob

- [ ] The aging hold-off knob gates **both** the hourly loop
      (`akosha/mcp/server.py:1886`) and the **boot pre-warm migration**
      (`:1910-1932`), which runs synchronously at every boot. A service
      restart alone is enough to age rows.
- [ ] `AKOSHA_SKIP_AGING=1` is not usable: it removes the warm store
      entirely and takes the cold exporter down with it
      (`server.py:1197,1235`). The knob must skip **only** the aging
      migration loop.
- [ ] It was introduced to protect the float32 originals. Once the warm
      tier is pgvector and the input is float32, there is nothing left to
      protect — **remove it** rather than leaving a permanent off-switch.
      Until Phase 4 it stays on.

### 4.2 — Bound the hot tier by size, not age

- [ ] The OTel span rate is ~17k rows/day. A size cap bounds disk without
      touching the newest rows. An age-based sweep destroys exactly the
      float32 originals the tier exists to hold.

### 4.3 — The cold exporter moves to the store API

- [ ] `akosha/storage/cold_exporter.py:373-381` (`_fetch_expired_rows`) and
      `:449` (`_evict_rows`) read `self._warm_store.conn` with DuckDB SQL.
      They call `fetch_older_than` and `delete_by_ids` instead.
- [ ] The cold record's shape (Parquet batches, `system_id/YYYY/MM/DD`
      partitions, MinHash fingerprints) is unaffected — only the **source**
      changes.

---

## Verification gate

The whole plan is done when all of these hold, and **none of them is an
HTTP status code**:

- [ ] `embeddings.mode == "real"` on `/health`, with the model id and the
      space id matching the contract.
- [ ] A live Bifrost `/v1/embeddings` round trip returns vectors of exactly
      `FLEET_EMBEDDING_DIM`, unit-norm, and the direct-OpenAI failover
      returns vectors in the **same** space.
- [ ] A wrong-width vector is rejected; so is a right-width vector from the
      wrong model.
- [ ] The mock is unreachable in any semantic tier — a startup failure, not
      a fallback.
- [ ] `akosha_warm` exists with `halfvec(<fleet_dim>)` and an HNSW index;
      the planner uses it (`EXPLAIN` shows an index scan, not a seq scan).
- [ ] `conversations` has rows, and they were written by the path chosen in
      3.5 — **not** by `spans_substrate`'s row count.
- [ ] `mcp_tool_call_feed.entities_count > 0` **and** the row count of the
      table aging actually reads is greater than zero. Both, not either.
- [ ] `akosha_search_all_systems` returns **real rows**, not the
      hard-coded "No websocket invocations indexed yet" placeholder. The
      response must not carry `mode: "fallback"`.
- [ ] `akosha_get_system_metrics` returns a non-zero `total_metrics` —
      proving the tool reads the *initialised* analytics instance, not a
      second one that was never initialised.
- [ ] `event_subscriber_running` is **accurate** in both directions: true
      when the consumer is alive, false when it is not. The current flag is
      a false negative.
- [ ] `knowledge_graph_feed` reports `entities_count > 0` and
      `edges_count > 0`.
- [ ] `aging_feed.errors_total` is non-zero when a batch fails — proven by
      a deliberate failure, not by inspection.
- [ ] The DuckDB warm store is gone and `warm_store.conn` is no longer
      referenced anywhere.
- [ ] `/health` `_aggregate.status` is `ok` with `data_feeds_ok: true` —
      not `warming_up`.

## Risks

| Risk | Severity | Mitigation |
|---|---|---|
| The width decision changes after Phase 0 | High | The contract is `(model, dimensions, space_version)`, so a later change is a re-embed, not a redesign. But every column is frozen at creation — decide before Phase 0 exits. |
| The Bifrost virtual key is missing or wrong | High | Fail loud. A wrong VK returns a misleading `400`, not a 401 — surface the gateway's error body verbatim. |
| Bifrost's `openai` provider key does not allowlist `text-embedding-3-small` | Medium | It already does (`config/bifrost/config.template.json`). `text-embedding-3-large` would need adding. |
| Bifrost's semantic-cache plugin cannot resolve the embedding provider | Medium | Known upstream issue. The cache is a separate concern from the fleet's own embedding client. |
| Deleting the fork drops HNSW indexing | High | 1.4 upstream first; 1.5 deletes last. Correct the false "Oneiric already supports HNSW" claims in the same change. |
| Akosha's `hot.db` rebuild loses telemetry | Medium | Back up `hot.db` **and** `hot.db.wal` together; stop the server first; the file is 853 MB. |
| The mock reaches a semantic tier | High | Startup failure (0.3) plus `/health` exposing the active leg and space id. |
| The feed stays empty after the producer fix | Medium | The feed reads `spans_substrate`; aging reads `conversations`. Assert on both. |
| Oneiric's release is not published before the pin moves | Medium | The pin bump is the operator's. Phases 2–4 wait on it; Phase 0 does not. |

## Out of scope

- Moving `spans_substrate` or `metrics_substrate` to Postgres. They are
  columnar observability data with no embedding column; DuckDB is the right
  tool and Postgres is not. What standardises is the **vector** store
  technology.
- The cold store (R2/S3) and the cold tier's record shape.
- Cross-component shared vector tables. The warm table is Akosha-scoped and
  default-deny (`caller_namespace='akosha'`, `cross_namespace_grant=False`).
- Legacy record retention and backwards-compatibility shims. Pre-1.0:
  replace, do not extend.

## Open decisions

Carried from the design (§12):

1. **Where `conversations` comes from** — 3.5(a), (b), (c) or **(d)**.
   Option (d) — decoding the `bodai.hooks.*` events already on the Redis
   stream, which carry `transcript_path` and `session_id` — is the one
   that restores the operator's original design intent: every machine's
   session content, pushed centrally, for collective processing.
2. **Tenant isolation** — logical predicate or physical partition.

### Closed by the operator (2026-11-17)

- **Width: 1536.** `text-embedding-3-small`'s native width. A measured MTEB
  point (62.3). No `dimensions` parameter, so no component can drift into a
  different truncation of the same model. No local model is width-eligible,
  so the chain either reaches a real provider or fails loudly. The
  `(model, dimensions, space_version)` contract keeps a later move to
  `text-embedding-3-large` @ 1024 a re-embed rather than a redesign.
- **The mock: startup failure.** Not a per-request fallback, and not
  "widen it to the fleet width and keep writing" as the fleet-768 plan
  proposes. The fleet-768 plan must be updated to match.
