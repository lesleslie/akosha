"""Phase B Task 5 — Cross-namespace ACL positive path.

REQ-OSUB-B-004: an Akosha caller CAN read ``sb.*`` reflections only when the
adapter is constructed with ``cross_namespace_grant=True``. This is the
admin-granted counterpart to the negative test in
``session-buddy/tests/integration/test_cross_namespace_acl_sb.py``.

Tests run against a fake pool (mirroring oneiric/tests/adapters/
test_pgvector_adapter.py and the akosha ``PgvectorHotStore`` e2e pattern).
When ``AKOSHA_TEST_PGVECTOR_URL`` is set, ``test_admin_grant_against_live_pgvector``
asserts the same behaviour against a real Postgres stack.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

# Optional asyncpg/pgvector runtime deps — Phase 2 dev install only.
pytest.importorskip("asyncpg", reason="asyncpg not installed")
pytest.importorskip("pgvector", reason="pgvector python package not installed")
pytest.importorskip(
    "oneiric.adapters.vector.pgvector",
    reason="oneiric pgvector adapter unavailable",
)

from oneiric.adapters.vector.pgvector import (  # noqa: E402
    PgvectorAdapter,
    PgvectorSettings,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.req("REQ-OSUB-B-004"),
]


class _FakePgConnection:
    """Minimal asyncpg stub that records every executed statement."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.search_results: list[dict[str, Any]] = [
            {
                "id": "sb-refl-1",
                "metadata": {"source": "session-buddy", "topic": "cross-ns"},
                "embedding": [0.1, 0.2, 0.3, 0.4],
                "distance": 0.05,
            },
            {
                "id": "sb-refl-2",
                "metadata": {"source": "session-buddy", "topic": "positive"},
                "embedding": None,
                "distance": 0.12,
            },
        ]

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append(("execute", query.strip()))
        return "OK"

    async def fetch(self, query: str, *args: Any):
        self.calls.append(("fetch", query.strip()))
        if "ORDER BY distance" in query:
            return self.search_results
        return []

    async def fetchrow(self, query: str, *args: Any):
        self.calls.append(("fetchrow", query.strip()))
        return {"id": args[0] if args else "row"}

    async def fetchval(self, query: str, *args: Any):
        self.calls.append(("fetchval", query.strip()))
        return 0


class _FakePgPool:
    def __init__(self) -> None:
        self.connection = _FakePgConnection()
        self.closed = False

    async def acquire(self) -> _FakePgConnection:
        return self.connection

    async def release(self, _conn: _FakePgConnection) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


def _make_adapter(
    *,
    caller_namespace: str = "akosha",
    cross_namespace_grant: bool = True,
) -> tuple[PgvectorAdapter, _FakePgPool]:
    pool = _FakePgPool()

    async def pool_factory(**kwargs: Any) -> _FakePgPool:
        return pool

    async def register_vector(_conn: Any) -> None:
        return None

    adapter = PgvectorAdapter(
        PgvectorSettings(
            caller_namespace=caller_namespace,
            cross_namespace_grant=cross_namespace_grant,
            dsn="postgresql://stub/akosha",
        ),
        pool_factory=pool_factory,
        register_vector=register_vector,
    )
    return adapter, pool


async def test_admin_granted_akosha_reads_sb_namespace() -> None:
    """Per spec §6.2 — admin grant permits Akosha caller to read sb.reflections."""
    adapter, pool = _make_adapter(
        caller_namespace="akosha", cross_namespace_grant=True
    )

    results = await adapter.search(
        collection="reflections",
        query_vector=[0.1, 0.2, 0.3, 0.4],
        limit=5,
        namespace="sb",
    )

    # Successful cross-namespace read returns rows; the assertion is non-empty.
    assert isinstance(results, list)
    assert len(results) == 2
    assert [r.id for r in results] == ["sb-refl-1", "sb-refl-2"]
    assert results[0].metadata["source"] == "session-buddy"

    # Cross-namespace reads MUST target a namespaced table — i.e. the SQL table
    # name MUST contain the sb_ prefix (otherwise akosha would have read its
    # own akosha_reflections collection by mistake).
    fetch_calls = [c[1] for c in pool.connection.calls if c[0] == "fetch"]
    assert any('"vectors_sb_reflections"' in sql for sql in fetch_calls), (
        f"Expected vectors_sb_reflections table in SQL; got: {fetch_calls}"
    )
    assert not any('"vectors_akosha_reflections"' in sql for sql in fetch_calls), (
        f"ACL routed akosha caller to akosha_reflections instead of sb_reflections: "
        f"{fetch_calls}"
    )


async def test_admin_grant_does_not_weaken_same_namespace_acl() -> None:
    """cross_namespace_grant=True is additive — same-namespace reads still work."""
    adapter, _pool = _make_adapter(
        caller_namespace="akosha", cross_namespace_grant=True
    )
    # Omitting namespace= defaults to caller (akosha), which is allowed.
    results = await adapter.search(
        collection="reflections",
        query_vector=[0.1, 0.2, 0.3, 0.4],
        limit=5,
    )
    assert isinstance(results, list)
    assert len(results) == 2


async def test_no_grant_blocks_akosha_from_sb_even_with_collection_name_hint() -> None:
    """Defence-in-depth — caller without grant is blocked regardless of how
    the collection name is spelled."""
    adapter, pool = _make_adapter(
        caller_namespace="akosha", cross_namespace_grant=False
    )
    with pytest.raises(PermissionError, match=r"target_namespace='sb'"):
        await adapter.search(
            collection="reflections",
            query_vector=[0.1, 0.2, 0.3, 0.4],
            limit=5,
            namespace="sb",
        )
    # SQL MUST NOT have been issued — denial is enforcement, not post-hoc.
    assert pool.connection.calls == []


# Live-stack opt-in: skip unless AKOSHA_TEST_PGVECTOR_URL is set. Mirrors the
# existing AKOSHA convention used by test_pgvector_hot_store_e2e.py.
LIVE_PGVECTOR_URL = os.environ.get("AKOSHA_TEST_PGVECTOR_URL", "").strip()
live_stack_required = pytest.mark.skipif(
    not LIVE_PGVECTOR_URL,
    reason="AKOSHA_TEST_PGVECTOR_URL not set — skipping live-stack assertion",
)


@live_stack_required
async def test_admin_grant_against_live_pgvector() -> None:
    """Same positive path against a real Postgres + pgvector stack.

    Requires a live SB-owned ``sb.reflections`` collection seeded by the
    operator (this test does not seed; it only reads).
    """
    settings = PgvectorSettings(
        dsn=LIVE_PGVECTOR_URL,
        caller_namespace="akosha",
        cross_namespace_grant=True,
    )
    adapter = PgvectorAdapter(settings)
    try:
        results = await adapter.search(
            collection="reflections",
            query_vector=[0.0] * 4,
            limit=5,
            namespace="sb",
        )
        assert isinstance(results, list)
    finally:
        await adapter.cleanup()
