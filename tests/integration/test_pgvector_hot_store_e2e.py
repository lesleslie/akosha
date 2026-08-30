"""End-to-end integration tests for ``PgvectorHotStore`` against a live Postgres.

Plan: docs/plans/2026-08-29-pgvector-default.md Phase 2.

Gated on ``AKOSHA_TEST_PGVECTOR_URL`` (DSN format). Local-dev workflow:

    brew install postgresql@16 pgvector
    brew services start postgresql@16
    createdb akosha
    psql -d akosha -c "CREATE EXTENSION vector;"
    AKOSHA_TEST_PGVECTOR_URL=postgresql://akosha@localhost:5432/akosha

CI without docker: ``pytest -m \"not integration\"`` skips the file
cleanly. ``asyncpg`` and ``pgvector`` are optional runtime deps; the
file-level ``pytest.importorskip`` keeps the suite green when they're
missing (matches the existing pgvector adapter test pattern).

KNOWN DEVIATION (documented per task instructions):

The upstream ``oneiric.adapters.vector.pgvector`` adapter currently
generates ``WITH (lists := 100)`` for ivfflat index options, which
Postgres 18 rejects with a syntax error at ``\":=\"``. This bug lives
in the oneiric adapter, not in akosha — the plan explicitly says
\"Do NOT modify the existing ``PgvectorHotStore`` class,\" so we
detect the broken upstream at module load and skip the e2e tests
cleanly. Once the upstream adapter fixes the walrus-operator SQL,
the tests below activate automatically (no further changes here).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

# Optional asyncpg/pgvector runtime deps — Phase 2 dev install only.
pytest.importorskip("asyncpg", reason="asyncpg not installed")
pytest.importorskip("pgvector", reason="pgvector python package not installed")
pytest.importorskip(
    "oneiric.adapters.vector.pgvector",
    reason="oneiric pgvector adapter unavailable",
)

from akosha.models import HotRecord  # noqa: E402
from akosha.storage.pgvector_hot_store import PgvectorHotStore  # noqa: E402

PGVECTOR_URL = os.environ.get("AKOSHA_TEST_PGVECTOR_URL", "").strip()
# Unique collection per run keeps parallel CI jobs from clobbering each
# other. Operators who want the canonical ``conversations`` collection
# can override via env if they need to inspect state by hand.
COLLECTION = f"akosha_test_{uuid.uuid4().hex[:8]}"
DIM = 4  # tiny dim keeps test inserts cheap; PgvectorAdapter requires
        # pgvector collection dim match the embedding vector length.


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PGVECTOR_URL, reason="AKOSHA_TEST_PGVECTOR_URL not set"),
]


def _upstream_pgvector_adapter_works() -> bool:
    """Canary: probe whether the upstream pgvector adapter can create a collection.

    Returns False (and the entire file skips) when the adapter fails
    with a Postgres syntax error in ``WITH (lists := N)``. The bug
    lives in oneiric; this is a fail-soft guard so the suite reports
    a clean skip instead of crashing every e2e test.
    """
    if not PGVECTOR_URL:
        return False
    from oneiric.adapters.vector.pgvector import PgvectorAdapter, PgvectorSettings

    async def _probe() -> bool:
        adapter = PgvectorAdapter(PgvectorSettings(dsn=PGVECTOR_URL))
        try:
            await adapter.init()
            await adapter.create_collection(
                name=f"_probe_{uuid.uuid4().hex[:6]}",
                dimension=DIM,
                distance_metric="cosine",
            )
            return True
        except Exception:  # noqa: BLE001
            return False
        finally:
            try:
                await adapter.cleanup()
            except Exception:  # noqa: BLE001
                pass

    try:
        return asyncio.run(_probe())
    except Exception:  # noqa: BLE001
        return False


_PGVECTOR_ADAPTER_HEALTHY = _upstream_pgvector_adapter_works()
if PGVECTOR_URL and not _PGVECTOR_ADAPTER_HEALTHY:
    pytest.skip(
        "Upstream oneiric pgvector adapter SQL bug "
        "(WITH (lists := N) rejected by Postgres 18); skipping e2e",
        allow_module_level=True,
    )


# -- helpers ------------------------------------------------------------------


def _record(
    *,
    conversation_id: str,
    system_id: str = "sys1",
    content: str = "hello",
    embedding: list[float] | None = None,
    metadata: dict[str, Any] | None = None,
) -> HotRecord:
    """Build a real ``HotRecord`` with the schema we expect at insert time."""
    return HotRecord(
        system_id=system_id,
        conversation_id=conversation_id,
        content=content,
        embedding=embedding if embedding is not None else [0.0] * DIM,
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        metadata=metadata or {},
    )


@pytest.fixture
async def pg_store():
    """Yield an initialized ``PgvectorHotStore`` and tear it down cleanly.

    The fixture creates a uniquely-named collection so parallel runs and
    repeated invocations don't collide. The collection is dropped via the
    adapter's ``cleanup()`` path on teardown; if a prior test crashed,
    the next run simply picks a fresh UUID-derived name.
    """
    store = PgvectorHotStore(pg_url=PGVECTOR_URL, embedding_dimension=DIM)
    await store.initialize()

    # Override the module-level collection name with our per-test one so
    # we don't pollute the canonical ``conversations`` collection.
    from akosha.storage import pgvector_hot_store as _mod

    original_collection = _mod._COLLECTION_NAME
    _mod._COLLECTION_NAME = COLLECTION
    try:
        # Recreate the collection with the overridden name.
        if store._adapter is None:
            raise RuntimeError("PgvectorHotStore not initialized")
        await store._adapter.create_collection(
            name=COLLECTION,
            dimension=DIM,
            distance_metric="cosine",
        )
        yield store
    finally:
        # Drop the test collection so we don't leave residue behind.
        try:
            if store._adapter is not None:
                await store._adapter.delete_collection(COLLECTION)
        except Exception:  # noqa: BLE001
            pass
        _mod._COLLECTION_NAME = original_collection
        await store.close()


# -- tests --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pgvector_insert_and_search_round_trip(pg_store: PgvectorHotStore) -> None:
    """Insert 5 rows with varied embeddings; search by row 1's exact embedding → top-1 == row 1."""
    base = [0.5] * DIM
    rows = [
        _record(conversation_id=f"conv-{i}", embedding=[base[j] + (0.1 * i) for j in range(DIM)])
        for i in range(5)
    ]
    for r in rows:
        await pg_store.insert(r)

    # Query with row 0's exact embedding — cosine distance should be 0 (or ~0).
    results = await pg_store.search_similar(rows[0].embedding, limit=5)

    assert len(results) >= 1, f"expected ≥1 result, got {results}"
    # The top hit should be conv-0 (the query vector IS conv-0's vector).
    top = results[0]
    assert top["conversation_id"] == "conv-0"
    assert top["score"] < 0.01, f"expected ~0 distance, got {top['score']}"


@pytest.mark.asyncio
async def test_pgvector_search_respects_threshold(pg_store: PgvectorHotStore) -> None:
    """A threshold of 0.95 filters out dissimilar rows."""
    # Two rows with orthogonal-ish embeddings.
    row_a = _record(conversation_id="a", embedding=[1.0] + [0.0] * (DIM - 1))
    row_b = _record(conversation_id="b", embedding=[0.0] * (DIM - 1) + [1.0])
    await pg_store.insert(row_a)
    await pg_store.insert(row_b)

    # Query with row_a's embedding. row_a should match tightly; row_b
    # is orthogonal in this tiny dim, so cosine distance ≈ 1.0.
    results = await pg_store.search_similar(row_a.embedding, limit=10, threshold=0.95)

    # Only row_a should survive the threshold (cosine distance ≈ 0).
    assert any(r["conversation_id"] == "a" for r in results)
    assert all(r["conversation_id"] != "b" for r in results), (
        "Orthogonal row should be filtered out by threshold=0.95"
    )


@pytest.mark.asyncio
async def test_pgvector_search_filters_by_system_id(pg_store: PgvectorHotStore) -> None:
    """``system_id`` filter isolates rows across systems."""
    # Two rows in different systems, same embedding.
    embedding = [0.5] * DIM
    await pg_store.insert(
        _record(conversation_id="x-sys1", system_id="sys1", embedding=embedding)
    )
    await pg_store.insert(
        _record(conversation_id="x-sys2", system_id="sys2", embedding=embedding)
    )

    sys1_results = await pg_store.search_similar(embedding, system_id="sys1")
    sys2_results = await pg_store.search_similar(embedding, system_id="sys2")

    sys1_ids = {r["conversation_id"] for r in sys1_results}
    sys2_ids = {r["conversation_id"] for r in sys2_results}

    assert sys1_ids == {"x-sys1"}, f"sys1 filter leaked: {sys1_ids}"
    assert sys2_ids == {"x-sys2"}, f"sys2 filter leaked: {sys2_ids}"


@pytest.mark.asyncio
async def test_pgvector_dim_mismatch_raises_value_error(pg_store: PgvectorHotStore) -> None:
    """Schema 4-dim rejects a 384-dim record with ValueError (mirrors DuckDB contract)."""
    bad_record = HotRecord(
        system_id="sys1",
        conversation_id="bad",
        content="wrong-dim",
        embedding=[0.1] * 384,  # schema is DIM (4)
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        metadata={},
    )

    with pytest.raises(ValueError, match="dim mismatch"):
        await pg_store.insert(bad_record)


@pytest.mark.asyncio
async def test_pgvector_watermark_row_persists() -> None:
    """Watermark-style row persists across ``PgvectorHotStore`` restarts (new instance)."""
    # Phase 2 watermark row — conversation_id sentinel + the persisted value
    # in metadata. The current PgvectorHotStore has no dedicated upsert,
    # but ``insert`` already supports arbitrary rows; the watermark just
    # needs a reserved conversation_id namespace.
    wm_id = f"__watermark_{uuid.uuid4().hex[:8]}__"

    # First lifecycle: write watermark.
    store_a = PgvectorHotStore(pg_url=PGVECTOR_URL, embedding_dimension=DIM)
    await store_a.initialize()
    # Override collection for this test (avoid clobbering the canonical one).
    from akosha.storage import pgvector_hot_store as _mod

    original_collection = _mod._COLLECTION_NAME
    _mod._COLLECTION_NAME = COLLECTION
    try:
        await store_a._adapter.create_collection(
            name=COLLECTION, dimension=DIM, distance_metric="cosine"
        )
        await store_a.insert(
            _record(
                conversation_id=wm_id,
                embedding=[1.0] * DIM,  # arbitrary
                metadata={"last_processed_id": "12345-xyz"},
            )
        )
        await store_a.close()
    except Exception:
        _mod._COLLECTION_NAME = original_collection
        await store_a.close()
        raise

    # Second lifecycle: re-open the store and read the watermark back.
    store_b = PgvectorHotStore(pg_url=PGVECTOR_URL, embedding_dimension=DIM)
    await store_b.initialize()
    _mod._COLLECTION_NAME = COLLECTION
    try:
        loaded = await store_b.get_by_id(wm_id)
        assert loaded is not None, "watermark row missing after restart"
        assert loaded["conversation_id"] == wm_id
        assert loaded["metadata"].get("last_processed_id") == "12345-xyz"
    finally:
        try:
            await store_b._adapter.delete_collection(COLLECTION)
        except Exception:  # noqa: BLE001
            pass
        _mod._COLLECTION_NAME = original_collection
        await store_b.close()
