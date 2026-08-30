"""Akosha storage layer."""

from __future__ import annotations

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
from akosha.storage.pgvector_hot_store import PgvectorHotStore
from akosha.storage.warm_store import WarmStore

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
    "PgvectorHotStore",
    "StoragePathResolver",
    "SystemMemoryUpload",
    "WarmRecord",
    "WarmStore",
    "create_hot_store",
    "get_config_dir",
    "get_default_resolver",
    "get_warm_store_path",
]


def create_hot_store(
    pg_url: str = "",
    embedding_dim: int | None = None,
) -> HotStore | PgvectorHotStore:
    """Create a hot store instance based on AKOSHA__STORAGE__HOT__BACKEND env var.

    Args:
        pg_url: PostgreSQL connection string for pgvector backend.
                Can also be set via AKOSHA__STORAGE__HOT__PG_URL env var.
        embedding_dim: Optional embedding vector dimension to thread
            through to the underlying store. ``None`` lets the store
            resolve via the embedding-dim contract (defaulting to 384).
            Pass an explicit ``int`` when the embedding backend's dim is
            already known (e.g. from ``AkoshaApplication.start``).

    Returns:
        PgvectorHotStore when AKOSHA__STORAGE__HOT__BACKEND=pgvector and pg_url is set,
        otherwise HotStore (DuckDB in-memory).
    """
    backend = os.getenv("AKOSHA__STORAGE__HOT__BACKEND", "")
    resolved_pg_url = pg_url or os.getenv("AKOSHA__STORAGE__HOT__PG_URL", "")

    if backend == "pgvector" and resolved_pg_url:
        return PgvectorHotStore(
            pg_url=resolved_pg_url,
            embedding_dimension=embedding_dim,
        )

    return HotStore(database_path=":memory:", embedding_dim=embedding_dim)
