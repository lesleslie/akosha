"""Tests for HotStore / PgvectorHotStore embedding-dim validation.

Plan: docs/plans/2026-08-29-embedding-dim-fix.md (Phase 2). Pins the
fail-loud contract: schema dim equals ``embedding_dim``; insert() and
search_similar() raise ``ValueError`` on dim mismatch.

The pgvector cases live behind ``pytest.importorskip`` so the suite
stays green on dev installs that lack the optional oneiric pgvector
adapter; the DuckDB cases are the canonical coverage.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from akosha.storage.hot_store import HotStore


def _record(
    *,
    conversation_id: str = "conv-1",
    embedding: list[float] | None = None,
) -> MagicMock:
    """Build a HotRecord-shaped MagicMock with a controllable embedding."""
    rec = MagicMock()
    rec.system_id = "sys1"
    rec.conversation_id = conversation_id
    rec.content = "hello"
    rec.embedding = embedding if embedding is not None else [0.1] * 384
    rec.timestamp = datetime(2024, 1, 1, tzinfo=UTC)
    rec.metadata = {"k": "v"}
    return rec


class TestHotStoreAcceptsConfiguredDim:
    """``HotStore(embedding_dim=N)`` builds a schema sized for N."""

    @pytest.mark.asyncio
    async def test_hot_store_accepts_configured_dim(self) -> None:
        """A 768-dim record inserts cleanly into a 768-dim store."""
        hs = HotStore(embedding_dim=768)
        await hs.initialize()

        record = _record(embedding=[0.1] * 768)
        # Should not raises.
        await hs.insert(record)

        result = hs.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()
        assert result[0] == 1
        await hs.close()

    @pytest.mark.asyncio
    async def test_hot_store_default_dim_is_384(self) -> None:
        """Without an embedding service, default falls back to 384."""
        hs = HotStore()
        await hs.initialize()

        # Public attribute must be 384 so existing 384-dim fixtures work.
        assert hs._embedding_dim == 384

        record = _record(embedding=[0.1] * 384)
        await hs.insert(record)
        await hs.close()


class TestHotStoreRejectsMismatchedDim:
    """``insert()`` raises ``ValueError`` on dim mismatch (fail-loud)."""

    @pytest.mark.asyncio
    async def test_hot_store_rejects_mismatched_dim(self) -> None:
        """384 schema rejects a 768-dim record with ValueError."""
        hs = HotStore(embedding_dim=384)
        await hs.initialize()

        bad_record = _record(embedding=[0.1] * 768)
        with pytest.raises(ValueError, match="dim mismatch"):
            await hs.insert(bad_record)
        await hs.close()

    @pytest.mark.asyncio
    async def test_hot_store_768_rejects_384_record(self) -> None:
        """The mirror case — 768 schema rejects a 384-dim record."""
        hs = HotStore(embedding_dim=768)
        await hs.initialize()

        bad_record = _record(embedding=[0.1] * 384)
        with pytest.raises(ValueError, match="dim mismatch"):
            await hs.insert(bad_record)
        await hs.close()


class TestHotStoreSearchRejectsMismatchedQueryDim:
    """``search_similar()`` raises ``ValueError`` on dim mismatch."""

    @pytest.mark.asyncio
    async def test_hot_store_search_rejects_mismatched_query_dim(self) -> None:
        """768-dim store rejects a 384-dim query."""
        hs = HotStore(embedding_dim=768)
        await hs.initialize()

        with pytest.raises(ValueError, match="query dim mismatch"):
            await hs.search_similar([0.1] * 384)
        await hs.close()

    @pytest.mark.asyncio
    async def test_hot_store_384_search_rejects_768_query(self) -> None:
        """Mirror: 384-dim store rejects a 768-dim query."""
        hs = HotStore(embedding_dim=384)
        await hs.initialize()

        with pytest.raises(ValueError, match="query dim mismatch"):
            await hs.search_similar([0.1] * 768)
        await hs.close()


class TestPgvectorHotStoreConfiguredDim:
    """Pgvector variant mirrors HotStore's dim contract when deps are present.

    Skipped when the oneiric pgvector adapter isn't installed; the
    DuckDB cases above cover the same contract for environments without
    the optional pgvector dependency group.
    """

    pytest.importorskip("oneiric.adapters.vector.pgvector", reason="pgvector adapter unavailable")

    def test_pgvector_variant_uses_configured_dim(self) -> None:
        """PgvectorHotStore captures the configured embedding dim."""
        from akosha.storage.pgvector_hot_store import PgvectorHotStore

        store = PgvectorHotStore(
            pg_url="postgresql://localhost:5432/akosha",
            embedding_dimension=768,
        )
        assert store._embedding_dimension == 768

    def test_pgvector_variant_resolves_via_contract_when_unset(self) -> None:
        """Default construction resolves via the dim contract (no service → 384)."""
        from akosha.storage.pgvector_hot_store import PgvectorHotStore

        store = PgvectorHotStore(pg_url="postgresql://localhost:5432/akosha")
        # Without an embedding service this falls back to the 384 default.
        assert store._embedding_dimension == 384