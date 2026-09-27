"""Akosha storage layer."""

from __future__ import annotations

import logging
import os

from akosha.storage.aging import AgingService, MigrationStats
from akosha.storage.cold_store import ColdStore
from akosha.storage.hot_store import HotStore
from akosha.storage.models import (
    CodeGraphMetadata,
    ColdRecord,
    ConversationMetadata,
    HotRecord,
    IngestionStats,
    SystemMemoryUpload,
    WarmRecord,
)
from akosha.storage.path_resolver import (
    StoragePathResolver,
    get_config_dir,
    get_default_resolver,
    get_warm_store_path,
)
from akosha.storage.pgvector_warm_store import PgvectorWarmStore
from akosha.storage.warm_store import WarmStore

logger = logging.getLogger(__name__)

# NOTE (2026-09-27 audit): ``PgvectorHotStore`` was the hot-tier pgvector
# backend. pgvector moved to the warm tier as part of the storage-allowlist
# cleanup (the hot tier's read-only-in-memory contract and pgvector's
# network-attached cost made pgvector a poor fit for hot). Use
# ``PgvectorWarmStore`` (one collection per shard, see
# ``akosha/storage/pgvector_warm_store.py``) for the warm-tier pgvector
# backend. The class itself is retained at
# ``akosha/storage/pgvector_hot_store.py`` only as a deprecation stub so
# external imports keep resolving; remove after 2026-Q4.

__all__ = [
    "AgingService",
    "CodeGraphMetadata",
    "ColdRecord",
    "ColdStore",
    "ConversationMetadata",
    "HotRecord",
    "HotStore",
    "IngestionStats",
    "MigrationStats",
    "PgvectorWarmStore",
    "StoragePathResolver",
    "SystemMemoryUpload",
    "WarmRecord",
    "WarmStore",
    "create_hot_store",
    "create_warm_store",
    "get_config_dir",
    "get_default_resolver",
    "get_warm_store_path",
]


def create_hot_store(
    backend: str = "duckdb-memory",
    embedding_dim: int | None = None,
    database_path: str = ":memory:",
) -> HotStore:
    """Create a hot store instance.

    The hot tier accepts only DuckDB-backed implementations (``duckdb-memory``
    for the zero-dep default, ``duckdb-ssd`` for on-disk). pgvector was a
    historical hot-tier backend and was removed 2026-09-27 — see the
    ``HotStorageConfig`` docstring for the migration path.

    Args:
        backend: Storage backend selector. ``"duckdb-memory"`` (default) or
            ``"duckdb-ssd"``. ``AkoshaApplication.start()`` reads this from
            the ``hot.backend`` block in ``settings/akosha.yaml`` and
            passes it through; tests should pass it explicitly.
        embedding_dim: Optional embedding vector dimension to thread
            through to the underlying store. ``None`` lets the store
            resolve via the embedding-dim contract (defaulting to 384).
            Pass an explicit ``int`` when the embedding backend's dim is
            already known (e.g. from ``AkoshaApplication.start``).
        database_path: DuckDB database path. Defaults to ``":memory:"``
            so tests and local dev never accidentally write to disk.

    Returns:
        ``HotStore`` (DuckDB-backed).
    """
    return HotStore(database_path=database_path, embedding_dim=embedding_dim)


def create_warm_store(
    backend: str = "duckdb-ssd",
    pg_url: str = "",
    shard_count: int | None = None,
    database_path: "os.PathLike[str] | None" = None,
) -> WarmStore | PgvectorWarmStore:
    """Create a warm store instance based on the resolved backend.

    Args:
        backend: Storage backend selector. One of ``"duckdb-ssd"`` (default),
            ``"duckdb-hdd"``, or ``"pgvector"``. ``AkoshaApplication.start()``
            reads this from the ``warm.backend`` block in
            ``settings/akosha.yaml`` and passes it through.
        pg_url: PostgreSQL connection string for the pgvector backend.
            Required when ``backend="pgvector"``. Falls back to the
            ``AKOSHA__STORAGE__WARM__PG_URL`` env var when the kwarg is
            empty.
        shard_count: Number of shards for the pgvector warm store.
            ``None`` reads ``akosha.config.config.shard_count``. Ignored
            when ``backend != "pgvector"``.
        database_path: Filesystem path for the DuckDB warm store. ``None``
            resolves to ``akosha.config.config.warm.path``. Ignored when
            ``backend == "pgvector"``.

    Returns:
        ``PgvectorWarmStore`` when ``backend == "pgvector"`` and a pg_url
        is available. Otherwise ``WarmStore`` (DuckDB on-disk).
        When ``backend="pgvector"`` is requested but no pg_url is set,
        logs a WARNING and falls back to the on-disk ``WarmStore`` —
        fail-soft so a missing config never silently breaks ``start()``.
    """
    if backend == "pgvector":
        resolved_pg_url = pg_url or os.getenv("AKOSHA__STORAGE__WARM__PG_URL", "")
        if not resolved_pg_url:
            logger.warning(
                "akosha.warm_store.pg_url_missing: "
                "backend=pgvector requested but no pg_url provided "
                "(pass via create_warm_store(pg_url=...) or "
                "AKOSHA__STORAGE__WARM__PG_URL); falling back to duckdb-ssd"
            )
            return WarmStore(database_path=database_path)
        return PgvectorWarmStore(
            pg_url=resolved_pg_url,
            shard_count=shard_count,
        )

    return WarmStore(database_path=database_path)
