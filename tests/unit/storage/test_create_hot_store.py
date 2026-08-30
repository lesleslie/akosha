"""Tests for ``akosha.storage.create_hot_store``.

Plan: docs/plans/2026-08-29-pgvector-default.md Phase 1.

Pins the factory contract:

- ``backend="duckdb-memory"`` (default) returns an in-memory ``HotStore``.
- ``backend="pgvector"`` with a ``pg_url`` returns a ``PgvectorHotStore``.
- ``backend="pgvector"`` with no ``pg_url`` falls back to ``HotStore`` and
  logs a WARNING (fail-soft).
- The ``AKOSHA__STORAGE__HOT__PG_URL`` env var is honored when ``pg_url``
  is empty (Phase 2 cleanup target — preserved for back-compat callers).
"""

from __future__ import annotations

import logging

import pytest

from akosha.storage import create_hot_store
from akosha.storage.hot_store import HotStore
from akosha.storage.pgvector_hot_store import PgvectorHotStore


pytestmark = pytest.mark.unit


def test_create_hot_store_defaults_to_duckdb_memory() -> None:
    """No args → in-memory DuckDB ``HotStore``."""
    store = create_hot_store()

    assert isinstance(store, HotStore)
    assert store.db_path == ":memory:"
    assert store._embedding_dim == 384


def test_create_hot_store_with_pgvector_backend_and_pg_url() -> None:
    """``backend='pgvector'`` + ``pg_url`` → ``PgvectorHotStore``."""
    store = create_hot_store(
        backend="pgvector",
        pg_url="postgresql://akosha@localhost:5432/akosha",
    )

    assert isinstance(store, PgvectorHotStore)
    assert store._pg_url == "postgresql://akosha@localhost:5432/akosha"


def test_create_hot_store_with_pgvector_backend_no_pg_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``backend='pgvector'`` without ``pg_url`` → falls back to ``HotStore`` + WARNING."""
    with caplog.at_level(logging.WARNING, logger="akosha.storage"):
        store = create_hot_store(backend="pgvector", pg_url="")

    assert isinstance(store, HotStore)
    assert store.db_path == ":memory:"
    assert any(
        "akosha.hot_store.pg_url_missing" in record.getMessage()
        for record in caplog.records
    ), "Expected a WARNING tagged 'akosha.hot_store.pg_url_missing'"


def test_create_hot_store_with_explicit_database_path() -> None:
    """``database_path`` is forwarded to the ``HotStore`` ctor."""
    store = create_hot_store(
        backend="duckdb-memory",
        database_path="/tmp/akosha-explicit.db",
    )

    assert isinstance(store, HotStore)
    assert store.db_path == "/tmp/akosha-explicit.db"


def test_create_hot_store_reads_pg_url_from_env_when_kwarg_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty ``pg_url`` kwarg + ``AKOSHA__STORAGE__HOT__PG_URL`` env → use env value."""
    monkeypatch.setenv(
        "AKOSHA__STORAGE__HOT__PG_URL",
        "postgresql://envhost:5432/envdb",
    )

    store = create_hot_store(backend="pgvector", pg_url="")

    assert isinstance(store, PgvectorHotStore)
    assert store._pg_url == "postgresql://envhost:5432/envdb"


def test_create_hot_store_kwarg_pg_url_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit kwarg wins over the env-var fallback (Phase 2 cleanup guard)."""
    monkeypatch.setenv(
        "AKOSHA__STORAGE__HOT__PG_URL",
        "postgresql://envhost:5432/envdb",
    )

    store = create_hot_store(
        backend="pgvector",
        pg_url="postgresql://kwarghost:5432/kwargdb",
    )

    assert isinstance(store, PgvectorHotStore)
    assert store._pg_url == "postgresql://kwarghost:5432/kwargdb"
