"""Pgvector-backed warm store for Akosha — implements WarmStore interface via Oneiric PgvectorAdapter.

This is the warm-tier drop-in for ``WarmStore`` (DuckDB) when
``AKOSHA__STORAGE__WARM__BACKEND=pgvector``. Each shard from the existing
``ShardRouter`` (256 by default) maps 1:1 to a dedicated pgvector collection
named ``akosha_warm_shard_{N:03d}``. System ID is encoded as a metadata
filter (``metadata.system_id``) so the per-system query path stays close
to the existing DuckDB design.

Sharding design (option B from the 2026-09-27 design discussion):
- One collection per shard → small per-collection HNSW indexes (~400 MB at
  100 GB / 256 shards) keep inserts and searches fast.
- Parallel queries via ``asyncio.gather`` over all (or one) shard
  collections — the existing ``ShardRouter.get_target_shards(system_id)``
  returns the right list.
- Real tenant isolation (cross-system queries require explicit collection
  enumeration, not just WHERE-clause filter pushdown).

Quantization contract (matches ``WarmRecord`` from ``akosha/storage/models.py``):
``WarmRecord.embedding`` is ``list[int]`` (INT8[384] quantized). When a hot
record ages to warm, the aging service re-quantizes FLOAT→INT8 once at age
time and inserts the INT8 vector verbatim. The pgvector warm collection
has ``vector(384)`` columns typed to ``float[]``; the quantized ints are
stored as ``metadata.quantized=true`` and converted to float on insert
(lossy round-trip matches today's DuckDB-on-disk behavior).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from oneiric.adapters.vector.pgvector import PgvectorAdapter, PgvectorSettings
from oneiric.adapters.vector.vector_types import VectorDocument

from akosha.processing.embedding_dim import resolve_embedding_dim

# NOTE: ``ShardRouter`` is imported lazily inside ``__init__`` to avoid the
# circular import chain ``akosha.config → akosha.storage.__init__ →
# akosha.storage.pgvector_warm_store → akosha.storage.sharding →
# akosha.config``. Same lazy-import pattern used elsewhere in this package.

if TYPE_CHECKING:
    from akosha.models import WarmRecord
    from akosha.storage.sharding import ShardRouter

logger = logging.getLogger(__name__)


def _redact_dsn(msg: str, pg_url: str | None) -> str:
    """Return ``msg`` with the configured DSN (if any) replaced by ``"***"``.

    asyncpg and PgvectorAdapter surface the connection string in many
    exception messages. Logging the raw message persists credentials to
    log sinks. Callers should pass every DSN they hold; this helper is
    a no-op when ``pg_url`` is empty or absent from ``msg``.
    """
    if not msg or not pg_url:
        return msg
    return msg.replace(pg_url, "***")

_COLLECTION_PREFIX = "akosha_warm_shard_"  # akosha_warm_shard_000 ... _255
_DISTANCE_METRIC = "cosine"

#: Sentinel for ``__init__``: ``None`` means "resolve via the embedding-dim
#: contract at construction time". The pre-fix default of 384 is preserved
#: as ``_DEFAULT_LEGACY_DIMENSION`` for callers that pass an explicit 384
#: (e.g. legacy tests).
_DEFAULT_LEGACY_DIMENSION = 384
_EMBEDDING_DIMENSION: int | None = None


def _collection_name(shard_id: int) -> str:
    """Map a shard id to its pgvector collection name."""
    return f"{_COLLECTION_PREFIX}{shard_id:03d}"


class PgvectorWarmStore:
    """Pgvector-backed warm store — mirrors WarmStore interface using Oneiric's PgvectorAdapter.

    Initialize with pgvector settings from config:
        store = PgvectorWarmStore(pg_url="postgresql://localhost:5432/akosha", shard_count=256)
        await store.initialize()
        await store.insert(record)
        results = await store.search_similar(query_embedding, system_id="foo")
    """

    def __init__(
        self,
        pg_url: str,
        *,
        shard_count: int | None = None,
        embedding_dimension: int | None = _EMBEDDING_DIMENSION,
        shard_router: ShardRouter | None = None,
    ) -> None:
        """Initialize PgvectorWarmStore.

        Args:
            pg_url: PostgreSQL connection string (DSN format).
            shard_count: Number of shards to provision. ``None`` (default)
                reads ``akosha.config.config.shard_count`` via the
                default ShardRouter. Tests should pass explicitly.
            embedding_dimension: Embedding vector dimension. ``None``
                (default) resolves via
                :func:`akosha.processing.embedding_dim.resolve_embedding_dim`
                so the pgvector collection dim matches the active
                backend; pass an explicit ``int`` to pin it.
            shard_router: Pre-built ShardRouter instance. When ``None`` a
                router is constructed from ``shard_count``.
        """
        self._pg_url = pg_url
        if embedding_dimension is None:
            self._embedding_dimension: int = resolve_embedding_dim()
        elif embedding_dimension == _DEFAULT_LEGACY_DIMENSION:
            self._embedding_dimension = _DEFAULT_LEGACY_DIMENSION
        else:
            self._embedding_dimension = embedding_dimension
        self._shard_router = shard_router or self._build_default_router(shard_count)
        self._adapters: dict[int, PgvectorAdapter] = {}
        self._init_lock = asyncio.Lock()
        self._initialized = False

    @staticmethod
    def _build_default_router(num_shards: int | None) -> "ShardRouter":
        """Build a ShardRouter from ``akosha.config.config.shard_count`` (lazy).

        Defers the ``akosha.storage.sharding`` import until the first
        ``PgvectorWarmStore`` is constructed — that's after the full
        ``akosha.config`` module has finished loading, which sidesteps
        the circular import (config → storage → pgvector_warm_store →
        sharding → config).
        """
        from akosha.storage.sharding import ShardRouter

        return ShardRouter(num_shards=num_shards)

    async def initialize(self) -> None:
        """Initialize pgvector adapters and ensure each shard's collection exists."""
        async with self._init_lock:
            if self._initialized:
                return
            settings = PgvectorSettings(dsn=self._pg_url)
            for shard_id in range(self._shard_router.num_shards):
                adapter = PgvectorAdapter(settings)
                await adapter.init()
                await adapter.create_collection(
                    name=_collection_name(shard_id),
                    dimension=self._embedding_dimension,
                    distance_metric=_DISTANCE_METRIC,
                )
                self._adapters[shard_id] = adapter
            self._initialized = True
            logger.info(
                "PgvectorWarmStore initialized (shards=%d, dim=%d)",
                self._shard_router.num_shards,
                self._embedding_dimension,
            )

    async def insert(self, record: WarmRecord) -> None:
        """Insert a WarmRecord into the shard collection for ``record.system_id``.

        Args:
            record: WarmRecord with system_id, conversation_id, summary,
                INT8-quantized embedding, timestamp, metadata.

        Raises:
            ValueError: If ``len(record.embedding) != self._embedding_dimension``.
            RuntimeError: If ``initialize()`` has not been called.
        """
        if not self._initialized:
            raise RuntimeError(
                "PgvectorWarmStore not initialized. Call initialize() first."
            )

        actual_dim = len(record.embedding)
        if actual_dim != self._embedding_dimension:
            logger.warning(
                "akosha.pgvector_warm_store.dim_mismatch",
                extra={
                    "expected": self._embedding_dimension,
                    "actual": actual_dim,
                    "conversation_id": record.conversation_id,
                },
            )
            raise ValueError(
                f"PgvectorWarmStore.insert: embedding dim mismatch "
                f"(expected {self._embedding_dimension}, got {actual_dim})"
            )

        shard_id = self._shard_router.get_shard(record.system_id)
        adapter = self._adapters[shard_id]

        # INT8 quantized values are stored as metadata.quantized=true and
        # converted to float[] on insert. The lossy round-trip is by design
        # (matches the existing DuckDB warm-store INT8 storage contract).
        embedding_floats = [int(v) / 127.0 for v in record.embedding]

        doc = VectorDocument(
            id=record.conversation_id,
            metadata={
                "system_id": record.system_id,
                "summary": record.summary,
                "timestamp": record.timestamp.isoformat()
                if hasattr(record.timestamp, "isoformat")
                else str(record.timestamp),
                "quantized": True,
            }
            | dict(record.metadata.items()),
            vector=embedding_floats,
        )
        await adapter.insert(_collection_name(shard_id), [doc])

    async def search_similar(
        self,
        query_embedding: list[float],
        system_id: str | None = None,
        limit: int = 10,
        threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        """Search for similar records across all (or one) shard collections.

        Args:
            query_embedding: Query vector.
            system_id: Optional system filter — restricts the search to the
                single shard owning this system_id, AND applies a metadata
                filter for that system within the shard.
            limit: Maximum results per shard (final results are merged and
                truncated to ``limit``).
            threshold: Minimum similarity score (0-1). Applied as a
                post-query cosine-distance filter, mirroring the hot-tier
                pgvector store's contract.

        Returns:
            List of matching records as dicts sorted by similarity.
        """
        if not self._initialized:
            raise RuntimeError(
                "PgvectorWarmStore not initialized. Call initialize() first."
            )

        target_shards = self._shard_router.get_target_shards(system_id)
        filter_expr = {"system_id": system_id} if system_id else None

        async def _search_one(shard_id: int) -> list[Any]:
            adapter = self._adapters[shard_id]
            return await adapter.search(
                collection=_collection_name(shard_id),
                query_vector=query_embedding,
                limit=limit,
                filter_expr=filter_expr,
                include_vectors=False,
            )

        shard_results = await asyncio.gather(
            *(_search_one(s) for s in target_shards)
        )
        flat = [r for sub in shard_results for r in sub]

        if threshold is not None:
            max_distance = 1.0 - threshold
            flat = [r for r in flat if r.score <= max_distance]

        flat.sort(key=lambda r: r.score)
        top = flat[:limit]

        return [
            {
                "conversation_id": r.id,
                "score": r.score,
                "system_id": r.metadata.get("system_id"),
                "summary": r.metadata.get("summary"),
                "timestamp": r.metadata.get("timestamp"),
                "metadata": {
                    k: v
                    for k, v in r.metadata.items()
                    if k not in ("system_id", "summary", "timestamp", "quantized")
                },
            }
            for r in top
        ]

    async def get_by_id(
        self, conversation_id: str, *, shard_id: int | None = None
    ) -> dict[str, Any] | None:
        """Retrieve a single conversation by ID.

        Searches across shards in parallel with early-exit on first hit.
        Pass ``shard_id`` to query a single known shard (cheaper — avoids
        the cross-shard fan-out amplification).

        Returns:
            Record dict or None if not found.
        """
        if not self._initialized:
            raise RuntimeError(
                "PgvectorWarmStore not initialized. Call initialize() first."
            )

        target_shards: list[int]
        if shard_id is not None:
            target_shards = [shard_id]
        else:
            target_shards = list(self._adapters.keys())

        async def _get_one(shard: int) -> list[Any]:
            adapter = self._adapters[shard]
            return await adapter.get(
                _collection_name(shard), [conversation_id], include_vectors=False
            )

        # NOTE (2026-09-27 security review): ``as_completed`` + break-on-
        # first-hit avoids waiting for every shard's primary-key lookup
        # when the row lives on an early shard. Mitigates the
        # unauthenticated-amplification concern of always hitting all
        # 256 shards when only one is needed. Pending tasks are
        # explicitly cancelled on early-exit so the in-flight coros
        # don't keep running pointlessly (and don't show up as
        # "task exception was never retrieved" warnings in tests).
        tasks = [
            asyncio.create_task(_get_one(s), name=f"pgvector-warm-get-{s}")
            for s in target_shards
        ]
        try:
            for coro in asyncio.as_completed(tasks):
                docs = await coro
                if docs:
                    doc = docs[0]
                    return {
                        "conversation_id": doc.id,
                        "system_id": doc.metadata.get("system_id"),
                        "summary": doc.metadata.get("summary"),
                        "timestamp": doc.metadata.get("timestamp"),
                        "metadata": {
                            k: v
                            for k, v in doc.metadata.items()
                            if k not in (
                                "system_id",
                                "summary",
                                "timestamp",
                                "quantized",
                            )
                        },
                    }
            return None
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            # Drain cancellations so the loop's "Task exception was
            # never retrieved" warning doesn't fire on shutdown.
            for task in tasks:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    async def delete(
        self, conversation_id: str, *, shard_id: int | None = None
    ) -> None:
        """Delete a conversation by ID.

        With no ``shard_id``, fans out across shards in parallel with
        early-exit on first hit. Pass ``shard_id`` to delete from a single
        known shard (cheaper — avoids the cross-shard fan-out).
        """
        if not self._initialized:
            raise RuntimeError(
                "PgvectorWarmStore not initialized. Call initialize() first."
            )

        target_shards: list[int]
        if shard_id is not None:
            target_shards = [shard_id]
        else:
            target_shards = list(self._adapters.keys())

        async def _delete_one(shard: int) -> bool:
            adapter = self._adapters[shard]
            # pgvector's delete returns the count of removed rows; treat
            # any positive count as a hit. The remaining shards share no
            # primary-key namespace, so first-hit is safe to stop on.
            removed = await adapter.delete(
                _collection_name(shard), [conversation_id]
            )
            return bool(removed)

        # Same early-exit pattern as get_by_id: schedule every shard,
        # break on first hit, cancel the rest. See get_by_id for the
        # security rationale (2026-09-27 review).
        tasks = [
            asyncio.create_task(_delete_one(s), name=f"pgvector-warm-delete-{s}")
            for s in target_shards
        ]
        try:
            for coro in asyncio.as_completed(tasks):
                if await coro:
                    return
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            for task in tasks:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    async def close(self) -> None:
        """Close all pgvector adapter connections."""
        for shard_id, adapter in self._adapters.items():
            try:
                await adapter.cleanup()
            except Exception as e:  # pragma: no cover - cleanup best-effort
                # NOTE (2026-09-27 security review): str(e) may contain
                # the connection DSN — redact before logging.
                logger.warning(
                    "akosha.pgvector_warm_store.cleanup_failed",
                    extra={
                        "shard_id": shard_id,
                        "error_type": type(e).__name__,
                        "error": _redact_dsn(str(e), self._pg_url),
                    },
                )
        self._adapters.clear()
        self._initialized = False
        logger.info("PgvectorWarmStore closed")
