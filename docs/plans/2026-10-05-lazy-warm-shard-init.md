---
status: complete
role: superseded
date: 2026-10-05
last_reviewed: 2026-11-21
superseded_by: docs/plans/2026-11-17-unified-pgvector-warm-tier.md
topic: warm-tier-storage
---

# Plan — Lazy initialization of PgvectorWarmStore shards
> **Superseded 2026-11-21.** This plan is not to be implemented. The
> unified warm tier (docs/plans/2026-11-17-unified-pgvector-warm-tier.md) replaces the per-shard pgvector
> store with a single table, which removes the connection problem this
> plan was written to schedule around. The document is retained for the
> chain and for the record of the shard-count arithmetic.
>
> Its stated remedy was also dead config: `warm.num_partitions` is read by
> no production code. The knob that works is the top-level `shard_count`.


## Context

The pgvector warm store creates one asyncpg connection pool per shard
synchronously during `PgvectorWarmStore.initialize()`:

```python
# akosha/storage/pgvector_warm_store.py:141-156
async def initialize(self) -> None:
    async with self._init_lock:
        if self._initialized:
            return
        settings = PgvectorSettings(dsn=self._pg_url)
        for shard_id in range(self._shard_router.num_shards):  # 256 by default
            adapter = PgvectorAdapter(settings)
            await adapter.init()                              # one pool per shard
            await adapter.create_collection(
                name=_collection_name(shard_id),
                dimension=self._embedding_dimension,
                distance_metric=_DISTANCE_METRIC,
            )
            self._adapters[shard_id] = adapter
        self._initialized = True
```

Each `PgvectorAdapter` builds an asyncpg pool with `min_size=1, max_size=10`
defaults. With `shard_count=256`, the cold-init concurrency is
256 × (1 to 10) connections — well past:

| Backend | Hard limit | Observed error at 256 shards |
|---|---|---|
| Direct Postgres | `max_connections=100` (default) | `TooManyConnectionsError: sorry, too many clients already` |
| Pgbouncer (default `default_pool_size=20`) | pool saturated | `ConnectionError: unexpected connection_lost() call` |

Reproduced 2026-10-05 during restart verification. Reducing the
top-level `shard_count` to 4 unblocks both backends, but 4 shards caps
delivery concurrency at 4 — an artificial ceiling.

(This plan originally named `warm.num_partitions` for that. That key is
declared on `WarmStorageConfig` and read by no production code — the
warm store sizes its shards from `config.shard_count` at
`akosha/storage/sharding.py:31`. Both defaults are 256, so the mistake
was invisible: the arithmetic below is right and the remedy was dead.)

## Goal

Replace eager shard initialization with lazy per-shard initialization
on first read or write, so `shard_count=256` becomes a logical capacity
ceiling rather than a cold-start resource bomb.

## Integration Contract

- **Triggered from**: `PgvectorWarmStore.insert()` and
  `PgvectorWarmStore.search_similar()` (any code path that needs a
  specific shard's adapter).
- **Returns to / updates**: the shard's adapter is cached on first
  use; subsequent calls reuse it.
- **Demonstrable by**: `shard_count=256` cold-init succeeds against
  pgbouncer and direct Postgres without exceeding connection limits;
  `insert()` / `search_similar()` latency on cold shards is bounded by
  a single pool-create (~50 ms), not catastrophic.
- **Rollback signal**: revert `_get_or_init_adapter(shard_id)` to the
  eager `initialize()` loop; behavior is identical at small `shard_count`.
- **Observability added**: per-shard `first_use_at` timestamp on
  `PgvectorAdapter`; warm-store health-feed should report
  `initialized_shards` vs `shard_count` so operators see lazy-init
  progress instead of a binary not-ready/healthy flip.

## Implementation sketch

Replace the eager loop in `initialize()` with:

```python
def _get_or_init_adapter(self, shard_id: int) -> PgvectorAdapter:
    if (adapter := self._adapters.get(shard_id)) is not None:
        return adapter
    with self._init_lock:  # only one shard inits at a time
        if (adapter := self._adapters.get(shard_id)) is None:
            settings = PgvectorSettings(dsn=self._pg_url)
            adapter = PgvectorAdapter(settings)
            await adapter.init()
            await adapter.create_collection(
                name=_collection_name(shard_id),
                dimension=self._embedding_dimension,
                distance_metric=_DISTANCE_METRIC,
            )
            self._adapters[shard_id] = adapter
        return adapter
```

`insert()` and `search_similar()` swap `_adapters[shard_id]` for
`_get_or_init_adapter(shard_id)`. `_initialized` flag stays (read by
the `_polling_loop`/search APIs that need "the store exists") but no
longer requires all shards to be live.

## Wire-up

Per `docs/.claude/decisions/wire-up-contract.md`:

- **Integration Contract** block above covers triggers + observability.
- **Per CLAUDE.md**: "every phase deliverable must include an
  Integration Contract block" — done.
- After implementation: run `python scripts/audit_orphans.py` (no new
  public symbols expected; `_get_or_init_adapter` is internal).
- Crackerjack gates (`crackerjack run -v`) before merge.

## Rollback

The eager init is contained to one method (`initialize()`); the
post-merge revert is `git revert <merge-commit>` plus reverting the
two call-site swaps in `insert()` / `search_similar()`.

## Status

`complete` / `superseded` — surfaced 2026-10-05 during restart
verification, and superseded on 2026-11-21 by
`docs/plans/2026-11-17-unified-pgvector-warm-tier.md`. The unified single-table design removes the
per-shard pool problem at its root rather than scheduling around it,
which is what this plan was written to do.

The configured workaround recorded here was applied to the XDG layer as
`num_partitions: 16`. That key was **never read** — the warm store sizes
its shards from the top-level `shard_count` setting
(`akosha/storage/sharding.py:31`), which was 256. The workaround that
actually works is `shard_count: 16`.
