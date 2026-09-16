"""Regression tests for the DuckDB HNSW index creation bug.

Phase 5 of ``docs/plans/2026-09-14-common-mcp-client-transport-unification.md``
(also tracked separately in
``docs/followups/2026-09-14-akosha-hnsw-on-duckdb.md``): the prior
``HotStore.initialize()`` attempted
``CREATE INDEX ... USING HNSW (embedding)`` against DuckDB, which has
no native HNSW support. The resulting ``Binder Error: Unknown index
type: HNSW`` fired on every poll cycle (5x per akosha cycle, 5x per
kg_refresh, 5x per OTel ingester) and produced no useful index.

The fix removes the always-failing HNSW attempt and logs a single
INFO line explaining the DuckDB situation. Vector similarity queries
continue to use ``array_cosine_similarity(...)`` (brute-force scan,
no index needed) — operators who want ANN acceleration can install
the ``vss`` community extension manually.

These tests pin:

1. The HNSW ``Binder Error`` is no longer logged at any level during
   ``HotStore.initialize()``.
2. A single INFO line surfaces the new behavior so operators can
   understand why no vector index is configured.
3. ``search_similar()`` still works — the vector similarity path is
   unaffected by removing the (non-existent) index.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from akosha.storage.hot_store import HotStore
from akosha.storage.models import HotRecord


def _make_store(tmp_path: Path) -> HotStore:
    """Build a HotStore against an ephemeral :memory: DuckDB."""
    store = HotStore(database_path=str(tmp_path / "test.duckdb"), embedding_dim=4)
    return store


async def _make_record(embedding: list[float], system_id: str = "test") -> dict:
    """A canned conversation record for ``store.insert()``."""
    return {
        "system_id": system_id,
        "conversation_id": f"conv-{system_id}-{len(embedding)}",
        "content": "test content",
        "embedding": embedding,
        "timestamp": "2026-09-15T00:00:00+00:00",
        "metadata": {"test": True},
        "content_hash": "deadbeef",
    }


@pytest.mark.asyncio
async def test_initialize_does_not_emit_hnsw_binder_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Phase 5 fix: ``Binder Error: Unknown index type: HNSW`` is gone.

    Prior to the fix, every ``HotStore.initialize()`` logged a WARNING
    containing ``"HNSW index creation failed: Binder Error: Unknown
    index type: HNSW"``. The fix removes the always-failing
    ``CREATE INDEX ... USING HNSW`` attempt entirely; we assert no
    log message containing ``"HNSW index creation failed"`` is emitted
    at any level.
    """
    store = _make_store(tmp_path)

    with caplog.at_level(logging.WARNING, logger="akosha.storage.hot_store"):
        await store.initialize()

    bad = [
        record for record in caplog.records
        if "HNSW index creation failed" in record.getMessage()
        or "Unknown index type: HNSW" in record.getMessage()
    ]
    assert bad == [], (
        f"HNSW Binder Error surfaced during initialize(): "
        f"{[r.getMessage() for r in bad]}"
    )

    # Cleanup so the ephemeral file is released.
    await store.close()


@pytest.mark.asyncio
async def test_initialize_emits_info_explaining_no_hnsw(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The fix surfaces a single INFO line so operators can see why no index.

    Without this message, operators reading ``akosha.storage.hot_store``
    logs would have no signal that vector search uses brute-force
    scanning. The new INFO line names the alternative (``vss``
    extension) for operators who need ANN acceleration.
    """
    store = _make_store(tmp_path)

    with caplog.at_level(logging.INFO, logger="akosha.storage.hot_store"):
        await store.initialize()

    info_lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("skipping HNSW index" in line for line in info_lines), (
        f"expected an INFO line explaining the HNSW skip; got: {info_lines}"
    )
    assert any("vss" in line for line in info_lines), (
        f"expected the INFO line to mention the ``vss`` extension as "
        f"the DuckDB ANN alternative; got: {info_lines}"
    )

    await store.close()


@pytest.mark.asyncio
async def test_search_similar_unaffected_by_hnsw_removal(
    tmp_path: Path,
) -> None:
    """``search_similar()`` continues to work — it never depended on HNSW.

    Vector similarity uses ``array_cosine_similarity(...)``, a DuckDB
    array function that scans rows brute-force. The removed HNSW
    index was never functional on DuckDB, so removing the create
    attempt does not change query behavior. This test pins that.
    """
    store = _make_store(tmp_path)
    await store.initialize()
    try:
        # Insert a couple of records (HotRecord objects).
        await store.insert(
            HotRecord(
                system_id="test",
                conversation_id="a",
                content="first",
                embedding=[1.0, 0.0, 0.0, 0.0],
                timestamp=datetime(2026, 9, 15, 0, 0, 0, tzinfo=UTC),
                metadata={"k": "v"},
                content_hash="deadbeef-a",
            )
        )
        await store.insert(
            HotRecord(
                system_id="test",
                conversation_id="b",
                content="second",
                embedding=[0.0, 1.0, 0.0, 0.0],
                timestamp=datetime(2026, 9, 15, 0, 1, 0, tzinfo=UTC),
                metadata={"k": "v"},
                content_hash="deadbeef-b",
            )
        )

        # Query with a vector close to record "a"; it should rank higher.
        # ``threshold=0.0`` so the orthogonal second row still passes
        # the threshold filter (its cosine similarity is 0, not negative).
        results = await store.search_similar(
            query_embedding=[1.0, 0.0, 0.0, 0.0],
            system_id="test",
            limit=2,
            threshold=0.0,
        )
        assert results, "expected at least one similar row"
        assert results[0]["conversation_id"] == "a"
        assert results[0]["score"] is not None
        # The second row is the orthogonal vector — similarity should be
        # lower (zero-ish cosine).
        assert results[1]["conversation_id"] == "b"
    finally:
        await store.close()
