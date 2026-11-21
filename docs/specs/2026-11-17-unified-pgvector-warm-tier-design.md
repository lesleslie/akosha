# Unified pgvector Warm Tier — Design

**Status:** Draft for review
**Date:** 2026-11-17
**Revised:** 2026-11-17 — after the fleet-standardisation decisions
**Repos touched:** `akosha` (primary), `oneiric` (adapter + embedding client + settings), `mahavishnu` (adapter fork), `session-buddy` (embedding chain), `crackerjack` (dimension bounds)
**Plan:** `docs/plans/2026-11-17-unified-pgvector-warm-tier.md` (Plan M — supersedes the architecture-first and feed-first plans)
**Supersedes:** the two-warm-store arrangement described in `akosha/mcp/server.py:1815-1833`

______________________________________________________________________

## 1. Intent and why

Akosha's warm tier is a write-only sink with no consumer.

Three facts establish this, all verified against the running system on 2026-11-17:

1. **Nothing reads the warm tier.** A repo-wide search finds exactly one query
   against a warm store: `SELECT COUNT(*) FROM conversations` inside
   `AgingService.get_migration_stats` (`akosha/storage/aging.py:462`). No MCP
   tool searches it. `akosha/query/distributed.py` searches the *hot* tier.
1. **Two warm stores exist.** `AkoshaApplication.warm_store`
   (`akosha/main.py:154`) can be pgvector and is never read. The MCP server's
   `aging_warm_store` (`akosha/mcp/server.py:1833`) is pinned to `duckdb-ssd`
   and is both written (aging) and read (cold export).
1. **The pgvector path is strictly dominated.** As written it stores
   INT8-quantized vectors widened back to float4 — the same bytes as float32 —
   and loses ~1.6% of top-10 recall. The DuckDB backend's `INT8[384]` column
   saves a real 4× on disk. The quantization was a DuckDB storage-format
   decision that becomes a pure loss the moment the tier moves to Postgres.

Meanwhile the current per-shard-collection design multiplies everything:
256 collections, 256 asyncpg pools (one per shard), and 256 IVFFlat indexes
created with `lists=100` — which need roughly 25M rows before they are
useful. The tier has 0 rows.

**The irreversibility.** Aging is a one-way lossy door. In
`AgingService._migrate_batch` (`akosha/storage/aging.py:204-242`) the float32
embedding is quantized to INT8, written to warm, and the hot row is deleted.
The cold tier keeps only a MinHash fingerprint and a one-sentence summary — no
embedding. Once a record ages into the DuckDB warm store, its float32
embedding cannot be recovered.

**That constraint governed the ordering until 2026-11-17, when the operator
retired it.** The fleet does not retain legacy records and does not require
backwards compatibility; records may be lost. The ordering question was only
ever "which records do we sacrifice," and the answer is now *all of them,
freely*. Section 8 is rewritten around what actually binds: the embedding
dimension.

______________________________________________________________________

## 2. Goals

- One warm store. One connection pool. One table. One vector index.
- A warm tier whose records can be searched semantically.
- The aging→cold chain and any query path read and write the *same* store.
- An embedding contract pinned in one place, so a model change is a
  migration rather than a silent dimension mismatch.
- **A fleet-wide embedding standard**: one model, one dimension, one client,
  one vector store technology. The fleet's own brief (2026-11-17) is
  "standardise embeddings, dbs, models and such as much as possible
  throughout the ecosystem for simplicity, standardisation and compatibility
  while still maintaining the flexibility the oneiric adapters give us as far
  as having multiple options available to us."

## 3. Non-goals

- Changing the cold store (R2/S3) or the cold tier's record shape.
- **Moving the hot tier's telemetry tables to Postgres.** `spans_substrate`
  (~362k rows) and `metrics_substrate` (~3.1M rows) are columnar
  observability data with no embedding column; DuckDB is the right tool for
  them and Postgres is not. What standardises is the *vector* store
  technology, not the telemetry store.
- Cross-component shared vector tables. The warm table is Akosha-scoped and
  default-deny (`caller_namespace='akosha'`, `cross_namespace_grant=False`).
- Replacing the OTel span ingestion path or the hot tier's telemetry schema.

## 4. Anti-goals

- Degrading to a different backend while a query path reads another. Today
  `create_warm_store` fails soft to DuckDB; under a unified tier that is a
  silent split brain.
- A second warm store.
- A config key that resolves but is never read (the class of bug that
  produced the `parents[2]` path defect, the `warm:` substrate strip,
  Mahavishnu's dead `embeddings:` YAML block and its dead committed
  `bifrost:` block). **Correction (2026-11-19):** Mahavishnu's
  `observability:` block is **not** in this class — it is read in four
  places, and it holds `tool_enrichment_enabled`, the flag that gates the
  `task_class` producer off. An earlier draft of this document wrongly
  listed it as dead.

______________________________________________________________________

## 5. Decisions

### D1 — One vector store technology: pgvector, halfvec precision

The DuckDB warm tier is retired, not migrated. With legacy retention and
backwards compatibility both out of scope, there is no reason to keep a
second vector store that stores INT8 and cannot do approximate nearest
neighbour search at all. What remains is a single choice of column type:

| Column | Bytes/row @ D=1536 | Top-10 recall vs float32 | Verdict |
|---|---|---|---|
| `vector(1536)` | 6144 | 1.0000 | rejected — 2× the bytes for no recall gain |
| **`halfvec(1536)`** | **3072** | **1.0000** | **chosen** |
| INT8 widened to float4 | 6144 | 0.9840 | rejected — same bytes as float32, strictly worse |
| `sparsevec` | n/a | n/a | rejected — different metric, needs its own index |

Measured on 20,000 unit-norm 384-dim vectors across 200 tight clusters,
recall@10 against exact float32 neighbours:

```
float32  vector/4B              4   -7.90e-09     1.0000     1.0000
halfvec  halfvec/2B             2   -4.16e-09     1.0000     1.0000
INT8 as-float32 (today)         4    2.64e-05     0.9840     1.0000
```

`halfvec` is float16 — 10 explicit mantissa bits, ~4.9e-4 relative
precision — and is half the bytes of float32 with no measurable recall loss.
pgvector 0.8.6 supports `halfvec_cosine_ops` for HNSW (verified against the
live catalog).

**INT8 is removed from the pgvector path entirely**, not converted to
float32: it stores the same bytes as float32 while being strictly less
accurate. The DuckDB warm store's `INT8[384]` column is deleted rather than
widened, because the table it lived in is deleted.

Consequence: `WarmRecord.embedding: list[int]` cannot be the carrier for the
pgvector path. The aging service must hand the raw float32 to the backend and
let the backend choose its representation. This is the one model change this
design requires.

**The width in the table header is now D=1536, but the measurement above was
run at 384.** The measurement is per-dimension — it establishes that halfvec's
*relative* precision loss is below the noise floor of recall@10 at that
width — and the conclusion transfers because the comparison is float32 vs
halfvec vs INT8 *at a fixed width*. At D=1536 the float32 reference is
2× larger; the halfvec mantissa is the same 10 bits.

### D2 — One table, not 256 collections

A single table with `system_id` as an indexed column replaces the
per-shard-collection layout. Shard routing leaves the warm path entirely
(`ShardRouter` remains in use for the hot tier's per-system DuckDB files).

Rejected alternatives:

- **Partition by `system_id`** (LIST/HASH). Restores physical tenant
  isolation, but each partition gets its own index and its own training data
  — the small-index problem returns.
- **Keep per-shard collections.** Preserves the 256-pool and 256-index
  explosion.

Trade-off accepted: tenant isolation becomes a policy (an indexed predicate)
rather than a physical boundary. For a single-tenant internal deployment this
is acceptable; if it is not, D2 must be revisited.

### D3 — HNSW, not IVFFlat

HNSW is the correct index for a tier that starts empty.

- **IVFFlat needs a trained dataset.** With `lists=100` it needs ~100k rows
  before it clusters meaningfully. The Oneiric adapter hardcodes
  `lists=100`; across 256 shards that is ~25M rows. At 1 row the planner
  declines to use the index at all (verified: `Seq Scan` on the live
  `vectors_akosha_warm_shard_001`).
- **HNSW has no data floor.** It works from the first row and degrades
  gracefully.

Parameters come from the **substrate**, which already declares them and
which nothing reads today
(`oneiric/core/substrate_settings.py:120-145`):

| Field | Substrate default | Mahavishnu's fork default |
|---|---|---|
| `warm__hnsw_m` | 16 | 16 |
| `warm__hnsw_ef_construction` | **200** | 64 |
| `warm__hnsw_ef_search` | **64** | 40 |

The substrate's `ef_construction=200` is roughly 3× the build cost of 64 and
produces a materially different index. **The substrate is the source of
truth** — these fields exist precisely so that the warm tier's index
parameters are fleet-wide rather than adapter-local. **This design makes
them live**: the consolidated Oneiric adapter reads them, and the fork's
defaults are deleted with the fork.

**Two defects in the fork's HNSW implementation must not be carried across.**

1. **`ef_search` is applied by a statement that raises.** The fork runs
   `SET LOCAL hnsw.ef_search = $1`
   (`mahavishnu/adapters/pgvector_adapter.py:238`). PostgreSQL's `SET` is a
   utility statement and **does not accept bind parameters** — the portable
   form is `SELECT set_config('hnsw.ef_search', $1, true)`. The fork also
   runs the `SET` on one pooled connection (`:236`) and the query on another
   (`:259`), and `SET LOCAL` outside an explicit transaction is a no-op. The
   configured `ef_search` therefore **never reaches any query**; pgvector's
   built-in default of 40 applies instead, which is coincidentally the same
   number and hides the bug. No test covers it — the suite mocks the
   connection, which structurally cannot catch a SQL syntax error.
1. **`CREATE INDEX IF NOT EXISTS` matches on name only.** Changing `m`,
   `ef_construction` or `lists` in config has **no effect** on an existing
   table; the old definition silently wins with no warning. The consolidated
   adapter must compare the live index definition against the intended one
   and log a mismatch.

**One naming hazard.** The fork names its index `{table}_embedding_hnsw_idx`
(`:635`); Oneiric's names its IVFFlat `{table}_embedding_idx` (`:273`). They
never collide — which means switching a table from Oneiric to the fork
silently leaves **two** ANN indexes on one column, and PostgreSQL's planner
then picks one arbitrarily. The consolidated adapter must use a single index
name derived from the index method.

### D4 — Schema ownership

The table needs columns the Oneiric adapter's fixed schema
(`id TEXT PK, embedding vector(N), metadata JSONB`) cannot express:

```sql
CREATE TABLE akosha_warm (
    id          TEXT PRIMARY KEY,
    embedding   halfvec(<FLEET_EMBEDDING_DIM>),
    system_id   TEXT        NOT NULL,
    summary     TEXT,
    timestamp   TIMESTAMPTZ NOT NULL,
    metadata    JSONB       DEFAULT '{}'::jsonb
);
CREATE INDEX akosha_warm_embedding_hnsw_idx
    ON akosha_warm USING hnsw (embedding halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 200);
CREATE INDEX akosha_warm_timestamp_idx ON akosha_warm (timestamp);
CREATE INDEX akosha_warm_system_id_idx  ON akosha_warm (system_id);
```

`<FLEET_EMBEDDING_DIM>` is a placeholder, not a literal. The dimension is
resolved from the contract (Section 7.7) at DDL time — 1536 if the
recommended width is adopted, 768 if the existing fleet plan is kept. It is
never written into a committed file as a number, because a number in a
committed file is a number that will disagree with the contract.
`ef_construction` is 200, the substrate's value, not the fork's 64 (D3).

Real columns, not JSONB extraction, because:

- the cold exporter's `ORDER BY timestamp ASC LIMIT ?` needs an indexable
  timestamp — with JSONB-only metadata that is a seq scan plus sort;
- the per-tenant filter needs an indexable `system_id` — the Oneiric
  adapter's `metadata @> $1::jsonb` has no index at all.

**Where the DDL lives.** The DDL is **owned by Oneiric's `PgvectorAdapter`**,
extended to support `halfvec` and HNSW. Akosha consumes a Oneiric adapter
and never owns SQL. Akosha must not depend on Mahavishnu.

The previous revision of this design said the adapter work should be ported
from Mahavishnu's "superset adapter" because it "is already the base class".
**Both halves of that are wrong.** Mahavishnu's
`mahavishnu/adapters/pgvector_adapter.py` is a standalone 716-line class
(`:143`, imports at `:17-32`) with **zero** `oneiric` imports — it does not
extend Oneiric's adapter and is not a superset of it. Oneiric's adapter has
**no HNSW at all** (IVFFlat only, `oneiric/adapters/vector/pgvector.py:271-278`),
and Mahavishnu's own design and decision documents assert the opposite
(`docs/specs/2026-10-14-decision-adapter-category-design.md:596-598`,
`docs/decisions/2026-10-14-decision-adapter-category.md:81-88`). Those claims
are false and must be corrected before the existing deletion plan is executed —
deleting the fork on that rationale would silently drop HNSW indexing.

What the fork *does* have, and what must not be carried across: HNSW
creation, the `m`/`ef_construction`/`ef_search` knobs, a GIN index on
metadata, and `created_at`/`updated_at`. What it gets wrong and must be
fixed rather than inherited: `ef_search` is applied by
`SET LOCAL hnsw.ef_search = $1`, which PostgreSQL rejects (a utility
statement does not take bind parameters) and which runs on a different pooled
connection from the query (§D3); the configured `ef_search` never reaches a
query at all; JSONB comes back from asyncpg as a `str`, which breaks the
search path in `mahavishnu/ingesters/otel_ingester.py:986` while the GET
path at `:1073-1078` works around it; and `health()` catches bare
`Exception` where Oneiric's catches only `(OSError, RuntimeError)`.

The fork has **no live production caller**: the MCP server constructs
`OtelIngester` without `storage_type` (so DuckDB is always used), and only
`create_otel_ingester()` — reached from examples and tests — honours the
configured `storage_type: "postgresql"`. Its tables (`vectors_otel_traces`)
are almost certainly absent from the live database. That removes the
blast-radius objection to consolidation.

**The sequence is two steps, not one.** Step 1: make the fork a true
subclass of Oneiric's adapter, overriding only what differs, and fix the
defects above — no Oneiric release, no version-pin bump, no change to
Akosha. Step 2: upstream the HNSW DDL into Oneiric's adapter as a separate
release, keeping IVFFlat the default so Akosha's collections are unaffected,
then delete the fork. The version bump is the operator's, per the standing
rule.

### D5 — The embedding contract is `(model, dimensions, space_version)`

Not a dimension. A dimension check cannot distinguish two vector spaces
(Section 7.5), and the fleet has already shipped a chain that admits a
*different model* at the same width — EmbeddingGemma-300M and
`text-embedding-3-small` are both 768-wide and are not interchangeable.

Every vector store carries all three values, and every write is refused on a
mismatch of any of them. The constants live in Oneiric
(`FLEET_EMBEDDING_MODEL` / `_DIM` / `_SPACE`, Section 7.7); each component
mirrors the width with its own module-level `int` so a cross-repo check can
read them without importing across repos.

The table's dimension is **derived** from the resolved embedding dimension,
never hardcoded in Akosha — and the substrate's `embedding__*` knobs are
either wired to this contract or deleted. A knob that resolves but is never
read is the defect class this whole document is about.

### D6 — Failure semantics

`create_warm_store` currently fails soft to DuckDB when the pgvector backend
is unavailable. Under a unified tier that is a split brain: aging writes
DuckDB, queries read pgvector, nothing errors. Under A:

- the backend is selected once, at startup, and the selection is logged;
- if the selected backend cannot initialise, **startup fails loudly** — or
  the whole tier is disabled, consistently. There is no mixed state.

### D7 — Store-level API, not raw SQL

The aging service and cold exporter currently speak DuckDB SQL against
`warm_store.conn`. Under A they call store methods:

| Method | Replaces |
|---|---|
| `insert_batch(records)` | DuckDB `executemany` |
| `count()` | `SELECT COUNT(*) FROM conversations` |
| `fetch_older_than(cutoff, limit)` | `SELECT ... WHERE timestamp < ? ORDER BY timestamp LIMIT ?` |
| `delete_by_ids(ids)` | `DELETE ... WHERE conversation_id IN (...)` |

`insert_batch` uses asyncpg's `copy_records_to_table`. The Oneiric adapter's
current `insert` is **one round trip per document**
(`oneiric/adapters/vector/pgvector.py:_write_documents` loops `fetchrow`), so
a 1000-record aging batch would be 1000 round trips through pgbouncer.

______________________________________________________________________

## 6. Operational inventory

### Schema and indexes

- [ ] `halfvec` support in Oneiric's `PgvectorAdapter` (new).
- [ ] HNSW support ported from Mahavishnu's adapter (new).
- [ ] Optional real columns + timestamp/system_id indexes, or a table Akosha
  owns outright (D4).
- [ ] `CREATE EXTENSION IF NOT EXISTS vector` ownership.
- [ ] Drop the 25 orphaned `vectors_akosha_warm_shard_*` tables and the
  `vectors__probe_*` leftover in `akosha_pgvector`.

### Write path

- [ ] `insert_batch` via `copy_records_to_table`, not per-row `fetchrow`.
- [ ] `WarmRecord` carries float32 alongside the INT8 form, or gains a
  float32 sibling (D1).
- [ ] Remove the INT8→float widening in `PgvectorWarmStore.insert`.

### Read path

- [ ] `fetch_older_than` / `delete_by_ids` / `count` on the store (D7).
- [ ] `ColdExporter` and `AgingService` stop touching `.conn`.
- [ ] `PgvectorWarmStore` gains no `.conn` — the DuckDB-shaped surface goes
  away rather than being emulated.

### Connections

- [ ] One pool per process. `min_size=1`, `max_size` ≈ 5–10.
- [ ] pgbouncer `default_pool_size=20` covers both processes.
- [ ] `shard_count` becomes irrelevant to the warm tier; the 16-vs-256
  workaround is retired for this path.

### Configuration

- [ ] `resolve_warm_store_config()` must read the layered config, not bypass
  it. Today `~/.config/akosha/local.yaml`'s `warm:` block is loaded by
  Oneiric (verified) and then discarded twice: by
  `akosha/config.py:_strip_substrate_mapped` and by the resolver's own
  direct file read.
- [ ] Oneiric's `SubstrateSettings.warm__pg_url` default
  (`postgresql+psycopg://user:pass@localhost:5432/oneiric_substrate`)
  must become empty. A default DSN containing `user:pass` satisfies
  `if pg_url:` while pointing nowhere.
- [ ] `PgvectorSettings.dsn` is a plain `str`, not a `SecretStr` — the DSN
  is cleartext in the settings object.
- [ ] `ssl` default is `False`; a non-loopback DSN needs `sslmode=verify-full`
  (asyncpg honours `sslmode` in the DSN).
- [ ] `caller_namespace` / `cross_namespace_grant` stay default-deny and get
  documented.

### Data migration

- [ ] DuckDB warm → pgvector. Trivial today (the DuckDB warm store is
  effectively empty); expensive and lossy after aging has run.
- [ ] INT8 → float conversion can only carry the INT8 values forward.

### Observability

- [ ] A `warm_feed` health check. There is none today. `aging_feed` derives
  from the *hot* count, not the warm store.
- [ ] A warm-tier size gauge alongside `cold_store_size_bytes`.
- [ ] The effective backend is already logged; extend it to report the
  resolved dimension and the table name.

### Backup and restore

- [ ] `pg_dump` of the `akosha_pgvector` database, plus a restore runbook
  that recreates the extension and the indexes.

### Tests

- [ ] A warm-tier e2e: create table → `insert_batch` → search →
  `fetch_older_than` → `delete_by_ids` → close.
- [ ] The pgvector path has no file lock, so tests stop failing while the
  service runs. This is the known pre-existing failure
  (`Could not set lock on file .../hot/hot.db`, PID 72836).

### Cross-repo

- [ ] Oneiric release → version pin bump (operator).
- [ ] The DSN must reach the MCP launchd process (plist / `secrets.env`).

______________________________________________________________________

## 7. The embedding contract (prerequisite, not a warm-tier concern)

The warm table's dimension is fixed at DDL time, so the embedding contract
has to be settled before the table exists. This section was rewritten on
2026-11-17 after three independent reviews plus a fleet-wide survey. The
short version: **the fleet does not have an embedding contract, and the one
the substrate appears to declare is inert.**

### 7.1 The substrate declares a contract; almost nothing reads it

Oneiric's substrate declares five embedding knobs
(`oneiric/core/substrate_settings.py:198-230`). A fleet-wide grep for
readers, excluding the definition file and the tests:

| Field | Default | Production readers |
|---|---|---|
| `embedding__backend` | `"llama_server"` | **0** |
| `embedding__model` | `"nomic-embed-text"` | **1** — session-buddy's `search.embedding_model` default (`session_buddy/settings.py:334`) |
| `embedding__dim` | `384` | **0** |
| `embedding__normalize` | `True` | **0** |
| `warm__embedding_dim` | `384` | **0** |

Three further facts make the declaration actively misleading:

- **There are no validators.** The docstring at `substrate_settings.py:111-113`
  says a mismatch "triggers a hard substrate init error (REQ-WARM-DIM-001)".
  `grep -n "model_validator\|field_validator"` over the 354-line file returns
  nothing. The invariant at `:216-217` — "`embedding__dim` must equal
  `warm__embedding_dim`" — is documentation, not code.
- **The `Literal` omits the legs that actually run.**
  `embedding__backend: Literal["openai", "fastembed", "llama_server"]`
  (`:198`) does not include `minimax` or `model2vec`, which *are* legs of the
  live chain. The substrate's vocabulary and the runtime's vocabulary
  disagree in both directions.
- **Its own defaults are inconsistent.** `embedding__model` defaults to
  `nomic-embed-text` (768-dimensional natively) while `embedding__dim` and
  `warm__embedding_dim` both default to `384`.

The earlier draft of this document said the standardisation would be "a
config change to existing knobs". **That was wrong.** It is a *wiring*
change: the knobs exist, nothing reads them, and the value that is read
(`embedding__model`) does not reach the code that produces vectors.

### 7.2 There is no fleet width constant — there are four widths

The survey plan `session-buddy/docs/plans/2026-11-06-fleet-embedding-width-768.md`
states that the source of truth "should be" a module-level
`FLEET_EMBEDDING_DIM` in `oneiric/adapters/observability/embeddings.py`.
**That constant does not exist.** Each repo declares its own:

| Width | Where |
|---|---|
| **768** | `session_buddy/memory/embedding_dim.py:39`, `session_buddy/reflection/embeddings.py:81` |
| **384** | `akosha/processing/embedding_dim.py:63` (`DEFAULT_DIMENSION`), `oneiric/adapters/observability/settings.py:36`, `oneiric/adapters/vector/duckdb_hot_store.py:343`, `mahavishnu/core/repositories/embeddings.py:79`, `crackerjack/models/semantic_models.py:171` |
| **1536** | `oneiric/adapters/vector/vector_types.py:37` (`VectorBaseSettings.default_dimension`) |
| **1024** | the *upper bound* on six Pydantic fields (`le=1024`) — oneiric ×3, mahavishnu ×2, crackerjack ×1 |

A fourth width is hardcoded in a comparison:
`oneiric/adapters/observability/queries.py:55` — `if embedding.shape != (384):`
— which already rejects the 768 vectors Session-Buddy produces.

### 7.3 The chain is hardcoded, ends in a mock, and cannot speak to Bifrost

`oneiric/adapters/observability/embeddings.py:110-112` probes:

```
llama_cpp → minimax → model2vec → mock
```

- **There is no `openai` leg**, even though `oneiric/adapters/embedding/openai.py`
  ships full `dimensions` support and is exercised only by Oneiric's own
  tests. No fleet component uses it.
- **The `llama_cpp` leg cannot authenticate.** `_encode_llama_cpp`
  (`:391-403`) POSTs `{"model": ..., "input": texts}` with **no headers and
  no `dimensions`**. Pointing `llama_cpp_base_url` at Bifrost therefore
  returns `401 virtual_key_required`, the probe returns `None`
  (`:277-299`), and the service silently selects the **mock** backend.
  A system full of meaningless vectors, with no error anywhere.
- **The mock is 384** (`embedding_settings.py:67`) — *exactly* the width of
  Akosha's stale `INT8[384]` warm column. The mock's width is why the
  dimension defect was invisible while a local provider was down, and why it
  appeared the moment a real provider came up.

### 7.4 Akosha's hot-store schema width depends on the health of an unrelated service

`akosha/processing/embedding_dim.py:63-67` resolves the width as:

```python
BACKEND_DEFAULT_DIMS = {"mock": 384, "model2vec": 256, "llama_cpp": 768, "minimax": 1024}
```

falling back to `DEFAULT_DIMENSION = 384`. That value is interpolated into
`embedding FLOAT[{self._embedding_dim}]`
(`oneiric/adapters/vector/duckdb_hot_store.py:703`) under
`CREATE TABLE IF NOT EXISTS`.

**So the width of a fixed-size DuckDB column is a function of which provider
happened to be reachable at the moment the file was first created.** A later
boot with a different provider finds the column unchanged — DuckDB never
alters it — and every insert then raises
`ValueError: DuckdbHotStore.insert: embedding dim mismatch (expected 384, got 768)`
(`duckdb_hot_store.py:1061-1072`).

**The mitigation is that the scope is small.** Only the `conversations`
table carries an embedding; `spans_substrate` (~362k rows) and
`metrics_substrate` (~3.1M rows) have no embedding column at all, and
`conversations` currently holds **0 rows**. Rebuilding it at a new width is
close to free.

### 7.5 Width is not identity

A dimension check cannot tell you whether two vectors are comparable. The
live Session-Buddy chain proves it:

| Leg | Model | Width | Eligible at 768 |
|---|---|---|---|
| `openai_gateway` | text-embedding-3-small (`dimensions=768`) | 768 | ✅ |
| `openai_direct` | text-embedding-3-small (`dimensions=768`) | 768 | ✅ |
| `llama_embed` | **EmbeddingGemma-300M** (native) | 768 | ✅ |
| `llama_server` | Qwen3-8B (chat model) | 4096 | ❌ width mismatch |

Verified live via `session_buddy.reflection.embeddings.get_embedding_system_info()`.
The `llama_embed` leg is a **different model in a different vector space**
serving the **same width**, and it sits *below* the OpenAI legs in the
chain. If Bifrost is unreachable and no direct OpenAI key is present, the
chain falls to EmbeddingGemma — and every width check passes while the
vectors become mutually incomparable. `text-embedding-3-small` and
`text-embedding-3-large` have a mutual dot product of ≈0.01 for the same
text: different models are never interchangeable, even at equal width.

**The same trap exists inside one model.** `dimensions=768` selects a
*different point on the sphere* than native 1536 — Matryoshka nesting means
the 768-vector is the leading coordinates of the 1536-vector, renormalised.
Vectors produced at different `dimensions` values do not belong in one
index.

**Therefore the contract is `(model, dimensions, space_version)`, not a
dimension.** Every vector store must carry all three, and a write must be
refused on a mismatch of any of them.

### 7.6 The dimension options

OpenAI's own MTEB numbers, from the 25 Jan 2024 launch post (the table is
client-rendered on the live page; the values below are from the 2 Feb 2024
server-rendered capture and are corroborated by the live docs for the
native-dimension column):

| Model | Embedding size | Average MTEB | Cost / 1M tokens |
|---|---|---|---|
| ada-002 | 1536 | 61.0 | $0.10 |
| **text-embedding-3-small** | **512** | **61.6** | **$0.02** |
| **text-embedding-3-small** | **1536** | **62.3** | **$0.02** |
| text-embedding-3-large | 256 | 62.0 | $0.13 |
| text-embedding-3-large | 1024 | 64.1 | $0.13 |
| text-embedding-3-large | 3072 | 64.6 | $0.13 |

**OpenAI has published no MTEB for 768 or for 384 on any `text-embedding-3`
model.** The measured granularities are 512 and 1536 for 3-small, and 256,
1024 and 3072 for 3-large. The "trained granularities" list is community
inference (Supabase), not an OpenAI statement; the API schema itself accepts
any integer ≥ 1 and OpenAI's own example requests `dimensions: 3`.

The options for a fleet standard:

| Option | Model | Dim | MTEB | Storage (fp32 / halfvec) | Local fallback | Assessment |
|---|---|---|---|---|---|---|
| **A** | 3-small | **1536** | **62.3** (measured) | 6 KB / 3 KB | ❌ none eligible | **CHOSEN (2026-11-17)** — native, no truncation, measured |
| B | 3-small | 768 | ~61.9–62.1 (*interpolated, not measured*) | 3 KB / 1.5 KB | ✅ EmbeddingGemma | rejected — unmeasured point; the width the existing fleet plan picked |
| C | 3-large | 1024 | **64.1** (measured) | 4 KB / 2 KB | ❌ | rejected for now — best quality-per-byte OpenAI has published; +1.8 over A |
| D | 3-small | 512 | 61.6 (measured) | 2 KB / 1 KB | ❌ | rejected — only 0.6 above ada-002, and throws away most of the reason to prefer 3-small |

**Option A is decided (operator, 2026-11-17), and the reason 768 was
originally chosen no longer holds.** The fleet-768 plan justifies its width
by the local tier: nomic-embed-text, bge-base-en-v1.5 and
EmbeddingGemma-300M are all natively 768. The operator has since ruled that
the fleet does not depend on the local llama-server ("due mostly to hardware
limitations, we are not depending on it"). With the local tier retired, 768
is an *unmeasured truncation* kept alive by a constraint that no longer
exists, and 1536 is the model's native width.

Additional reasons for A:

- **It removes a parameter.** At native width the fleet never sends
  `dimensions`, so no component can drift into requesting a different
  truncation of the same model — the §7.5 trap, closed at the source.
- **It makes the chain honest.** At 1536 no local model is width-eligible,
  so the chain either reaches a real OpenAI endpoint or **fails loudly**.
  That is exactly the operator's stated position.
- **The storage cost is negligible for this fleet.** halfvec(1536) is 3 KB
  per row. Akosha's vector-bearing table is empty; Session-Buddy holds 313
  vectors in a 53 MB database.

**Is there really an advantage to 1024?** The advantage is real but small,
and it is not the binding constraint today.

| | 3-small @ 1536 | 3-large @ 1024 |
|---|---|---|
| MTEB | 62.3 | **64.1** (+1.8) |
| Storage, halfvec | 3 KB/row | 2 KB/row |
| Token cost | $0.02/1M | $0.13/1M ($1.30 at 10M tokens/month) |
| Truncation | none | none — 1024 is a measured granularity of 3-large |
| Model in the fleet | already 3-small | a **different** model |

The +1.8 MTEB is a genuine difference, and the storage saving is real. But
neither is what is currently costing the fleet anything: Akosha's
vector-bearing table holds **0 rows**, Session-Buddy holds **313 vectors in
a 53 MB database**, and the warm tier has **no input at all**. The binding
constraint is that retrieval has nothing to retrieve, not that it retrieves
at 62.3 instead of 64.1. Choosing a different model to buy a quality
margin on a corpus that does not exist yet inverts the priority.

**The migration is cheap and reversible in the way that matters.** The
contract in §7.5 carries `(model, dimensions, space_version)`, so moving to
3-large @ 1024 later is a re-embed plus a column rebuild — not a redesign.
It should be driven by a measured evaluation on the fleet's own corpus
(a small labelled query set with Recall@k), not by an MTEB average, which
hides the code-identifier and exact-term failures that matter for a
code-search fleet.

**What the dimension choice does *not* fix.** Six Pydantic fields cap the
dimension at 1024 and would reject 1536 *at construction time*
(`oneiric/core/substrate_settings.py:132,141,230`,
`oneiric/adapters/observability/settings.py:36`,
`mahavishnu/core/config.py:1799,3060`,
`mahavishnu/core/repositories/embeddings.py:79`,
`crackerjack/models/semantic_models.py:171` — all `le=1024`), and
`oneiric/adapters/observability/queries.py:55` hardcodes a `(384,)` shape
gate. Every one of those bounds is part of the work.

### 7.7 The contract, stated

```python
# oneiric/adapters/observability/embeddings.py
FLEET_EMBEDDING_MODEL   = "text-embedding-3-small"
FLEET_EMBEDDING_DIM     = 1536
FLEET_EMBEDDING_SPACE   = "openai/text-embedding-3-small/v1"   # any change is a new space
FLEET_EMBEDDING_SOURCE  = "bifrost"                            # then direct OpenAI
```

**The chain.**

| # | Leg | Endpoint | Auth | Width | Behaviour |
|---|---|---|---|---|---|
| 1 | `openai_gateway` | Bifrost `{base}/embeddings` | virtual key (`x-bf-vk`) | 1536 | primary |
| 2 | `openai_direct` | `https://api.openai.com/v1/embeddings` | `OPENAI_API_KEY` | 1536 | failover |
| 3 | — | — | — | — | **no mock, no local model** |

Legs 1 and 2 serve the *same model*, so failover between them is a
transparent swap — same vector space, no re-embedding. That is the only
kind of fallback that is valid. A local model at any width is a **tier
fallback**, not a provider failover: it belongs in a separate collection
with the tier encoded in the name, never mixed into the same index.

**The mock is removed from any tier that holds semantic vectors** and
becomes a **startup failure** (see O11 in §7b). The mock is what hid the
original dimension defect; it must not be able to hide the next one.

**Oneiric's existing `OpenAIEmbeddingAdapter`** (`oneiric/adapters/embedding/openai.py`)
already handles `dimensions`, credential resolution and batching. The chain
leg should **reuse** it rather than add a parallel implementation — this is
the fleet-768 plan's central design decision, and it is correct.

### 7.8 The Bifrost side, verified

Bifrost is running on `127.0.0.1:8471` (launchd `ai.bifrost.gateway`,
`BIFROST_APP_DIR=/Users/les/.config/bifrost`). Findings:

- **The `openai` provider's key allowlist already contains
  `text-embedding-3-small`** (`config/bifrost/config.template.json`), so 3-small
  is reachable through the gateway with no key-model change. 3-large would
  need to be added to that allowlist.
- **The gateway requires a virtual key** (`x-bf-vk`); a provider key is a
  hard `401 virtual_key_required`. The virtual key is a *grant* over
  already-configured provider keys — it does not carry the OpenAI credential.
- **Bifrost schema-parses the embeddings body before authenticating.** A
  valid `dimensions` integer passes validation and reaches routing
  (`request_type: "embedding"`, `provider: "openai"`); an invalid type is
  rejected with `400`. The `dimensions` parameter is typed as an integer.
- **The `llama-server` provider allows only `chat_completion` and
  `chat_completion_stream`** — no embeddings request type. It cannot serve
  embeddings through the gateway even if it wanted to.
- **The gateway's own semantic-cache plugin is already configured for
  `text-embedding-3-small` at `dimension: 1536`** — the native width, and
  the same width this design selects. The gateway's internals already agree
  with the recommendation; the fleet's own components do not.

Two Bifrost issues worth carrying into the plan: a wrong virtual key on an
embeddings request returns a misleading `400 "could not auto resolve a provider"` rather than a 401, and the semantic-cache plugin cannot resolve
local/custom embedding providers inside its `PreLLMHook` even when the same
provider/model succeeds through the normal `/v1/embeddings` path.

**The chain is live and the fleet standard is reachable.** Verified against
the running gateway on 2026-11-17, after the operator confirmed the OpenAI
credits had been restored:

```
bifrost virtual key present : True
direct OpenAI key present   : True
Session-Buddy chain         -> active_leg = "openai_gateway"
                               len = 768 (today's canonical width)
                               L2 norm = 0.999904
                               cosine(probe, unrelated) = 0.154
```

and directly against the gateway's embeddings endpoint:

```
POST {bifrost}/embeddings {"model":"text-embedding-3-small","input":[...]}
  -> HTTP 200, len = 1536, L2 norm = 0.999898     (native width, no `dimensions`)

POST {bifrost}/embeddings {..., "dimensions": 1536}
  -> HTTP 200, len = 1536
```

**So `text-embedding-3-small` at native 1536 is reachable through Bifrost
today, with no `dimensions` parameter.** The credit exhaustion that gated
this work is gone; what remains is the plumbing, not the provider.

______________________________________________________________________

### 7.9 The model landscape at 1536

Choosing 1536 is not a bet that only OpenAI can reach it. Several other
providers and one open-weights model land on exactly 1536, which means the
fleet standard is not hostage to a single vendor — and, importantly, that a
*local* tier at the same width is possible even though the local tier was
retired from the standard chain.

**OpenAI-API providers that serve 1536**

| Provider | Model | How it reaches 1536 | Price / 1M tok | API shape |
|---|---|---|---|---|
| OpenAI | `text-embedding-3-small` | **native** | $0.02 | OpenAI |
| **MiniMax** | **`embo-01`** | **native** | ~$0.069 (¥0.5) | **not OpenAI** — `{"texts":[…],"type":"db"\|"query"}`, `type` mandatory |
| **Perplexity** | **`pplx-embed-v1-4b`** | `dimensions: 1536` (range 128–2560) | **$0.03** | OpenAI |
| Mistral | `codestral-embed` | default 1536 (up to 3072) | $0.15 | OpenAI |
| Cohere | `embed-v4.0` | default 1536 (MRL 256/512/1024/1536) | $0.12 | Cohere-shaped |
| Google | `gemini-embedding-001` | MRL to 1536 (native 3072) | ~$0.025 | Google-shaped |
| Voyage | `voyage-3-large`, `voyage-3.5`, `voyage-4*` | — | — | 1536 is **not** offered on any current model; only the deprecated `voyage-large-2` / `voyage-code-2` |

Two of these are directly useful to this fleet:

- **Perplexity `pplx-embed-v1-4b` is OpenAI-compatible at 1536 for $0.03/1M**
  — a drop-in third leg behind Bifrost and direct OpenAI, at a price point
  that is still negligible. It is *not* the same model, so it is a **tier
  fallback** (its own collection), never a provider failover.
  `pplx-embed-v1-0.6b` cannot reach 1536 — it caps at 1024.
- **MiniMax `embo-01` is natively 1536** — and `minimax` is *already a leg*
  of Oneiric's chain. **The fleet's `BACKEND_DEFAULT_DIMS` entry says
  `minimax: 1024`, which is wrong**: MiniMax's own documentation gives
  `embo-01` a 1536-wide output. That is one more reason to delete the
  table rather than extend it. Note also that `embo-01` is documented on
  MiniMax's **China** platform; the international pricing page lists only
  M-series LLMs, so international availability must be confirmed before
  `embo-01` is used as anything other than an offline tier.

**Open-weights models that are natively 1536**

| Model | Native | MRL? | GGUF? | Notes |
|---|---|---|---|---|
| **`Alibaba-NLP/gte-Qwen2-1.5B-instruct`** | **1536** | **not documented** | **yes** — several community conversions | Apache-2.0, 32K ctx, `--pooling last`, MTEB 67.16 EN |
| `openai/text-embedding-3-small` | 1536 | yes | no | API only |
| `voyage-large-2`, `voyage-code-2` | 1536 | n/a | no | deprecated |

`gte-Qwen2-1.5B-instruct` is **the only open-weights model natively 1536
with a llama.cpp-servable GGUF**. Two caveats before anyone adopts it:
every GGUF is community-made rather than Alibaba-published, and llama.cpp
issue #14234 reports ~20% retrieval-quality loss on the Qwen3-Embedding GGUF
path — so a known-answer cosine test against the HuggingFace reference is
mandatory, and it must be re-run after any llama.cpp upgrade, because
quantisation drift and pooling drift are both silent.

**A fact that settles the width question for any local tier: llama.cpp does
not support the `dimensions` parameter at all.** Issue #25210 is open; the
implementing PR #24898 (`--embd-dimensions`) was closed unmerged. A
`dimensions: 128` request to a 768-dim model returns 768 floats. Any
truncation against a llama.cpp server must be done **client-side** (slice +
renormalise), which is exactly the kind of client-side arithmetic that
produces the "same width, different space" corruption §7.5 describes.

**So the landscape is:**

- **Cloud, same model, provider failover:** OpenAI via Bifrost → OpenAI
  direct. Same vector space, interchangeable. This is the only valid
  failover.
- **Cloud, same width, different model:** Perplexity `pplx-embed-v1-4b`,
  MiniMax `embo-01`, Mistral `codestral-embed`, Cohere `embed-v4.0`. A
  **tier**, with its own collection and its own space id.
- **Local, same width, different model:** `gte-Qwen2-1.5B-instruct` at
  native 1536. A **tier** — and unlike the retired EmbeddingGemma leg, it
  is at least the same *width*, so a width check cannot be fooled by it
  confusing dimensions; the space check is what protects it.

No model is simultaneously open-weights, natively 1536, MRL-trained, and
available on a mainstream API. The fleet standard therefore has to be a
**model**, not a dimension — which is why §7.7's contract carries
`(model, dimensions, space_version)` and not a width.

______________________________________________________________________

## 7a. Builder-check corrections

A builder check against the design and both plans surfaced eight technical
corrections. All eight are verified against the code; they change the task
lists, not the design's shape.

**C1 — The DuckDB warm store has a second dimension freeze.**
`akosha/storage/warm_store.py:77` hardcodes `embedding INT8[384]` in the
DDL, exactly like the hot store's `FLOAT[N]`. D1's table assumed
`INT8[768]` for the DuckDB backend but no task widens it. With a 768-dim
backend, the DuckDB warm store's `insert_batch` fails on array length even
after the float32 carrier change — which makes "fall back to the DuckDB
backend" an incoherent rollback. **Either the DuckDB warm store is widened
to the fleet dim, or it is retired and the pgvector backend is the only
one.** This is a decision, not a task.

**C2 — There are two `WarmRecord` classes.**
`akosha/models/__init__.py:34` (what `aging.py` imports) and
`akosha/storage/models.py:43` (what `akosha/storage/__init__.py`
re-exports). Both declare `embedding: list[int]` INT8[384]; they differ in
`metadata` and in the extra fields. D1 calls this "the one model change
this design requires" — it is two, and the split has to be resolved before
the carrier can change.

**C3 — The Oneiric adapter cannot express D4's schema.**
`create_collection` hardcodes `embedding vector({dimension})` and an
`ivfflat` index (`oneiric/adapters/vector/pgvector.py:263-278`), and
`_write_documents` hardcodes `$2::vector` (`:513`). It has no notion of
`halfvec`, typed columns, or `copy_records_to_table`. Two further wrinkles:

- Collection names get the `collection_prefix` (default `"vectors_"`,
  `:39`, `:462-465`), so a collection named `akosha_warm` becomes
  `public.vectors_akosha_warm`. The final table name — and therefore the
  orphaned-table cleanup list — depends on this.
- The only filter mechanism is `metadata @> $2::jsonb` (`:149`). A real
  indexed `system_id` column needs a **column-predicate API** the adapter
  does not have. Either the store duplicates `system_id` into JSONB (which
  defeats D2/D4's rationale) or the Oneiric port grows a new API.

So "port into Oneiric" is an **API design**, not three bounded changes.
That is the largest structural fork in the plan and it needs answering
before either plan's phase structure is final.

**C4 — Making the resolver read the layered config is a behaviour flip.**
Today `AkoshaConfig.warm.backend` reads the substrate (`"pgvector"`) while
`resolve_warm_store_config()["backend"]` reads the repo YAML
(`"duckdb-ssd"`). T5.1 makes them agree — and the XDG overlay says
`pgvector`. Against today's `PgvectorWarmStore`, which has neither
`insert_batch` nor `.conn`, that **breaks aging outright the moment it
lands**. T5.1 must not land before the unified store exists; this is a hard
ordering constraint, not a preference.

**C5 — The Redis backlog needs `XAUTOCLAIM`, not a restart.**
`XREADGROUP ... >` returns only entries never delivered to any consumer.
The group's lag is 0 and the 659 pending entries live in its PEL, attached
to the leaked per-uuid consumer names. A restart reads only *new* messages.
Replaying the 659 requires `XAUTOCLAIM`/`XCLAIM` or reading the consumer's
own PEL with id `0` — the code has neither. Separately: `XGROUP DELCONSUMER` per consumer preserves the group's position, while `XGROUP DESTROY` + recreate resets it and re-delivers the whole stream. The choice
decides whether the drain is 659 rows or 13,281.

**C6 — Rebuilding the hot store is a data operation, and the file holds
more than `conversations`.**
`hot.db` also carries `spans_substrate`, `metrics_substrate`,
`_akosha_otel_receiver_state` (the OTLP receiver's resume watermark) and
`_dlq_rows` (`oneiric/adapters/vector/duckdb_hot_store.py:78,181,313,324`),
plus Akosha's own `code_graphs` (`akosha/storage/hot_store.py:348-369`).
The DDL uses `CREATE TABLE IF NOT EXISTS` with the dimension interpolated at
construction (`:699`, `:619-620`), so a process booting at 768 against an
existing file does **not** change the column — the insert fails with an
opaque DuckDB array-length conversion error instead of the friendly
`ValueError`. Deleting the file loses the receiver watermark and re-triggers
a full span re-ingest. The rebuild needs a named procedure: delete the
file, `CREATE TABLE ... AS SELECT` into a new table, or a column-type
migration.

**C7 — The store-level API's semantics are not yet specified, and the two
backends will diverge.** Four open contracts:

- **Timezone.** The DuckDB warm column is a naive `TIMESTAMP`; both
  `AgingService` and `ColdExporter` deliberately normalise to naive UTC,
  with an explicit comment about the 8-hour Pacific-time error from binding
  an aware value (`akosha/storage/aging.py:74-82`;
  `akosha/storage/cold_exporter.py:65-82`). The proposed pgvector column is
  `TIMESTAMPTZ`. `fetch_older_than` must state one contract or the cold
  exporter's 90-day boundary moves by the session's local offset.
- **Duplicates.** `copy_records_to_table` has no `ON CONFLICT`, so one
  repeated `conversation_id` fails the entire COPY, where the DuckDB
  `executemany` path fails per row. Re-running aging over an already-aged
  row changes from partial to total loss.
- **Count scope.** `AgingService.get_migration_stats` returns
  `{"hot_records", "warm_records"}` from two stores. A protocol method
  named `count()` that means "rows in this store" has to be reconciled with
  that.
- **The carrier.** C2.

**C8 — The failure contract is stated three different ways.** D6 and the
anti-goals say *fail loud, never degrade to a different backend*. Plan A's
T4.4 says *the tier is disabled as a whole*. Plan A's Phase 3 Integration
Contract says *fall back to the DuckDB backend for the whole tier*. Per C1
the DuckDB fallback is not viable at 768 anyway. One behaviour has to be
chosen, and it decides whether `create_warm_store`'s existing fail-soft
(`akosha/storage/__init__.py:284-298`) survives, dies, or becomes a
"disable the tier" path.

______________________________________________________________________

## 7b. Ops-review corrections

An operational review of both plans took read-only measurements of the live
machine and found thirteen more defects. All are verified.

**O1 — The adapter's pool is 10× bigger than documented.**
`PgvectorAdapter._connection_kwargs` returns **only** `{"dsn": ...}` when a
DSN is set (`oneiric/adapters/vector/pgvector.py:351-368`), so asyncpg's
defaults apply — `min_size=10, max_size=10` — not the `min_size=1` the
`PgvectorWarmStore` docstring claims. 256 shards × 10 = **2,560
connections** against pgbouncer's `default_pool_size=20`.
`statement_timeout_ms` is also silently ignored on the DSN path. This is
the real version of the connection bomb, and it is worse than the design
assumed.

**O2 — Changing the index type is a silent no-op.**
`create_collection` uses `CREATE INDEX IF NOT EXISTS {table_name}_embedding_idx`
(`pgvector.py:270-279`). The IVFFlat and HNSW DDL share that name, so
switching the method makes the new statement a no-op and **the old IVFFlat
index survives**. The adapter logs "hnsw"; the planner uses ivfflat.
Visible only in `pg_indexes`. The port must drop the index explicitly or
use a distinct name.

**O3 — `hot.db` is 894 MB and holds more than `conversations`.**
It carries `spans_substrate` (the OTLP receiver's 14.5k batches),
`metrics_substrate`, `_akosha_otel_receiver_state` (the receiver's resume
watermark), `_dlq_rows`, and Akosha's `code_graphs` (24 repos). **A naive
`rm hot.db` destroys the OTel span history and the code-graph index** —
neither of which is in either plan's scope. `hot.db` and `hot.db.wal` must
be copied **together** (or `EXPORT DATABASE`), and the copy must be
verified open before the original is touched. Deleting the db but leaving
the WAL yields "WAL file exists but the database file is missing" and a
restart storm.

**O4 — `CREATE TABLE IF NOT EXISTS` never alters.** Editing the DuckDB DDL
alone does nothing to an existing `hot.db` or `warm.db`. A "rebuild" that
forgets to delete the file is a silent no-op, and the dimension error
returns verbatim — reading like "the rebuild didn't work" rather than "the
rebuild produced the wrong schema".

**O5 — `/health` returns HTTP 200 with four empty feeds.**
`http_ok = worst_severity < 2` and `warming_up` is severity 1
(`akosha/mcp/server.py:192-207`). **Every gate in either plan must assert
`entities_count`, never the HTTP status.** A gate written as "the service
is healthy" passes on a completely empty system.

**O6 — The split-brain warm store is live today.**
`AkoshaApplication.warm_store` (`akosha/main.py:240-266`) and the MCP
server's `aging_warm_store` (`akosha/mcp/server.py:1833`) are two objects
built from two config paths. A pgvector warm tier can be fully live, fully
green, and **never receive a single record**, because the aging service
writes to a DuckDB file on a different path while a `warm_feed` reads the
pgvector table. The gates must assert **store identity** — log and assert
`warm_store.backend` and `warm_store.table` at startup, and carry the same
identifiers in every aging-cycle log line.

**O7 — `aging_feed.entities_count` counts rows still in HOT.**
(`akosha/mcp/server.py:793-821`) It goes **down** as aging works and can
never report the warm tier's size. `cold_store_size_bytes` is the only
warm-adjacent gauge and it measures the cold tier. A `warm_feed` is
required, not optional.

**O8 — the DSN is already on the machine and already discarded.**
`~/.config/akosha/local.yaml` already carries
`warm: {backend: pgvector, pg_url: postgresql://…/akosha_pgvector}`. It
has no effect because it is stripped twice (Section 5, D5). The plist sets
only `AKOSHA_OTLP_ENDPOINT`, `AKOSHA_TOOL_PROFILE`, `MCP_PORT` and `PATH` —
**it does not source `~/.config/secrets.env`**, and neither does
`launch_with_healthcheck.sh`. A DSN added to `secrets.env` today reaches
nothing.

**O9 — `XGROUP DELCONSUMER` orphans pending entries invisibly.**
It does not return the PEL entries to the group; it strands them under a
deleted consumer, and `XPENDING` then reads **0** — so the loss looks like
a clean drain. The order must be: fix the leak, **XAUTOCLAIM the 659
pending entries first**, then delete the dead consumers in pages. Do not
`XGROUP DESTROY` — that loses the pending entries and the group's position
outright.

**O10 — the subscriber's insert path costs three statements per message.**
The record insert, `_update_watermark`'s `DELETE FROM conversations WHERE conversation_id = '__bodai_subscriber_watermark__'`, then a second insert
of the zero-vector watermark row. All serialized on the MCP server's event
loop, each autocommitting to the WAL, and the watermark churn runs on
**every** message, defeating batching. There is no batch path: the
subscriber calls `generate_embedding` once per message, serially. 13,281
messages ≈ 39,843 serialized DuckDB statements, delivered in 1–3 ms slices
that each look individually fine while the event loop is blocked.

**O11 — the mock must never reach the hot store.**
The fallback is deterministic, local, and has no rate limit, so it absorbs
a backlog at full speed and fills the tier with noise. The moment bifrost
gets credits, the identical code path becomes a 13,281-request burst
against a metered API. The mock must be a **startup** failure, not a
per-request fallback, whenever the tier is meant to hold semantic vectors.

**O12 — never pad a 384-dim vector to 768.** Zero-padding contributes 384
zero components that dilute cosine by `sqrt(384/768) ≈ 0.707` — a 29%
systematic score deflation that would silently break every absolute
similarity threshold ever set. Re-embed from source text instead.

**O13 — Plan B's Phase 4 retention sweep is a trap.**
It is framed as "keep the window long enough that nothing is swept" or
"accept the loss knowingly". The first means unbounded growth; the second
means destroying exactly the float32 originals the plan exists to preserve.
Bound the hot tier by **size, not age** — the risk is volume (the OTel
span rate is ~17k rows/day), and a size cap bounds disk without touching
the newest rows.

______________________________________________________________________

## 7c. Adversarial-review corrections

A hostile audit attacked the design's load-bearing claims directly. Six
corrections survive verification.

**A1 — Porting Mahavishnu's adapter over Oneiric's would DROP the
namespace ACL.** Oneiric's `PgvectorAdapter` enforces
`caller_namespace` / `cross_namespace_grant` (default-deny —
`oneiric/adapters/vector/pgvector.py:48-62`, enforced at `:386-439`).
Mahavishnu's adapter has **zero** occurrences of either field (verified).
Mahavishnu's adapter also lacks write batching — its `insert` has the same
per-document `fetchrow` loop. So D4's own security sentence depends on
Oneiric's adapter, and the port must be *into* Oneiric, never *over* it.

**A2 — `ef_search` has never been applied to a query.**
Mahavishnu's `ef_search=40` (`mahavishnu/adapters/pgvector_adapter.py:59`)
is defined but never read. The `SET LOCAL hnsw.ef_search = $1` path
(`:236-238`) is broken twice over: PostgreSQL's `SET` does not accept bind
parameters, and the `SET` runs on one pooled connection while the query
runs on another (`:259-260`). The design cited 40 as a "validated"
default; it is a dead field. The substrate's
`warm__hnsw_ef_search=64` is the only value with any claim to being
intended.

**A3 — `ivfflat_lists` is a setting, not a hardcoded value.**
`ivfflat_lists: int = Field(default=100, ge=1)`
(`oneiric/adapters/vector/pgvector.py:43-47`). What is hardcoded is the
index *method* — the adapter creates **only** ivfflat; there is no `hnsw`
string in the file. The design's "hardcodes `lists=100`" overstated.

**A4 — the read side of the dimension mismatch is broken too.**
`akosha.err` carries `search_all_systems: hot_store query failed: HotStore.search_similar: query dim mismatch (expected 384, got 768)`. The
plans discuss the hot store as a write-side schema change and never
mention that the search path fails on the same stale dimension.

**A5 — the running system is not running the committed configuration.**
The committed plist sets `AKOSHA_OTLP_ENDPOINT = http://127.0.0.1:4318/v1/traces`;
the running poller uses `http://127.0.0.1:14318/v1/traces`. The running
PID is the one this design cites, so its "all verified against the running
system" claim was verified against a system whose effective config no
longer matches the repo's committed plist. The source of the override was
not traced.

**A6 — `PgvectorWarmStore.search_similar` has no caller anywhere.** The
only calls to `warm_store.insert` / `insert_batch` anywhere in the repo
are inside `AgingService` (`akosha/storage/aging.py:125`, `:238`).
`warm_store.search_similar` has none. The pgvector warm store's search
path is dead code — which is consistent with the design's own "nothing
reads the warm tier", and with D7's `fetch_older_than` being required
only by the cold exporter.

**Two consistency corrections.** D1's table header says "Bytes/row
(D=768)" while its measurement note says "384-dim vectors" — the table
mixes dimensions; the measurement was run at 384 and is reported per
dimension. And the design's §1 "Nothing reads the warm tier" contradicts
D7, which lists `fetch_older_than` as replacing the cold exporter's
`SELECT … WHERE timestamp < ?` (`akosha/storage/cold_exporter.py:373-381`).
The cold exporter is a reader *and* a deleter.

**On the pgvector version.** The **live server** reports
`extversion = 0.8.6` for the `vector` extension (queried from the running
instance), and the live catalog exposes `halfvec_cosine_ops` for HNSW
alongside `vector_cosine_ops`, `bit_hamming_ops` and
`sparsevec_cosine_ops`. The Homebrew extension *files* on disk report
different default versions in different paths, so the live server's
reported version is the one that governs.

______________________________________________________________________

## 7d. Was the current arrangement optimal for the tasks?

**No — and the reason is structural, not accidental.** Each component was
individually reasonable and the composition was never sound. The evidence:

### 7d.1 The hot store's schema width depends on the health of an unrelated service

`CREATE TABLE IF NOT EXISTS conversations (embedding FLOAT[N])` with `N`
resolved from whichever provider happened to be reachable at the moment the
file was first created (§7.4). A fixed-size column whose width is a function
of runtime network state is not a design; it is a lottery. The
fail-loud `ValueError` on a later mismatch is the *good* outcome — the bad
outcome is a boot that silently succeeds with the wrong width.

### 7d.2 The fallback that hid the defect was also the wrong width

The mock's 384 dimensions exactly matched Akosha's stale `INT8[384]` warm
column. So the system was **self-consistent while broken**: with no provider
reachable, every component agreed on 384, and the aging path failed for a
*different* reason (the `ConversionException` on a 768-wide vector) that
looked like a schema bug rather than a provider-health bug.

The mock was never a safe fallback. It was a **width-compatible lie** that
made every downstream width check pass while the vectors were meaningless.

### 7d.3 The chain cannot reach the provider the fleet has standardised on

Oneiric's chain has no `openai` leg; the `llama_cpp` leg sends no auth
header and no `dimensions`; pointing it at Bifrost returns 401 and falls
through to the mock (§7.3). The fleet's chosen provider — OpenAI via
Bifrost — is **unreachable through the only embedding client in the fleet**,
and the failure is silent.

### 7d.4 The width check was necessary but not sufficient

A chain that checks only width admits a different model at the same width
(§7.5). The live Session-Buddy chain does exactly that today. Every
dimension assertion in the fleet would pass while the vectors from two
different spaces share a table.

### 7d.5 The declared contract was inert, and its own defaults were wrong

Five substrate knobs, one production reader, no validators, a `Literal` that
omits the legs that actually run, and a model default (nomic-embed-text,
768\) that contradicts its dimension default (384) (§7.1). A contract that
resolves but is never read is worse than no contract, because it looks like
one in every review.

### 7d.6 There were four widths, one of them hardcoded in a comparison

768, 384, 1536 and a 1024 ceiling, plus `if embedding.shape != (384,)` in
the query path (§7.2). Nothing reconciled them.

### 7d.7 Two independent pgvector implementations, neither a subclass of the other

Mahavishnu's 716-line `PgvectorAdapter` claims in its own docstring to
"extend Oneiric's PgvectorAdapter to support HNSW". It does not — it is a
standalone class with **zero** `oneiric` imports (`pgvector_adapter.py:143`,
imports at `:17-32`). Meanwhile Oneiric's adapter has **no HNSW at all**
(IVFFlat only, `:271-278`), and Mahavishnu's own design and decision
documents assert the opposite ("Oneiric's class supports HNSW per the
verified source" — `docs/specs/2026-10-14-decision-adapter-category-design.md:596-598`).

**Deleting the fork on that stated rationale would silently drop HNSW
indexing from Mahavishnu's OTel path.** That claim is false and must be
corrected before anyone acts on the existing plan.

### 7d.8 The aging path has no source table

This is the sharpest finding, and it is a configuration fact rather than a
code defect:

| Table | Written by | Rows | Read by |
|---|---|---|---|
| `spans_substrate` | the OTLP receiver on `:4318` (alive) | ~362,644 | the health aggregator |
| `metrics_substrate` | the same receiver | ~3,100,000 | the metrics feeds |
| **`conversations`** | **nothing** | **0** | **`AgingService`** |

**The two tables are separate by design, and the separation is correct.** The
DDL makes the intent unambiguous:

| | `spans_substrate` | `conversations` |
|---|---|---|
| Key | `span_id` PK, `trace_id`, `parent_span_id` | `conversation_id` PK, `system_id` |
| Payload | `attributes JSON`, `events JSON`, `resource JSON` | `content TEXT`, `content_hash VARCHAR` |
| **Embedding** | **none** | **`embedding FLOAT[N]`** + an HNSW index |
| Enrichment columns | `task_class`, `selector`, `outcome`, `duration_ms`, `latency_ms`, `gen_ai_provider_name`, `gen_ai_request_model`, `gen_ai_usage_input_tokens`, `gen_ai_usage_cost`, `status_code`, `status_message` | none — enrichment lives in `metadata JSON` |
| Time | `start_time_unix_nano`, `end_time_unix_nano`, `received_at_unix_nano` | `timestamp`, `uploaded_at` |
| Written by | the OTLP receiver | the event subscriber and the Session-Buddy tools |
| Read by | the health aggregator, span queries | the knowledge graph refresh, aging, semantic search |

So: **`spans_substrate` is machine telemetry — richly enriched, never
embedded. `conversations` is the semantic corpus — embedded, vector-indexed,
and the input to the knowledge graph, the aging chain and the cold tier.**
The operator's recollection is right: `conversations` was designed to be the
collective corpus that every session store pushes to, and the knowledge
graph's own health feed names that path explicitly
(`"source_producer": "event_subscriber (Redis bodai:events -> conversations)"`).

**The separation is intact; the `conversations` side was never connected.**

Note also that `spans_substrate` has `task_class` as a **real indexed
column** (`idx_spans_selector_task_class_outcome`), alongside `selector`,
`outcome` and `duration_ms` — the exact fields Mahavishnu's enricher
produces. The schema was built for the enriched spans; the arriving spans
simply do not carry the attributes. That is a producer-side gap, not a
schema gap.

`settings/akosha.yaml:223` sets `otel.receiver.trace_ingester_poller: false`.
The `OtelTraceIngester` was the only writer of `conversations`
(`akosha/ingestion/otel_ingester.py:444` → `hot_store.insert(record)`), and
it is disabled by configuration — deliberately, in the PR-A8 migration that
re-pointed the health feeds at the receiver. The server's own comment says
it: *"the poller's cycles_total kept climbing while it ingested nothing at
all"* (`akosha/mcp/server.py:551`).

Worse, the poller could not have worked as written. It POSTs an **empty**
`resourceSpans` envelope and expects the receiver to **return the spans it
holds** (`otel_ingester.py:258-265`) — but it was pointed at
`http://127.0.0.1:4318/v1/traces`, which is *Akosha's own export-only
receiver*. A standard OTLP/HTTP receiver accepts spans and returns an empty
`ExportTraceServiceResponse`; it does not serve them back.

**So the unified pgvector warm tier, as designed, would be a tier with no
input.** Neither plan contained a writer for `conversations`, and both plans'
verification gates asserted on `mcp_tool_call_feed.entities_count`, which
reads `spans_substrate` — the wrong table.

**The live health endpoint confirms it, and widens the blast radius.** Read
from the running server on `:8682` (2026-11-17, akosha-mcp 0.27.0, 22.8h
uptime, top-level `status: "warming_up"`):

| Feed | entities | errors | cycles | Its actual source |
|---|---|---|---|---|
| `mcp_tool_call_feed` | **0** | 0 | 16,365 | `spans_substrate(task_class='mcp_tool_call')` |
| `local_traces_feed` | **0** | 0 | 17,687 | `hot_store.query_traces` → **`FROM conversations`** |
| `aging_feed` | **0** | 0 | 23 | `AgingService.migrate_hot_to_warm(task_class='mcp_tool_call')` → **`conversations`** |
| `knowledge_graph_feed` | **0** | 0 | 1,322 | **`event_subscriber (Redis bodai:events → conversations)`** — `source_producer_running: false` |
| `code_graphs_feed` | 24 | 0 | 1 | the 24 indexed repos |
| `metrics_feed` | 1000 | 0 | 1 | `metrics_substrate` (OTLP MetricsService receiver) |
| `skills_signer` | 1 | 0 | 1 | — |

**Three of the four empty feeds trace to `conversations` having no writer.**
`local_traces_feed`'s own `source` string claims it aggregates
`hot_store.query_traces` **plus** `spans_substrate`, but its count is
computed *only* from `query_traces`
(`akosha/mcp/server.py:659`, `len(await hot_store.query_traces(limit=1000))`),
and `query_traces` reads **`FROM conversations`**
(`akosha/storage/hot_store.py:317`). The `spans_substrate` half of the label
is not reflected in the count. The count is also wrapped in
`with suppress(Exception)`, so a raised query would read as zero rather
than as an error.

The fourth, `mcp_tool_call_feed`, is empty for the separate reason already
established: no arriving span carries `task_class`.

**And the knowledge graph names its dead producer explicitly:**
`"source_producer": "event_subscriber (Redis bodai:events -> conversations)"`,
`"source_producer_running": false`, `"refresh_task_running": true`. That is
the intended architecture — Redis `bodai:events` → `conversations` →
knowledge graph → aging → warm tier → cold tier — and it is severed at the
first hop. The subscriber's own log shows why:

```
akosha.bodai_subscriber: xreadgroup error: Timeout reading from localhost:6379
akosha.bodai_subscriber.fallback_to_poll: xgroup_create failed: Timeout
```

**3,021 timeouts** in `akosha.err`. Live Redis confirms the cause — the
consumer group `akosha-tool-invocation-indexers` holds **405,790 consumers**
and **660 pending entries**, against a stream of 13,506 entries:

```
name: akosha-tool-invocation-indexers
consumers: 405790
pending: 660
last-delivered-id: 1794934136216-0
entries-read: 17310
lag: 0
```

The group *is* advancing (`last-delivered-id` current, `lag: 0`), so
messages are being read — and then dropped, because the decoder does not
understand the wire shape. Confirmed live from the newest stream entry:

```
1794934136216-0
  channel: bodai.hooks.post-tool-use
  payload: {"event": "PostToolUse", "harness": "claude", "session_id": "…", …}
  headers: …
```

That is `{channel, payload, headers}` — exactly the shape
`_decode_envelope` (`akosha/ingestion/bodai_event_subscriber.py:526-587`)
does not understand. It understands `envelope=<JSON>` and
`{topic, payload_json, headers_json}`. So the message is decoded to `None`
and XACKed and dropped without a warning.

**Redis persistence is also reporting a problem** — `MISCONF Redis is configured to save RDB snapshots, but it's currently unable to persist to disk` (12 occurrences). Disk has 67 GB free, so this is a path or
permissions issue rather than exhaustion, but it is worth a look: a Redis
that cannot persist is a Redis that loses the event stream on restart.

### 7d.9 The verdict

The individual choices were defensible: a deterministic fallback for
offline operation, a per-component dimension constant, a local index for a
local tier. The **composition** was not. Three defects — a width-compatible
mock, an unreachable standard provider, and a width-only compatibility check
— compounded into a system that was simultaneously dimension-broken,
model-blind, and quietly wrong.

**Standardising is not a simplification of a working system; it is the
repair of a system whose parts disagreed.** The three fixes below are what
"standardise" actually means in practice:

1. **One client, one chain, one model.** Oneiric's `EmbeddingService` gains
   the `openai_gateway` and `openai_direct` legs and reuses its existing
   `OpenAIEmbeddingAdapter`.
1. **The mock stops being a fallback** in any tier that holds semantic
   vectors. It becomes a startup failure.
1. **Compatibility is checked on `(model, dimensions, space_version)`, not
   on width.**

### 7d.10 The conflict with the existing fleet plan — now resolved

`session-buddy/docs/plans/2026-11-06-fleet-embedding-width-768.md` is a
**draft** that has already passed an 8-agent multi-lens review with 29
findings folded in. It is close to what this design wants, and two of its
choices collide with the corrections above. Both must be resolved before
either plan executes.

| | Fleet-768 plan | This design |
|---|---|---|
| Width | **768**, `dimensions=768` (Matryoshka-shortened) | **1536**, native, measured (MTEB 62.3 vs an interpolated ~61.9–62.1) |
| Mock | "The `mock` fallback widens 384 → 768, so an offline deployment still writes fleet-width vectors" | The mock must be a **startup failure** in any tier holding semantic vectors (O11) |

The mock is the important one. The 768 plan widens the mock so an offline
deployment keeps writing; this design says the mock must never write at all,
because a mock vector in a semantic tier is a silent corruption that a width
check cannot detect — and because the mock is precisely what hid the
original dimension defect (§7d.2).

**The 768 plan's own reasoning for its width is also weakened.** It aligns
the fleet with the local tier (nomic-embed-text, bge-base-en-v1.5 and
EmbeddingGemma-300M are all natively 768). The operator has since ruled
that the fleet does not depend on the local llama-server. With that
constraint retired, 768 is an unmeasured truncation of a cloud model kept
alive by a requirement that no longer applies.

______________________________________________________________________

### 7d.11 What Akosha advertises versus what it does

A live audit of the running server (2026-11-17, akosha-mcp 0.27.0, 39 MCP
tools on the wire; the README claims 34) found **ten advertised capabilities
that are dead or permanently empty**, and one very large body of data that
is written but never read.

**What is actually live today:** the OTLP spans substrate
(`akosha_search_spans`, `akosha_spans_summary`, retention sweep), the
code-graph index (24 repos, 300+ regex hits), the embedding tools
(**returning the deterministic 384-dim mock** — `/health` reports
`embeddings.mode = "fallback"`), the signed skill and agent catalogues,
ecosystem skill federation, the health and metrics surfaces, and the three
aging crons.

**What is advertised and empty:**

| # | Advertised capability | Why it is empty |
|---|---|---|
| B-1 | Time-series analytics — trends, anomalies, correlation | `akosha/mcp/tools/group_registers.py:73` creates a **second** `TimeSeriesAnalytics()` that is never `initialize()`d, while the properly-initialised instance (`akosha/mcp/server.py:931-934`) is only used at shutdown. The durable SQLite `~/.akosha/state/metrics.db` holds **47,386 rows** that the tool-side instance never loads — so `akosha_get_system_metrics` returns `total_metrics: 0` and the other three return "Insufficient data". |
| B-2 | `mcp_tool_call_feed` | No producer sets `task_class`. Sampling 200 live spans across all five services: `task_class = NULL` and `selector = NULL` for **every one**. The MCP middleware emits `rpc.system`/`rpc.method`/`mcp.*` attributes only. |
| B-3 | `local_traces_feed` | Its declared source claims `query_traces` **+** `spans_substrate`, but the count is computed only from `query_traces` — which reads `conversations`. |
| B-4 | "Subscriber not running" | **A false negative.** `akosha/ingestion/subscriber_wiring.py:128` reads `.running` off the *orchestrator*, which has no such property — the property exists only on the inner subscriber (`bodai_event_subscriber.py:154-155`). So `_event_subscriber_running` is **always** False. Redis proves the consumer is alive: `lag: 0`, `entries-read: 17311`. The feed lies about its own producer. |
| B-5 | Redis → `conversations` | The subscriber filters on `topic == "websocket_tool_invocation"` and XACKs everything else. **Zero stream entries carry that topic.** |
| B-6 | Knowledge graph | Fed from `conversations` via `kg_refresh` — 1,347 cycles, 0 rows, because the producer (B-5) writes nothing. |
| B-7 | Session-Buddy → Akosha push | The push client POSTs to a route that does not exist (below). |
| B-8 | Session-Buddy → S3 → Akosha pull | `IngestionWorker` exists (`akosha/ingestion/worker.py`) but **is never instantiated** — `akosha/main.py:118` initialises an empty list and nothing appends to it. |
| B-9 | "Hot tier + Redis cache" | `akosha/cache/` does not exist. `CLAUDE.md:115` and `docs/ARCHITECTURE.md` still claim it; `CLAUDE.md:190-193` is honest that it is disabled. |
| B-10 | Profile-aware `discover_tools` | `register_all_tools` is never called by the server (the W0 profile dispatch replaced it). The live `discover_tools` is mcp-common's flat catalog, so the profile gating is invisible at runtime. |

**One more that is neither live nor dead: `metrics_substrate` holds 1,000 rows,
its feed reports healthy, and no MCP tool reads it.** The only reader is the
health counter. It is also excluded from `_aggregate.all_data_feeds_ok`
(`akosha/mcp/server.py:2232-2240`).

**The Session-Buddy push fails three independent ways** — each alone would
stop every row:

1. **Wrong URL.** `akosha_url` is `"http://localhost:8682/mcp"`
   (`session_buddy/storage/akosha_sync.py:77`) and the client appends
   `"/mcp/v1"` (`:1068`), producing `http://localhost:8682/mcp/mcp/v1`.
   Probed live: **404**. Only `/mcp` is mounted.
1. **Wrong tool name.** The client calls `batch_store_memories`;
   Akosha exposes `akosha_batch_store_memories`.
1. **Wrong width.** Session-Buddy sends 768-dim; Akosha's hot store bakes
   `FLOAT[384]` and raises
   `ValueError: DuckdbHotStore.insert: embedding dim mismatch (expected 384, got 768)`
   — exactly the break the November spec predicted.

**And a fourth, structural blocker:** `akosha_cloud_endpoint` is
**overloaded** — simultaneously the S3/R2 endpoint and the Akosha MCP base
URL. There is no setting for "the central Akosha's MCP URL", so pointing a
second machine at a central Akosha is not expressible in the current config.

**The metadata convention mismatch.** `AgingService` filters
`metadata->>'task_class' = 'mcp_tool_call'` — a **span** concept — over a
table that holds both spans and memories. The Session-Buddy push path sets
**no** `task_class` at all: `_build_memory_metadata`
(`akosha/mcp/tools/session_buddy_tools.py:26-42`) keeps only `source`,
`original_id`, `type` and `ingestion_method`, discarding `category`,
`subcategory`, `importance_score`, `memory_tier`, `source_type`, `project`,
`namespace`, `session_id` and `user_id`. **Even a successful push would be
invisible** to `query_traces(task_class='mcp_tool_call')` and would never be
aged.

`★ Insight ─────────────────────────────────────`
This is the same failure shape as the embedding contract: a column, a filter
and a feed all agree on a vocabulary (`task_class = 'mcp_tool_call'`) that
only one producer ever speaks, and the second producer's metadata is
narrowed to four fields on the way in. Two producers, one table, two
incompatible conventions — and a dimension check cannot see it.
`─────────────────────────────────────────────────`

### 7d.12 The corpus is already on the stream

The central-store design the operator remembers **was real, canonical, and
never built.** `session-buddy/docs/specs/2026-11-05-conversation-transcript-ingestion-design.md`
(`status: draft`, `role: canonical`) states:

> "Session-Buddy is the fleet-wide session store: one queryable surface for
> session history reachable from any MCP harness, **including sessions that
> ran on machines whose local `~/.claude/projects` the querying host cannot
> see**."

and names the gap precisely:

> "The gap is the *producer*: nothing parses a Claude Code transcript and
> pushes turns. This spec builds that producer and the retrieval surface
> around it."

Its architecture is `Claude Code session (any machine) → Stop hook → transcript_ingest.py → POST ingest_turns → ┌─ Session-Buddy (central)`, with
the remote machine addressed by `SESSION_BUDDY_URL` (default
`http://localhost:8678`).

**None of it was built.** `grep -rn "ingest_turns"` returns **zero hits** in
the whole Session-Buddy repo. `SESSION_BUDDY_URL` returns zero hits.
`transcript_ingester.py`, `injection_guard.py`, `ingest_tools.py`,
`embed_worker.py`, `pruner.py` and `transcript_ingest.py` are all absent.
The spec and its plan are both still `status: draft`; the only commit is
`d80ebeb5 docs: add the conversation transcript ingestion and fleet embedding width plans`.

Worse, the November design **deliberately contradicted** the expectation
that raw turns reach Akosha:

> **"Raw turns never reach Akosha."** — plan Global Constraint L41
> **"No Akosha synchronization of raw turns."** — spec Non-goals L117

and it drew a boundary that the operator's recollection does not match:

> "Session-Buddy's `MemoryTier` is a **semantic** tier on a row — not a
> storage tier. **Akosha's hot/warm/cold are storage tiers.** They are
> orthogonal."

**So there are two different `conversations`, and the distinction matters:**

| | Akosha `conversations` | Session-Buddy `conversations_v2` |
|---|---|---|
| Holds | **memory items** — content, embedding, content_hash | **dialogue** — turns, with `turn_parent_id` for transcript chains |
| Embedded | yes | yes |
| Read by | aging, knowledge graph, semantic search | `search_conversations`, the sync client |

Akosha's `conversations` is a **misnomer** carried from the substrate: it is
the memory table.

**But the corpus is already on the Redis stream.** A read-only channel
histogram over the first 2,000 entries of `bodai:events`:

| channel | count |
|---|---|
| `bodai.hooks.user-prompt-submit` | 1218 |
| `bodai.hooks.post-tool-use` | 483 |
| `bodai.hooks.session-start` | 274 |
| `bodai.hooks.session-end` | 20 |
| `mcp.tool.call` | 4 |
| `websocket_tool_invocation` | **0** |

These are pushed by **Mahavishnu's** hook bridge
(`mahavishnu/mahavishnu/bodai_hook_bridge.py:379`), not Session-Buddy.
Session-Buddy's own publisher emits only `task.*` envelopes and a heartbeat
marker — **never a conversation**.

**And the payload carries what the unbuilt ingester needed.** Verified live
from the newest stream entry:

```
channel: bodai.hooks.post-tool-use
payload: {"event": "PostToolUse", "harness": "claude", "session_id": "8b9397e4-…",
          "cwd": "/Users/les/Projects/akosha", "tool_name": "mcp__tavily__tavily_search",
          "tool_input": {…}, "timestamp": "…",
          "raw": {"session_id": "…",
                  "transcript_path": "/Users/les/.claude/projects/…/8b9397e4-….jsonl",
                  "cwd": "…", "prompt_id": "…", "agent_id": "…",
                  "hook_event_name": "PostToolUse", "tool_name": "…", …}}
```

`transcript_path` and `session_id` are right there — the exact two fields
the November spec's `transcript_ingest.py` hook was designed to consume.
The transport exists; the parser does not. Akosha's subscriber reads these
messages, cannot decode them, and drops them.

**This reframes the missing writer (§7d.8, §8.2 step 21).** The decision is
not "which of (a)/(b)/(c)" alone — it is that the fourth option, **(d)
decode the `bodai.hooks.*` events that are already flowing and use the
`transcript_path` they carry**, is the one that restores the operator's
original design intent: every machine's session content, pushed to a
central store, for collective processing. It needs no new transport, no
new producer, and no cross-repo hook — only a decoder for the shape that is
already on the wire and an index that is not filtered to death.

______________________________________________________________________

## 8. Ordering — after the standardisation decisions

Three reviews and the operator's decisions have changed what binds. The
irreversibility argument is retired (§1); the width question has replaced
it; and one of the two paths into the warm tier was found to have no input
(§7d.8).

The sequence below is executed as **Plan M**
(`docs/plans/2026-11-17-unified-pgvector-warm-tier.md`), which supersedes
both the architecture-first and the feed-first plans.

### 8.1 What actually binds now

1. **The dimension must be pinned before any vector column is created.**
   Every fixed-width column — Akosha's hot `conversations`, the pgvector
   warm table, Session-Buddy's `FLOAT[N]` pair, Mahavishnu's `VECTOR(384)`,
   Crackerjack's index — is frozen at creation. Changing the fleet width
   afterwards means a rebuild of each.
1. **A real embedding provider must be reachable before anything writes.**
   Otherwise the chain writes mock vectors that are indistinguishable from
   real ones by width.
1. **Akosha must not own SQL.** The adapter consolidation has to land in
   Oneiric before the warm tier can use it — and Oneiric's release is
   followed by an operator-owned version pin.
1. **The feed repair and the embedding work are independent.** The producer
   fix is about `task_class`; the decoder fix is about the Redis wire shape.
   Neither depends on a dimension.

### 8.2 The sequence

**Phase 0 — the embedding contract** (blocks everything vector-shaped)

1. Oneiric: `FLEET_EMBEDDING_MODEL` / `_DIM` / `_SPACE` constants (§7.7).
1. Oneiric: `openai_gateway` + `openai_direct` legs, reusing
   `oneiric/adapters/embedding/openai.py`. Bifrost virtual key in `x-bf-vk`;
   direct `OPENAI_API_KEY`. `dimensions` sent only if the canonical width is
   a truncation.
1. Oneiric: **remove the mock from the semantic-vector path** — startup
   failure, not a per-request fallback (O11).
1. Oneiric: raise the six `le=1024` bounds; replace the `(384,)` shape gate
   in `queries.py:55` with the configured width.
1. Oneiric: either wire the substrate's `embedding__*` knobs so they are
   read, or delete them. A knob that resolves but is never read is the
   defect class this whole section is about.
1. Akosha: delete `BACKEND_DEFAULT_DIMS`. The probed dimension is the only
   source of truth — a hardcoded table per backend is exactly what let the
   mock's 384 and the live 768 coexist.
1. Akosha: rebuild `conversations` at the fleet width. It holds 0 rows, so
   this is free.
1. **Verify:** a live `/v1/embeddings` round trip through Bifrost returns
   vectors of the canonical width; `embeddings.mode == "real"`; the mock is
   unreachable in a semantic tier.

**Phase 1 — the adapter consolidation** (needs a Oneiric release)

9. Mahavishnu: make the fork a true subclass of Oneiric's adapter,
   overriding only `create_collection`, `search`, `_write_documents`,
   `health` and `_connection_kwargs`.
1. Fix `ef_search` with `SELECT set_config('hnsw.ef_search', $1, true)`
   on the **same connection**, inside an explicit transaction. Add a
   **real-Postgres** integration test — the mock-only harness structurally
   cannot catch a SQL syntax error.
1. Add `halfvec` support; derive the index name from the index method so a
   method change cannot leave two ANN indexes on one column.
1. Upstream HNSW into Oneiric's adapter as a separate release, keeping
   IVFFlat the default so Akosha's collections are unaffected.
1. Delete the fork; correct the false "Oneiric already supports HNSW"
   claims in the design and decision documents.

**Phase 2 — the unified pgvector warm tier**

14. `akosha_warm` with `embedding halfvec(<fleet_dim>)`, indexed with HNSW;
    `system_id` and `timestamp` as real, indexed columns.
01. `insert_batch` via `copy_records_to_table` — Oneiric's current
    `insert` is one round trip per document.
01. The store-level API: `count`, `fetch_older_than`, `delete_by_ids`.
    The cold exporter stops speaking DuckDB SQL against `warm_store.conn`.
01. Fail-loud backend selection (D6): no split brain between DuckDB and
    pgvector.

**Phase 3 — the feed repair and the missing writer** (independent of 0–2,
but must land before aging is enabled)

18. Producer side: get `task_class` onto arriving spans, or change the feed's
    query. Cross-repo.
01. Redis: per-process consumer name → XAUTOCLAIM the pending entries →
    delete the 400k stale consumers. Teach `_decode_envelope` the
    `{channel, headers, payload}` shape and **log decode failures** — today
    a decode failure is a silent drop. Read the PEL, not `>`.
01. Fix `aging_feed.errors_total` accounting — the loop logs `stats.errors`
    but never adds them to `_aging_errors`.
01. **Decide where `conversations` comes from.** This is the decision the
    two plans never made. Three options, in order of preference:
    - **(a) The OTLP receiver writes `conversations` as well as
      `spans_substrate`.** One ingestion path, one set of rows, and the
      receiver already receives every span. Cost: one embedding per span at
      ingest.
    - **(b) Aging embeds lazily at age time.** The metadata retains
      `otel.trace_id`/`span_id` (`otel_ingester.py:409-414`), so the span can
      be re-fetched and re-embedded when it ages. Cost: one embedding per
      aged row instead of per ingested row; no embedding cost for rows that
      never age. The window between ingest and age is the natural place to
      batch this.
    - **(c) Re-enable the poller.** Rejected: its design (POST an empty
      envelope and expect the spans back) does not match OTLP/HTTP, and it
      was pointed at an export-only receiver.

**Phase 4 — enable aging**

22. Remove the hold-off knob (it is a transition guard, not a permanent
    feature — once the warm tier is pgvector and the input is float32, there
    is nothing left to guard).
01. Bound the hot tier by **size, not age** (O13). The OTel span rate is
    ~17k rows/day; a size cap bounds disk without touching the newest rows.
01. The cold exporter reads and deletes through the store API.

### 8.3 Why this sequence

**Phases 0 and 1 come first** because both are *blocking* and both are
cheap relative to what they unblock. Phase 0 decides the width, and every
fixed-width column in the fleet is frozen at creation — changing the width
after the tier is built means rebuilding the tier. Phase 1 decides which
SQL Akosha is allowed to own, and Akosha must not own it.

**Phase 3 runs in parallel.** The feed repair is a producer-side and
wire-shape problem. Nothing about it depends on a dimension, and nothing
about the dimension depends on it. Sequencing them would waste a cycle.

**Phase 4 last**, because aging with an empty source table is a no-op and
aging with a mock-embedded source table is a silent corruption. It should
be enabled only once Phase 0's provider and Phase 3's writer are both real.

**What changed from the two original plans.** The merged sequence in the
previous revision put the feed repair in Phase 1 and the pgvector build in
Phase 2, ordered by "the cost of being wrong". That tie-break no longer
applies — records are not sacred — and the real constraint is the frozen
column width, which is Phase 0's job. The hold-off knob, which existed to
protect records, becomes a transition guard and then disappears.

### 8.4 The decision that remains

**Where `conversations` comes from.** The width and the mock's fate are
decided (§12). What remains is the source of the table aging consumes —
`(a)` the OTLP receiver also writes `conversations`, `(b)` aging embeds
lazily at age time, or `(c)` re-enable the disabled poller. Option (b) is
the recommendation; the choice is recorded in Plan M step 3.5.

The width decision is revisited only on evidence. `text-embedding-3-large`
at 1024 scores **+1.8 MTEB** at *less* storage than 3-small at 1536, and
the contract in §7.5 makes that a re-embed rather than a redesign — so the
switch should be driven by a measured Recall@k on the fleet's own corpus,
not by an MTEB average.

______________________________________________________________________

## 9. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Bifrost OpenAI route out of credits | Low | Renewal expected within hours; verify the route with a live call before starting the embedding work. MiniMax is a transport substitute, not a model substitute |
| Oneiric release cadence delays `halfvec`/HNSW | Medium | Akosha can own the table temporarily; the adapter port follows |
| Hot-tier growth while aging is held | Medium | Add a bounded retention sweep for `conversations` independent of aging |
| Dimension mismatch recurs on a model change | Medium | Derive the table dim from the resolved embedding dim; refuse to start on mismatch |
| Split brain from fail-soft | High | D6 — fail loud, never to a different backend |
| 25 orphaned shard tables left in `akosha_pgvector` | Low | Drop during the migration |
| Consumer-group leak recurs | Medium | Fix `_auto_consumer_name` to be per-process; add a group-size guard |

______________________________________________________________________

## 10. Verification

- [ ] `resolve_warm_store_config()` resolves from XDG and reports the source.
- [ ] A warm-tier e2e passes with the service running (no file lock).
- [ ] `mcp_tool_call_feed.entities_count > 0` after the feed repair.
- [ ] `local_traces_feed.entities_count > 0` after the dimension repair.
- [ ] The consumer group holds one consumer, not 400,000.
- [ ] Aging moves records and the warm row count rises.
- [ ] A pgvector similarity query returns the aged record.
- [ ] The cold exporter evicts from the pgvector store.
- [ ] `pg_dump`/restore round-trips the warm table.

______________________________________________________________________

## 11. Existing plans and overlap

Checked against `docs/plans/PLAN_INDEX.md` (regenerated 2026-11-16).

### `2026-10-05-lazy-warm-shard-init.md` — **superseded by this design**

Status `draft`, topic `warm-tier-storage`. It attacks the same problem this
design does: *"The pgvector warm store creates one asyncpg connection pool
per shard… 256 × (1 to 10) connections"*, reproduced 2026-10-05 with
`TooManyConnectionsError` and pgbouncer's `ConnectionError: unexpected connection_lost() call`.

Two things follow.

**Its goal is achieved more completely by D2.** Lazy per-shard init defers
the connection bomb to first use, but the steady state still reaches 256
pools as shards are touched, and it leaves the IVFFlat-per-shard problem and
the two-store split in place. One table (D2) removes the problem at its root
rather than scheduling around it. **This plan should be marked superseded by
this design rather than implemented.**

**Its stated workaround is dead config.** The plan records *"Configured
workaround applied to XDG layer (`num_partitions: 16`)"*. `num_partitions`
is **never read** — `ShardRouter` takes its shard count from
`akosha.config.config.shard_count` (`akosha/storage/sharding.py:31`), which
is 256. The `warm:` block in `settings/akosha.yaml` says so in a comment
("declared but UNREAD"), and the XDG overlay's `num_partitions: 16` has had
no effect. The warm store has been running with 256 shards. The workaround
that is real is `shard_count: 16` — the value the operator script stages.

### `2026-09-09-akosha-otel-feed-recovery.md` — **adjacent, and incomplete**

Status `draft`, topic `akosha-otel-feed-recovery`. It repairs the OTel
*transport* (a `GET` against an OTLP receiver that only answers `POST`, and
an Alloy pipeline that accepted traces and dropped them) and relaxes the
`local_traces_feed.ok` gate to tolerate an empty-but-healthy feed.

That is the same feed my Section 8 finding breaks, and **it does not mention
the embedding dimension mismatch** — the fourth bug. The feed it aims to
recover is still empty after its transport is fixed, because every insert
fails `DuckdbHotStore.insert: embedding dim mismatch (expected 384, got 768)`. This plan is **not superseded**; it is incomplete, and the dimension
repair has to be added to it or to a sibling plan.

### `2026-11-05-akosha-cold-write-path.md` — **affected**

Status `active`, topic `cold-write-path`. It builds the `ColdExporter` that
reads the warm tier, and its tech stack line reads *"DuckDB (warm tier)"*.
It assumes the DuckDB warm store and the `.conn` surface that D7 removes.
The exporter's *storage contract* (Parquet batches, `system_id/YYYY/MM/DD`
partitions, MinHash fingerprints) is unaffected; only its **source** changes
from DuckDB SQL to the store-level API.

### `2026-09-05-akosha-hardening-impl.md` — **partial overlap**

Status `draft`. It contains the audit-H3 work that rewrote
`akosha/storage/aging.py:_quantize_embedding` with scale persistence
(`quantize_embedding` / `dequantize`). Note that the *batch* write path
(`AgingService._migrate_batch`) still does its own inline numpy quantization
and **drops the scale**, so the persisted INT8 is still not round-trippable
on the path that actually writes. D1 keeps INT8 for the DuckDB backend, so
this work stays relevant — but it should be extended to the batch path, or
the pgvector path must carry float32 (which is what D1 does).

### `session-buddy/docs/plans/2026-11-06-fleet-embedding-width-768.md` — **overlapping, and it wins on scope**

Status `draft`, topic `embeddings`. **This is the fleet-wide embedding
standardisation plan**, already written and already reviewed — 8 agents ×
3 lenses plus a per-plan adversarial adjudicator, 29 findings folded in. It
is not an adjacent plan; it is *the* plan for the part of this design that
lives above the warm tier, and it should be treated as the primary source
for Phase 0.

What it already specifies, and what this design adopts:

- **Oneiric's `EmbeddingService` gains an OpenAI leg, probed ahead of the
  existing chain, reusing the `OpenAIEmbeddingAdapter` Oneiric already
  ships** — so credential resolution, `dimensions` handling, batching,
  retries and rate limiting are not re-implemented.
- **The probe order becomes
  `openai_gateway → openai_direct → llama_cpp → minimax → model2vec → mock`.**
- **Bifrost authenticates with the gateway virtual key** (`BIFROST_VIRTUAL_KEY`),
  never a provider key; direct OpenAI uses `OPENAI_API_KEY` read from the
  environment at call time.
- **Wrong-width vectors are rejected, never coerced** — every component
  writing to a fixed-width column raises on a mismatch.
- **A base-URL convention stated once**: a base URL includes the API prefix
  and the embeddings endpoint is `{base}/embeddings`.
- **`FLEET_EMBEDDING_DIM` in `oneiric/adapters/observability/embeddings.py`
  as the source of truth**, mirrored by each repo's own module-level `int`.
- **Crackerjack's SHA-256 pseudo-vectors replaced with real vectors.**
- A per-repo lint and test gate table (which differs materially across the
  fleet — `BLE001` is selected in session-buddy and mahavishnu but not in
  oneiric, akosha or crackerjack).

**Two conflicts existed. Both are now resolved by the operator (2026-11-17),
and the fleet-768 plan must be *updated* to match — not merely reconciled.**

| | Fleet-768 plan | Resolved position |
|---|---|---|
| Width | **768**, `dimensions=768` | **1536**, native, measured (Section 7.6) — operator decision |
| Mock | "The `mock` fallback widens 384 → 768, so an offline deployment still writes fleet-width vectors" | The mock must be a **startup failure** in any tier holding semantic vectors (O11) — operator decision |

The mock conflict is the substantive one. The 768 plan widens the mock so
an offline deployment keeps writing; this design says the mock must never
write at all, because a mock vector in a semantic tier is a silent
corruption that no width check can detect — and because the mock is
precisely what hid the original dimension defect (Section 7d.2).

The width conflict is a decision, not an error: 768 is an *unmeasured*
truncation point on OpenAI's MTEB curve, and the reason it was chosen (the
local tier is natively 768-wide) has been retired by the operator's decision
not to depend on the local llama-server.

**The width decision belongs to this plan, because this plan owns the
warm-tier column that the width fixes.** The fleet-768 plan should be
updated, not replaced.

### `mahavishnu/docs/plans/2026-10-14-decision-adapter-category.md` — **overlapping, and its stated rationale is false**

Status `active` (`role: decision`). Its Task 4 deletes Mahavishnu's
`pgvector_adapter.py` on the rationale that *"Oneiric's class supports HNSW
per the verified source"* (`docs/specs/2026-10-14-decision-adapter-category-design.md:596-598`).

**Oneiric's pgvector adapter has no HNSW.** Its only ANN index is IVFFlat
(`oneiric/adapters/vector/pgvector.py:271-278`). Deleting the fork on that
rationale would silently drop HNSW indexing from Mahavishnu's OTel path.

Its own precondition check is sound — *"Expected: empty. If anything is
found, that code path is a consumer of the fork and must be migrated before
deletion. Stop and migrate."* (`docs/plans/2026-10-14-decision-adapter-category.md:1086-1089`)
— and the grep is **not** empty (`mahavishnu/ingesters/otel_ingester.py:638`
imports it), so the plan correctly says *stop and migrate*. What must change
is the justification, and the migration path becomes the two-step sequence in
D4.

### Not overlapping

`2026-09-28-cold-store-gcs-endpoint-url-wiring.md` (cold-store config),
`2026-09-27-pyproject-single-source-of-truth.md` (version strings),
`2026-09-06-otel-trace-ingester-impl.md` and its spec (the ingester that
produces the vectors — relevant as the source of the 768-dim values, but it
owns no warm-tier code).

______________________________________________________________________

## 12. Open decisions for the operator

Most of the questions this design originally opened are now closed by the
operator's decisions (2026-11-17) or by evidence. **Two remain.**

1. **Where `conversations` comes from.** The table aging reads has 0 rows
   and no writer: the only writer, the `OtelTraceIngester` poller, is
   disabled by configuration (`settings/akosha.yaml:223`) and could not have
   worked as written. Options (a) the OTLP receiver also writes
   `conversations`, (b) aging embeds lazily at age time, (c) re-enable the
   poller (not recommended). Section 8.2 step 21; Plan M step 3.5.
1. **Tenant isolation.** Accept a logical (indexed predicate) boundary in
   exchange for one table, or partition by `system_id` and give up the
   single-index property. Unchanged from the original design — and still a
   live question only if the deployment becomes multi-tenant.

### Closed by the operator's decisions

- **The embedding width — 1536.** `text-embedding-3-small`'s native width,
  a measured MTEB point (62.3), no `dimensions` parameter, and no local
  model width-eligible — so the chain either reaches a real provider or
  fails loudly. 768 was rejected as an unmeasured truncation whose
  justification (a local tier) has been retired; 1024 via
  `text-embedding-3-large` was rejected as buying a quality margin on a
  corpus that does not exist yet, with the migration kept cheap by the
  `(model, dimensions, space_version)` contract. Section 7.6.
- **The mock's fate in the semantic tiers — startup failure.** Not a
  per-request fallback, and not "widen it to the fleet width and keep
  writing". A width-compatible mock is a silent corruption no dimension
  check can detect, and the mock is what hid the original defect. This
  resolves the conflict with the fleet-768 plan recorded in Section 7d.10;
  that plan must be updated, not merely reconciled.
- **Ordering.** The feed-repair-first ordering was resolved by the
  discovery that the aging path has no input (Section 7d.8) and by the
  retirement of the irreversibility constraint (Section 1). Section 8 gives
  the resulting sequence.
- **Legacy retention and backwards compatibility.** Out of scope. Records may
  be lost; the DuckDB warm store is deleted rather than migrated.
- **The local llama-server.** Not depended upon. The chain's local legs are
  not fleet-width-safe at 1536 and are excluded from the standard chain.
- **Oneiric vs Akosha ownership of the DDL.** Oneiric owns it. D4 gives the
  two-step path and the corrections to the existing deletion plan.
