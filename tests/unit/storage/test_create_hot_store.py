"""Tests for ``akosha.storage.create_hot_store``.

Pins the post-2026-09-27 factory contract:

- ``create_hot_store()`` (no args) returns an in-memory ``HotStore``.
- ``backend="duckdb-ssd"`` returns an on-disk ``HotStore``.
- ``database_path`` is forwarded to the ``HotStore`` ctor.

NOTE (2026-09-27 audit): the pgvector backend was removed from the hot
tier; pgvector is now a warm-tier-only backend via ``create_warm_store``.
The legacy ``pg_url`` kwarg and the ``AKOSHA__STORAGE__HOT__PG_URL`` env
var were removed in the same pass — see ``akosha/storage/__init__.py``
for the migration path. The old test cases that exercised the pgvector
hot path (Phase 1 of ``docs/plans/2026-08-29-pgvector-default.md``) are
deleted; warm-tier pgvector coverage now lives in
``tests/unit/test_pgvector_warm_store.py``.
"""

from __future__ import annotations

import pytest

from akosha.storage import create_hot_store
from akosha.storage.hot_store import HotStore


pytestmark = pytest.mark.unit


def test_create_hot_store_defaults_to_duckdb_memory() -> None:
    """No args → in-memory DuckDB ``HotStore``."""
    store = create_hot_store()

    assert isinstance(store, HotStore)
    assert store.db_path == ":memory:"
    assert store._embedding_dim == 384


def test_create_hot_store_with_duckdb_ssd_backend() -> None:
    """``backend='duckdb-ssd'`` → on-disk ``HotStore`` (caller passes ``database_path``)."""
    store = create_hot_store(
        backend="duckdb-ssd",
        database_path="/tmp/akosha-explicit.db",
    )

    assert isinstance(store, HotStore)
    assert store.db_path == "/tmp/akosha-explicit.db"


def test_create_hot_store_with_explicit_database_path() -> None:
    """``database_path`` is forwarded to the ``HotStore`` ctor."""
    store = create_hot_store(
        backend="duckdb-memory",
        database_path="/tmp/akosha-explicit.db",
    )

    assert isinstance(store, HotStore)
    assert store.db_path == "/tmp/akosha-explicit.db"


def test_create_hot_store_rejects_pgvector_kwarg() -> None:
    """The legacy ``pg_url`` kwarg was removed — the call must TypeError.

    Operators who previously set ``backend='pgvector'`` on the hot tier
    should migrate to ``create_warm_store(backend='pgvector', pg_url=...)``.
    """
    with pytest.raises(TypeError):
        create_hot_store(  # type: ignore[call-arg]
            backend="duckdb-memory",
            pg_url="postgresql://legacy",
        )
