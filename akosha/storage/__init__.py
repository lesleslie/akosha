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
from akosha.storage.pgvector_hot_store import PgvectorHotStore
from akosha.storage.warm_store import WarmStore

logger = logging.getLogger(__name__)

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
    backend: str = "duckdb-memory",
    pg_url: str = "",
    embedding_dim: int | None = None,
    database_path: str = ":memory:",
) -> HotStore | PgvectorHotStore:
    """Create a hot store instance based on the resolved backend.

    Plan: docs/plans/2026-08-29-pgvector-default.md Phase 1.

    Args:
        backend: Storage backend selector. One of ``"duckdb-memory"`` (default)
            or ``"pgvector"``. ``AkoshaApplication.start()`` reads this from
            the ``hot_store.backend`` block in ``settings/akosha.yaml`` and
            passes it through; tests should pass it explicitly.
        pg_url: PostgreSQL connection string for the pgvector backend.
            Required when ``backend="pgvector"``. Falls back to the
            ``AKOSHA__STORAGE__HOT__PG_URL`` env var when the kwarg is
            empty (preserved for Phase 2 callers; the env-var fallback is
            scheduled for removal once ``start()`` always threads the
            value from the YAML config).
        embedding_dim: Optional embedding vector dimension to thread
            through to the underlying store. ``None`` lets the store
            resolve via the embedding-dim contract (defaulting to 384).
            Pass an explicit ``int`` when the embedding backend's dim is
            already known (e.g. from ``AkoshaApplication.start``).
        database_path: DuckDB database path. Defaults to ``":memory:"``
            so tests and local dev never accidentally write to disk;
            ignored when ``backend="pgvector"``.

    Returns:
        ``PgvectorHotStore`` when ``backend == "pgvector"`` and a pg_url
        is available. Otherwise ``HotStore`` (DuckDB in-memory).
        When ``backend="pgvector"`` is requested but no pg_url is set,
        logs a WARNING and falls back to the in-memory ``HotStore`` —
        fail-soft so a missing config never silently breaks ``start()``.
    """
    # TODO: Phase 2 cleanup — drop the AKOSHA__STORAGE__HOT__PG_URL env-var
    # fallback once ``akosha/main.py:start()`` always threads the value
    # from ``settings/akosha.yaml`` and operators are migrated off the
    # env-var path.
    resolved_pg_url = pg_url or os.getenv("AKOSHA__STORAGE__HOT__PG_URL", "")

    if backend == "pgvector":
        if not resolved_pg_url:
            logger.warning(
                "akosha.hot_store.pg_url_missing: "
                "backend=pgvector requested but no pg_url provided "
                "(pass via create_hot_store(pg_url=...) or "
                "AKOSHA__STORAGE__HOT__PG_URL); falling back to in-memory DuckDB"
            )
            return HotStore(database_path=database_path, embedding_dim=embedding_dim)
        return PgvectorHotStore(
            pg_url=resolved_pg_url,
            embedding_dimension=embedding_dim,
        )

    return HotStore(database_path=database_path, embedding_dim=embedding_dim)
