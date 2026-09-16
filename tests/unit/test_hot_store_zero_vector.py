"""Regression test: HotStore.search_similar zero-vector fallback.

Pre-fix: a zero query embedding produced NULL cosine similarities
against every row (``0 / 0 = NaN`` in DuckDB), and ``NULL >=
threshold`` filtered them all out. The substrate silently returned
``[]``, breaking callers like
:func:`akosha.ingestion.bodai_event_subscriber._read_watermark_async`
which probes with ``[0.0] * embedding_dim`` to scan for the
watermark conversation.

Post-fix: HotStore.search_similar detects zero vectors and routes
to a timestamp-ordered scan with ``similarity=None``. This test
pins the fallback contract so future substrate changes cannot
silently regress it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from akosha.storage.hot_store import _is_zero_vector, HotStore


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------


def test_is_zero_vector_returns_true_for_all_zeros() -> None:
    """A vector of all zeros is detected as a zero vector."""
    assert _is_zero_vector([0.0] * 8) is True
    assert _is_zero_vector([0.0, 0.0, 0.0]) is True


def test_is_zero_vector_returns_false_for_nonzero() -> None:
    """Any nonzero component disqualifies the vector as zero."""
    assert _is_zero_vector([0.0, 0.1, 0.0]) is False
    assert _is_zero_vector([1.0, 0.0, 0.0]) is False
    assert _is_zero_vector([0.5, 0.5, 0.5]) is False


def test_is_zero_vector_tolerates_submachine_epsilon() -> None:
    """Components under ``1e-10`` are still treated as zero.

    This prevents legitimate near-zero embeddings from triggering
    the timestamp-scan fallback. The threshold matches the
    pre-Phase-5 akosha heuristic.
    """
    assert _is_zero_vector([1e-11] * 4) is True
    assert _is_zero_vector([1e-9] * 4) is False


# ---------------------------------------------------------------------------
# search_similar fallback path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_similar_zero_vector_falls_back_to_recent_scan() -> None:
    """Zero query → timestamp-ordered scan, NOT empty result set.

    Pre-fix this returned ``[]`` (substrate's NULL-filtered cosine
    similarity). Post-fix it returns the most-recent ``limit`` rows
    with ``similarity=None``.
    """
    store = HotStore(database_path=":memory:", embedding_dim=3)
    store._lock = MagicMock()  # AsyncMock-friendly wrapper
    store._lock.__aenter__ = AsyncMock(return_value=store._lock)
    store._lock.__aexit__ = AsyncMock(return_value=None)

    # Stub the connection with a preloaded scan result. The
    # substrate's cosine-similarity path is bypassed because the
    # override short-circuits on zero vector BEFORE delegating.
    recent_rows = [
        ("sys_a", "conv_1", "first", "2026-09-16 12:00:00", "{}"),
        ("sys_a", "conv_2", "second", "2026-09-16 11:00:00", "{}"),
        ("sys_b", "conv_3", "third", "2026-09-16 10:00:00", "{}"),
    ]
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchall.return_value = recent_rows
    store.conn = fake_conn

    results = await store.search_similar(
        query_embedding=[0.0, 0.0, 0.0],
        limit=10,
    )

    # All three rows surface; substrate's cosine-similarity path
    # was bypassed.
    assert len(results) == 3
    # similarity=None for every row (cosine sim is undefined for
    # the zero probe vector).
    assert all(r["similarity"] is None for r in results)
    # The fallback SQL is the timestamp-ordered scan, NOT the
    # substrate's cosine-similarity path. Verify by inspecting the
    # SQL text — must NOT contain ``list_cosine_similarity``.
    sql = fake_conn.execute.call_args[0][0]
    assert "list_cosine_similarity" not in sql, (
        "zero-vector fallback must bypass substrate's "
        "cosine-similarity SQL; got: " + sql
    )
    assert "ORDER BY timestamp DESC" in sql


@pytest.mark.asyncio
async def test_search_similar_zero_vector_filters_by_system_id() -> None:
    """Zero-vector fallback honors ``system_id`` filter.

    The substrate path passes system_id through to the SQL WHERE
    clause; the fallback must do the same so callers can scope
    their watermark probe to a single component.
    """
    store = HotStore(database_path=":memory:", embedding_dim=3)
    store._lock = MagicMock()
    store._lock.__aenter__ = AsyncMock(return_value=store._lock)
    store._lock.__aexit__ = AsyncMock(return_value=None)

    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchall.return_value = []
    store.conn = fake_conn

    await store.search_similar(
        query_embedding=[0.0, 0.0, 0.0],
        system_id="sys_a",
        limit=5,
    )

    sql = fake_conn.execute.call_args[0][0]
    params = fake_conn.execute.call_args[0][1]
    assert "system_id = ?" in sql
    assert "sys_a" in params
    assert 5 in params  # LIMIT bound


@pytest.mark.asyncio
async def test_search_similar_nonzero_delegates_to_substrate() -> None:
    """Nonzero query → substrate path; fallback NOT triggered.

    Regression guard: the zero-vector check must NOT short-circuit
    the common case. A nonzero query should still hit the
    substrate's cosine-similarity SQL.
    """
    substrate_call = AsyncMock(return_value=[{"score": 0.95}])
    store = HotStore(database_path=":memory:", embedding_dim=3)
    store._lock = MagicMock()
    store._lock.__aenter__ = AsyncMock(return_value=store._lock)
    store._lock.__aexit__ = AsyncMock(return_value=None)
    # Patch super().search_similar via a fake connection. Since
    # ``super()`` is bound at class-definition time, swap
    # ``DuckdbHotStore.search_similar`` in the MRO instead.
    from oneiric.adapters.vector.duckdb_hot_store import DuckdbHotStore
    original = DuckdbHotStore.search_similar
    DuckdbHotStore.search_similar = substrate_call  # type: ignore[method-assign]
    try:
        results = await store.search_similar(
            query_embedding=[0.1, 0.2, 0.3],
            limit=10,
        )
    finally:
        DuckdbHotStore.search_similar = original  # type: ignore[method-assign]

    # Substrate was called exactly once.
    assert substrate_call.await_count == 1
    # Result has the AkoSHA similarity alias.
    assert results == [{"score": 0.95, "similarity": 0.95}]
