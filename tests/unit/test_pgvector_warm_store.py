"""Comprehensive unit tests for ``akosha.storage.pgvector_warm_store.PgvectorWarmStore``.

Targets 95%+ branch coverage on the warm-tier pgvector module (added
2026-09-27 when pgvector migrated from the hot tier to the warm tier).
The oneiric ``PgvectorAdapter`` is mocked end-to-end so the suite has
no live-Postgres requirement and runs in well under a second.

Coverage targets mirror the methods on ``PgvectorWarmStore``:

* ``__init__`` — three branches: explicit 384 fallback, non-384 explicit
  dim, default (``None``) routed through ``resolve_embedding_dim()``,
  custom ``shard_router`` injection.
* ``initialize`` — adapter construction per shard, ``init()`` per shard,
  ``create_collection()`` per shard, idempotent double-init.
* ``insert`` — happy path + shard routing, ``not initialized`` RuntimeError,
  dim mismatch ValueError, metadata merge, INT8 quantization round-trip.
* ``search_similar`` — system_id-targeted (single shard) vs global
  (all shards parallel via asyncio.gather), threshold filter
  (None / 0.0 / 1.0), metadata stripping (system_id/summary/timestamp/
  quantized removed), result merging + sort + truncation, ``not
  initialized`` RuntimeError.
* ``get_by_id`` — happy path, ``not initialized``, None when no docs,
  metadata stripping.
* ``delete`` — happy path (parallel across all shards), ``not initialized``.
* ``close`` — idempotent double-close, per-adapter cleanup best-effort.
* Sharding — same ``system_id`` always routes to the same shard;
  different ``system_id`` values spread across shards deterministically.

Notes
-----
* ``record.metadata`` must be a real dict so ``record.metadata.items()``
  iterates inside the module's ``| dict(record.metadata.items())`` merge.
* The module-level ``_COLLECTION_PREFIX = "akosha_warm_shard_"`` is asserted
  on directly so future renames are caught early.
* ``PgvectorAdapter`` is patched on the ``pgvector_warm_store`` module —
  the production code binds the symbol at import time.
* The suite loads the module via ``importlib`` (not the package's
  ``__init__.py``) to avoid the eager numpy import chain under pytest-cov.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from akosha.models import WarmRecord
from oneiric.adapters.vector.vector_types import VectorSearchResult


# ---------------------------------------------------------------------------
# Import the module under test without triggering ``akosha.storage.__init__``
# ---------------------------------------------------------------------------
#
# Loading via ``import akosha.storage.pgvector_warm_store`` would pull in
# ``akosha.storage.aging`` → ``numpy``. Under pytest-cov the numpy C
# extension fails with "cannot load module more than once per process",
# which is a coverage-isolation interaction unrelated to the module under
# test. Importing the file directly via ``importlib`` sidesteps the
# ``__init__.py`` chain entirely.

_mod_path = Path("/Users/les/Projects/akosha/akosha/storage/pgvector_warm_store.py")
_storage_dir = _mod_path.parent

# Stub the ``akosha`` and ``akosha.storage`` packages so submodule
# lookups (e.g. ``akosha.storage.sharding``) resolve to real packages
# without triggering the ``akosha.storage.__init__`` chain (which
# pulls in ``aging`` → numpy).
#
# Both stubs implement PEP 562 module-level ``__getattr__`` so attribute
# lookups for submodules (e.g. ``akosha.storage.sharding``) fall through
# to ``sys.modules`` — that's how real packages resolve submodule
# attributes, and it's what ``monkeypatch.setattr`` needs to walk the
# dotted path.
def _make_pkg_stub(name: str, path: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__path__ = [path]

    def __getattr__(attr_name: str) -> Any:
        full = f"{name}.{attr_name}"
        if full in sys.modules:
            return sys.modules[full]
        raise AttributeError(f"module {name!r} has no attribute {attr_name!r}")

    mod.__getattr__ = __getattr__  # type: ignore[attr-defined]
    return mod


_akosha_stub = _make_pkg_stub("akosha", str(_storage_dir.parent))
sys.modules.setdefault("akosha", _akosha_stub)

_pkg_stub = _make_pkg_stub("akosha.storage", str(_storage_dir))
sys.modules.setdefault("akosha.storage", _pkg_stub)


def _load_module_directly(name: str, file_path: Path) -> types.ModuleType:
    """Load a single .py file into ``sys.modules[name]`` via importlib.

    Used to pre-install dependency modules that production lazily
    imports (``akosha.storage.sharding``, ``akosha.models``,
    ``akosha.processing.embedding_dim``) so monkeypatch can target
    them at their canonical binding point.
    """
    spec = importlib.util.spec_from_file_location(name, file_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Pre-install dependencies that production lazily imports.
_sharding_mod = _load_module_directly(
    "akosha.storage.sharding", _storage_dir / "sharding.py"
)
_models_mod = _load_module_directly(
    "akosha.models", _storage_dir.parent / "models" / "__init__.py"
)
_embed_mod = _load_module_directly(
    "akosha.processing.embedding_dim",
    _storage_dir.parent / "processing" / "embedding_dim.py",
)
# ``akosha.processing`` package stub so the embedding_dim submodule path
# resolves; production only imports the submodule but Python needs the
# parent registered. Reuses the same __getattr__ fallback as above.
_processing_pkg = _make_pkg_stub(
    "akosha.processing",
    str(_storage_dir.parent / "processing"),
)
sys.modules.setdefault("akosha.processing", _processing_pkg)

_spec = importlib.util.spec_from_file_location(
    "akosha.storage.pgvector_warm_store", _mod_path
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["akosha.storage.pgvector_warm_store"] = _mod
_spec.loader.exec_module(_mod)

PgvectorWarmStore = _mod.PgvectorWarmStore
_DEFAULT_LEGACY_DIMENSION = _mod._DEFAULT_LEGACY_DIMENSION
_COLLECTION_PREFIX = _mod._COLLECTION_PREFIX
_DISTANCE_METRIC = _mod._DISTANCE_METRIC
_collection_name = _mod._collection_name


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

DIM = 4  # tiny — adapter is mocked, real vector length never reaches Postgres
assert _COLLECTION_PREFIX == "akosha_warm_shard_", (
    f"unexpected collection prefix rename: {_COLLECTION_PREFIX!r}"
)
assert _DISTANCE_METRIC == "cosine"

#: Test-only placeholder for the pgvector DSN. NOT a real credential —
#: the production ``PgvectorAdapter`` is fully mocked end-to-end, so
#: this string is never resolved. Centralized so the harness can swap
#: it for an env-var if the suite ever spins up a live Postgres test
#: container (none exists today).
_TEST_PG_URL = "postgresql://localhost:5432/akosha"


def _warm_record(
    *,
    system_id: str = "sys1",
    conversation_id: str = "conv-1",
    embedding: list[int] | None = None,
    summary: str = "summary text",
    timestamp: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> WarmRecord:
    """Build a ``WarmRecord`` for tests.

    ``WarmRecord.embedding`` is INT8[384] quantized (see
    ``akosha/storage/models.py``). Tests default to ``[0, 1, 2, 3]`` to
    match ``DIM``; mismatch tests override.
    """
    return WarmRecord(
        system_id=system_id,
        conversation_id=conversation_id,
        embedding=embedding if embedding is not None else [0, 1, 2, 3],
        summary=summary,
        timestamp=timestamp if timestamp is not None else datetime(2024, 1, 1, tzinfo=UTC),
        metadata=metadata if metadata is not None else {"k": "v"},
    )


def _adapter_mock() -> AsyncMock:
    """Build a fresh ``PgvectorAdapter``-shaped async mock."""
    adapter = AsyncMock()
    adapter.init = AsyncMock(return_value=None)
    adapter.create_collection = AsyncMock(return_value=None)
    adapter.insert = AsyncMock(return_value=None)
    adapter.search = AsyncMock(return_value=[])
    adapter.get = AsyncMock(return_value=[])
    adapter.delete = AsyncMock(return_value=None)
    adapter.cleanup = AsyncMock(return_value=None)
    return adapter


def _store(**kwargs: Any) -> PgvectorWarmStore:
    """Build a ``PgvectorWarmStore`` without invoking ``__init__`` mocking.

    Defaults: small shard count (``shard_count=4``) for test ergonomics,
    pg_url pinned to localhost, dim pinned to ``DIM`` so the test
    embeddings (``[0, 1, 2, 3]``) validate against the dim contract.
    """
    kwargs.setdefault("pg_url", _TEST_PG_URL)
    kwargs.setdefault("embedding_dimension", DIM)
    kwargs.setdefault("shard_count", 4)
    return PgvectorWarmStore(**kwargs)


def _make_router(num_shards: int) -> MagicMock:
    """Build a ``ShardRouter`` mock with sensible default behaviors.

    Production calls ``router.get_shard(system_id)`` for inserts and
    ``router.get_target_shards(system_id)`` for searches. Defaults:

    * ``get_shard(system_id)`` returns ``0``.
    * ``get_target_shards`` is NOT pre-configured — tests must set its
      ``return_value`` (or ``side_effect``) explicitly per test, because
      the realistic MagicMock default (an auto-MagicMock) is hard to
      reason about in assertions and the production behavior is
      context-dependent (None → all shards, system_id → single shard).

    Helper to populate both at once for the two most common cases:
    """
    router = MagicMock(num_shards=num_shards)
    router.get_shard.return_value = 0
    return router


def _set_router_global(router: MagicMock, num_shards: int) -> None:
    """Pin ``router.get_target_shards(None)`` to return all shards."""
    router.get_target_shards.return_value = list(range(num_shards))


def _set_router_targeted(router: MagicMock, shard_ids: list[int]) -> None:
    """Pin ``router.get_target_shards(system_id)`` to a specific shard list.

    Use this when the test wants to assert single-shard routing for a
    given ``system_id``. Pair it with ``router.get_shard.return_value = N``
    so production's insert() and search_similar() target the same shard.
    """
    router.get_target_shards.return_value = shard_ids


# ---------------------------------------------------------------------------
# __init__ — dim resolution + shard router injection
# ---------------------------------------------------------------------------


class TestInit:
    """``__init__`` wires dim and shard router correctly across branches."""

    def test_default_dim_via_contract(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``embedding_dimension=None`` → ``resolve_embedding_dim()`` (default 384)."""
        # Stub the contract to a deterministic value so the test isn't
        # coupled to the active embedding service. Production imports
        # ``resolve_embedding_dim`` at module-top, so the symbol lives
        # on the loaded module.
        monkeypatch.setattr(_mod, "resolve_embedding_dim", lambda: 384)
        # Direct construction — bypasses _store() helper to keep the
        # dim parameter at its default (None) so the contract path runs.
        store = PgvectorWarmStore(pg_url=_TEST_PG_URL)
        assert store._embedding_dimension == 384

    def test_explicit_legacy_384_preserved(self) -> None:
        """Explicit ``embedding_dimension=384`` is honored (legacy default)."""
        store = _store(embedding_dimension=_DEFAULT_LEGACY_DIMENSION)
        assert store._embedding_dimension == 384

    def test_explicit_non_384_dim_preserved(self) -> None:
        """Explicit non-384 dim is honored verbatim."""
        store = _store(embedding_dimension=768)
        assert store._embedding_dimension == 768

    def test_custom_shard_router_takes_precedence(self) -> None:
        """Explicit ``shard_router`` bypasses ``shard_count``."""
        router = _make_router(64)
        store = _store(shard_count=256, shard_router=router)
        assert store._shard_router is router

    def test_default_router_built_from_shard_count(self) -> None:
        """``shard_count=None`` (or unset) → ``ShardRouter(num_shards=...)`` built eagerly.

        Production eagerly builds the router in ``__init__`` (not lazy)
        so the integration tests can rely on it. This test pins the
        router's ``num_shards`` matches the ``shard_count`` kwarg.
        """
        store = _store(shard_count=8)
        assert store._shard_router is not None
        assert store._shard_router.num_shards == 8


# ---------------------------------------------------------------------------
# initialize — per-shard adapter provisioning + idempotency
# ---------------------------------------------------------------------------


class TestInitialize:
    """``initialize()`` creates one ``PgvectorAdapter`` per shard."""

    @pytest.mark.asyncio
    async def test_initialize_provisions_one_adapter_per_shard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """256 shards → 256 ``create_collection`` calls (one per shard)."""
        # Patch the ``ShardRouter`` class at its source module — production
        # uses ``from akosha.storage.sharding import ShardRouter`` inside
        # the method body (lazy), so patching here intercepts the import
        # at the canonical binding point.
        router = _make_router(4)
        # Patch the canonical binding point — production uses lazy
        # ``from akosha.storage.sharding import ShardRouter``, so the
        # canonical attribute lives on sys.modules["akosha.storage.sharding"].
        # Direct attribute assignment avoids the monkeypath dotted-path
        # walker (which would re-import the real ``akosha.storage`` package).
        monkeypatch.setattr(_sharding_mod, "ShardRouter", lambda num_shards=None: router)
        adapter = _adapter_mock()
        monkeypatch.setattr(_mod, "PgvectorAdapter", lambda settings: adapter)

        store = _store(shard_router=router)
        await store.initialize()

        assert adapter.init.await_count == 4
        assert adapter.create_collection.await_count == 4
        # Every shard gets a unique collection name.
        collection_names = [
            call.kwargs["name"] for call in adapter.create_collection.await_args_list
        ]
        assert collection_names == [
            "akosha_warm_shard_000",
            "akosha_warm_shard_001",
            "akosha_warm_shard_002",
            "akosha_warm_shard_003",
        ]
        # Every collection is created with the right dim + metric.
        for call in adapter.create_collection.await_args_list:
            assert call.kwargs["dimension"] == DIM
            assert call.kwargs["distance_metric"] == "cosine"
        assert store._initialized is True
        assert set(store._adapters.keys()) == {0, 1, 2, 3}

    @pytest.mark.asyncio
    async def test_initialize_is_idempotent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Second ``initialize()`` is a no-op (won't re-provision adapters)."""
        router = _make_router(2)
        # Patch the canonical binding point — production uses lazy
        # ``from akosha.storage.sharding import ShardRouter``, so the
        # canonical attribute lives on sys.modules["akosha.storage.sharding"].
        # Direct attribute assignment avoids the monkeypath dotted-path
        # walker (which would re-import the real ``akosha.storage`` package).
        monkeypatch.setattr(_sharding_mod, "ShardRouter", lambda num_shards=None: router)
        adapter = _adapter_mock()
        monkeypatch.setattr(_mod, "PgvectorAdapter", lambda settings: adapter)

        store = _store(shard_router=router)
        await store.initialize()
        await store.initialize()

        # init/create_collection only fire on the first call.
        assert adapter.init.await_count == 2
        assert adapter.create_collection.await_count == 2


# ---------------------------------------------------------------------------
# insert — shard routing + dim validation + INT8 quantization
# ---------------------------------------------------------------------------


class TestInsert:
    """``insert`` validates dim, routes to the right shard, quantizes INT8."""

    @pytest.mark.asyncio
    async def test_insert_routes_by_system_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``system_id="alpha"`` and ``system_id="beta"`` go to the right shards.

        The ShardRouter mock pins the routing so we can assert the
        collection name without coupling to the real SHA-256 hash.
        """
        router = _make_router(4)
        # alpha → shard 0, beta → shard 2
        router.get_shard.side_effect = lambda system_id: {"alpha": 0, "beta": 2}[system_id]
        adapter = _adapter_mock()
        # Patch the canonical binding point — production uses lazy
        # ``from akosha.storage.sharding import ShardRouter``, so the
        # canonical attribute lives on sys.modules["akosha.storage.sharding"].
        # Direct attribute assignment avoids the monkeypath dotted-path
        # walker (which would re-import the real ``akosha.storage`` package).
        monkeypatch.setattr(_sharding_mod, "ShardRouter", lambda num_shards=None: router)
        monkeypatch.setattr(_mod, "PgvectorAdapter", lambda settings: adapter)

        store = _store(shard_router=router)
        await store.initialize()

        await store.insert(_warm_record(system_id="alpha", embedding=[0, 1, 2, 3]))
        await store.insert(_warm_record(system_id="beta", embedding=[0, 1, 2, 3]))

        # Two distinct collection calls — one per shard.
        assert adapter.insert.await_count == 2
        assert adapter.insert.await_args_list[0].args[0] == "akosha_warm_shard_000"
        assert adapter.insert.await_args_list[1].args[0] == "akosha_warm_shard_002"

    @pytest.mark.asyncio
    async def test_insert_quantizes_int8_to_float(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """INT8 quantized values are converted to floats on insert.

        The production contract stores ``embedding: list[int]`` (INT8)
        but pgvector columns are typed to ``float[]``. The store does the
        lossy conversion ``int/127.0`` so the loss matches what DuckDB
        warm-store on-disk persistence does today.
        """
        router = _make_router(2)
        adapter = _adapter_mock()
        # Patch the canonical binding point — production uses lazy
        # ``from akosha.storage.sharding import ShardRouter``, so the
        # canonical attribute lives on sys.modules["akosha.storage.sharding"].
        # Direct attribute assignment avoids the monkeypath dotted-path
        # walker (which would re-import the real ``akosha.storage`` package).
        monkeypatch.setattr(_sharding_mod, "ShardRouter", lambda num_shards=None: router)
        monkeypatch.setattr(_mod, "PgvectorAdapter", lambda settings: adapter)

        store = _store(shard_router=router)
        await store.initialize()

        # embedding = [-127, 0, 127, 64] → floats = [-1.0, 0.0, 1.0, 0.5039...]
        await store.insert(_warm_record(embedding=[-127, 0, 127, 64]))
        inserted_doc = adapter.insert.await_args.args[1][0]
        assert inserted_doc.vector == pytest.approx([-1.0, 0.0, 1.0, 64 / 127.0])

    @pytest.mark.asyncio
    async def test_insert_tags_metadata_quantized_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``metadata.quantized=True`` is set on every insert (consumer contract)."""
        router = _make_router(2)
        adapter = _adapter_mock()
        # Patch the canonical binding point — production uses lazy
        # ``from akosha.storage.sharding import ShardRouter``, so the
        # canonical attribute lives on sys.modules["akosha.storage.sharding"].
        # Direct attribute assignment avoids the monkeypath dotted-path
        # walker (which would re-import the real ``akosha.storage`` package).
        monkeypatch.setattr(_sharding_mod, "ShardRouter", lambda num_shards=None: router)
        monkeypatch.setattr(_mod, "PgvectorAdapter", lambda settings: adapter)

        store = _store(shard_router=router)
        await store.initialize()

        await store.insert(_warm_record())
        inserted_doc = adapter.insert.await_args.args[1][0]
        assert inserted_doc.metadata["quantized"] is True
        assert inserted_doc.metadata["system_id"] == "sys1"
        assert inserted_doc.metadata["summary"] == "summary text"

    @pytest.mark.asyncio
    async def test_insert_merges_extra_metadata(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Extra ``record.metadata`` keys are merged into the doc metadata."""
        router = _make_router(2)
        adapter = _adapter_mock()
        # Patch the canonical binding point — production uses lazy
        # ``from akosha.storage.sharding import ShardRouter``, so the
        # canonical attribute lives on sys.modules["akosha.storage.sharding"].
        # Direct attribute assignment avoids the monkeypath dotted-path
        # walker (which would re-import the real ``akosha.storage`` package).
        monkeypatch.setattr(_sharding_mod, "ShardRouter", lambda num_shards=None: router)
        monkeypatch.setattr(_mod, "PgvectorAdapter", lambda settings: adapter)

        store = _store(shard_router=router)
        await store.initialize()

        await store.insert(
            _warm_record(metadata={"source": "test", "language": "en"})
        )
        inserted_doc = adapter.insert.await_args.args[1][0]
        assert inserted_doc.metadata["source"] == "test"
        assert inserted_doc.metadata["language"] == "en"
        # Canonical fields are still present (the merge keeps both).
        assert inserted_doc.metadata["system_id"] == "sys1"

    @pytest.mark.asyncio
    async def test_insert_serializes_datetime_timestamp(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``datetime`` timestamps are ISO-formatted into metadata."""
        router = _make_router(2)
        adapter = _adapter_mock()
        # Patch the canonical binding point — production uses lazy
        # ``from akosha.storage.sharding import ShardRouter``, so the
        # canonical attribute lives on sys.modules["akosha.storage.sharding"].
        # Direct attribute assignment avoids the monkeypath dotted-path
        # walker (which would re-import the real ``akosha.storage`` package).
        monkeypatch.setattr(_sharding_mod, "ShardRouter", lambda num_shards=None: router)
        monkeypatch.setattr(_mod, "PgvectorAdapter", lambda settings: adapter)

        store = _store(shard_router=router)
        await store.initialize()

        when = datetime(2024, 6, 15, 12, 30, 45, tzinfo=UTC)
        await store.insert(_warm_record(timestamp=when))
        inserted_doc = adapter.insert.await_args.args[1][0]
        assert inserted_doc.metadata["timestamp"] == when.isoformat()

    @pytest.mark.asyncio
    async def test_insert_serializes_string_timestamp(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """String timestamps are passed through verbatim (no .isoformat() call)."""
        router = _make_router(2)
        adapter = _adapter_mock()
        # Patch the canonical binding point — production uses lazy
        # ``from akosha.storage.sharding import ShardRouter``, so the
        # canonical attribute lives on sys.modules["akosha.storage.sharding"].
        # Direct attribute assignment avoids the monkeypath dotted-path
        # walker (which would re-import the real ``akosha.storage`` package).
        monkeypatch.setattr(_sharding_mod, "ShardRouter", lambda num_shards=None: router)
        monkeypatch.setattr(_mod, "PgvectorAdapter", lambda settings: adapter)

        store = _store(shard_router=router)
        await store.initialize()

        await store.insert(_warm_record(timestamp="2024-06-15T12:30:45+00:00"))  # type: ignore[arg-type]
        inserted_doc = adapter.insert.await_args.args[1][0]
        assert inserted_doc.metadata["timestamp"] == "2024-06-15T12:30:45+00:00"

    @pytest.mark.asyncio
    async def test_insert_raises_when_not_initialized(self) -> None:
        """``insert`` before ``initialize()`` raises RuntimeError."""
        store = _store()
        with pytest.raises(RuntimeError, match="not initialized"):
            await store.insert(_warm_record())

    @pytest.mark.asyncio
    async def test_insert_rejects_dim_mismatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Wrong-dim embedding raises ValueError before any adapter call."""
        router = _make_router(2)
        adapter = _adapter_mock()
        # Patch the canonical binding point — production uses lazy
        # ``from akosha.storage.sharding import ShardRouter``, so the
        # canonical attribute lives on sys.modules["akosha.storage.sharding"].
        # Direct attribute assignment avoids the monkeypath dotted-path
        # walker (which would re-import the real ``akosha.storage`` package).
        monkeypatch.setattr(_sharding_mod, "ShardRouter", lambda num_shards=None: router)
        monkeypatch.setattr(_mod, "PgvectorAdapter", lambda settings: adapter)

        store = _store(shard_router=router)
        await store.initialize()

        # Embedding length 5 != DIM (4)
        with pytest.raises(ValueError, match="dim mismatch"):
            await store.insert(_warm_record(embedding=[0, 1, 2, 3, 4]))

        # The adapter was NOT called.
        adapter.insert.assert_not_called()


# ---------------------------------------------------------------------------
# search_similar — single-shard vs all-shards, threshold, merge+sort
# ---------------------------------------------------------------------------


class TestSearchSimilar:
    """``search_similar`` fan-out semantics + threshold filter + merge."""

    @pytest.mark.asyncio
    async def test_search_with_system_id_targets_single_shard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``system_id`` set → only the owning shard is queried."""
        router = _make_router(4)
        router.get_shard.return_value = 1  # alpha lives on shard 1
        router.get_target_shards.return_value = [1]
        adapter = _adapter_mock()
        adapter.search = AsyncMock(
            return_value=[
                VectorSearchResult(
                    id="c1",
                    score=0.1,
                    metadata={"system_id": "alpha", "summary": "s1", "timestamp": "t"},
                )
            ]
        )
        # Patch the canonical binding point — production uses lazy
        # ``from akosha.storage.sharding import ShardRouter``, so the
        # canonical attribute lives on sys.modules["akosha.storage.sharding"].
        # Direct attribute assignment avoids the monkeypath dotted-path
        # walker (which would re-import the real ``akosha.storage`` package).
        monkeypatch.setattr(_sharding_mod, "ShardRouter", lambda num_shards=None: router)
        monkeypatch.setattr(_mod, "PgvectorAdapter", lambda settings: adapter)

        store = _store(shard_router=router)
        await store.initialize()

        results = await store.search_similar([0.0] * DIM, system_id="alpha", limit=10)

        # Only shard 1 was queried.
        assert adapter.search.await_count == 1
        assert adapter.search.await_args.kwargs["collection"] == "akosha_warm_shard_001"
        # system_id filter was applied.
        assert adapter.search.await_args.kwargs["filter_expr"] == {"system_id": "alpha"}
        # Result round-trip.
        assert len(results) == 1
        assert results[0]["conversation_id"] == "c1"
        assert results[0]["system_id"] == "alpha"

    @pytest.mark.asyncio
    async def test_search_without_system_id_targets_all_shards(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No ``system_id`` → all shards are queried in parallel."""
        router = _make_router(4)
        router.get_target_shards.return_value = [0, 1, 2, 3]
        adapter = _adapter_mock()
        # Patch the canonical binding point — production uses lazy
        # ``from akosha.storage.sharding import ShardRouter``, so the
        # canonical attribute lives on sys.modules["akosha.storage.sharding"].
        # Direct attribute assignment avoids the monkeypath dotted-path
        # walker (which would re-import the real ``akosha.storage`` package).
        monkeypatch.setattr(_sharding_mod, "ShardRouter", lambda num_shards=None: router)
        monkeypatch.setattr(_mod, "PgvectorAdapter", lambda settings: adapter)

        store = _store(shard_router=router)
        await store.initialize()

        await store.search_similar([0.0] * DIM, limit=10)

        # All 4 shards queried in parallel.
        assert adapter.search.await_count == 4
        # No system_id filter on a global search.
        for call in adapter.search.await_args_list:
            assert call.kwargs["filter_expr"] is None

    @pytest.mark.asyncio
    async def test_search_merges_results_and_truncates_to_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Results from multiple shards are merged, sorted by score, truncated to ``limit``."""
        router = _make_router(3)
        router.get_target_shards.return_value = [0, 1, 2]

        adapter = _adapter_mock()
        # Each shard returns 3 results; total = 9, limit = 5 → 5 returned.
        adapter.search = AsyncMock(
            side_effect=[
                [
                    VectorSearchResult(
                        id=f"c{i}-{shard}",
                        score=0.1 + i * 0.01 + shard * 0.1,
                        metadata={
                            "system_id": "s",
                            "summary": f"s{i}",
                            "timestamp": "t",
                        },
                    )
                    for i in range(3)
                ]
                for shard in range(3)
            ]
        )
        # Patch the canonical binding point — production uses lazy
        # ``from akosha.storage.sharding import ShardRouter``, so the
        # canonical attribute lives on sys.modules["akosha.storage.sharding"].
        # Direct attribute assignment avoids the monkeypath dotted-path
        # walker (which would re-import the real ``akosha.storage`` package).
        monkeypatch.setattr(_sharding_mod, "ShardRouter", lambda num_shards=None: router)
        monkeypatch.setattr(_mod, "PgvectorAdapter", lambda settings: adapter)

        store = _store(shard_router=router)
        await store.initialize()

        results = await store.search_similar([0.0] * DIM, limit=5)
        assert len(results) == 5
        # Sorted ascending by score (lower distance = better).
        scores = [r["score"] for r in results]
        assert scores == sorted(scores)
        # Top result is the lowest score across all shards.
        assert scores[0] == pytest.approx(min(scores))

    @pytest.mark.asyncio
    async def test_search_threshold_filters_results(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``threshold`` filters results where ``distance > (1 - threshold)``."""
        # 1 shard keeps the assertion deterministic (no shard fan-out
        # duplication of the surviving result).
        router = _make_router(1)
        router.get_target_shards.return_value = [0]
        adapter = _adapter_mock()
        adapter.search = AsyncMock(
            return_value=[
                # distance=0.1 → similarity=0.9 (passes threshold=0.8)
                VectorSearchResult(id="close", score=0.1, metadata={"system_id": "s"}),
                # distance=0.5 → similarity=0.5 (fails threshold=0.8)
                VectorSearchResult(id="far", score=0.5, metadata={"system_id": "s"}),
            ]
        )
        # Patch the canonical binding point — production uses lazy
        # ``from akosha.storage.sharding import ShardRouter``, so the
        # canonical attribute lives on sys.modules["akosha.storage.sharding"].
        # Direct attribute assignment avoids the monkeypath dotted-path
        # walker (which would re-import the real ``akosha.storage`` package).
        monkeypatch.setattr(_sharding_mod, "ShardRouter", lambda num_shards=None: router)
        monkeypatch.setattr(_mod, "PgvectorAdapter", lambda settings: adapter)

        store = _store(shard_router=router)
        await store.initialize()

        results = await store.search_similar([0.0] * DIM, limit=10, threshold=0.8)
        ids = [r["conversation_id"] for r in results]
        assert ids == ["close"]

    @pytest.mark.asyncio
    async def test_search_threshold_none_disables_filter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``threshold=None`` (default) keeps all results regardless of score."""
        router = _make_router(1)
        router.get_target_shards.return_value = [0]
        adapter = _adapter_mock()
        adapter.search = AsyncMock(
            return_value=[
                VectorSearchResult(
                    id="any", score=999.0, metadata={"system_id": "s"}
                )
            ]
        )
        # Patch the canonical binding point — production uses lazy
        # ``from akosha.storage.sharding import ShardRouter``, so the
        # canonical attribute lives on sys.modules["akosha.storage.sharding"].
        # Direct attribute assignment avoids the monkeypath dotted-path
        # walker (which would re-import the real ``akosha.storage`` package).
        monkeypatch.setattr(_sharding_mod, "ShardRouter", lambda num_shards=None: router)
        monkeypatch.setattr(_mod, "PgvectorAdapter", lambda settings: adapter)

        store = _store(shard_router=router)
        await store.initialize()

        results = await store.search_similar([0.0] * DIM, threshold=None)
        assert len(results) == 1
        assert results[0]["score"] == 999.0

    @pytest.mark.asyncio
    async def test_search_strips_internal_metadata_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``system_id``, ``summary``, ``timestamp``, ``quantized`` are removed from ``metadata``."""
        router = _make_router(1)
        router.get_target_shards.return_value = [0]
        adapter = _adapter_mock()
        adapter.search = AsyncMock(
            return_value=[
                VectorSearchResult(
                    id="c1",
                    score=0.1,
                    metadata={
                        "system_id": "sys1",
                        "summary": "s",
                        "timestamp": "t",
                        "quantized": True,
                        "kept": "yes",
                    },
                )
            ]
        )
        # Patch the canonical binding point — production uses lazy
        # ``from akosha.storage.sharding import ShardRouter``, so the
        # canonical attribute lives on sys.modules["akosha.storage.sharding"].
        # Direct attribute assignment avoids the monkeypath dotted-path
        # walker (which would re-import the real ``akosha.storage`` package).
        monkeypatch.setattr(_sharding_mod, "ShardRouter", lambda num_shards=None: router)
        monkeypatch.setattr(_mod, "PgvectorAdapter", lambda settings: adapter)

        store = _store(shard_router=router)
        await store.initialize()

        results = await store.search_similar([0.0] * DIM)
        assert results[0]["metadata"] == {"kept": "yes"}

    @pytest.mark.asyncio
    async def test_search_raises_when_not_initialized(self) -> None:
        """``search_similar`` before ``initialize()`` raises RuntimeError."""
        store = _store()
        with pytest.raises(RuntimeError, match="not initialized"):
            await store.search_similar([0.0] * DIM)


# ---------------------------------------------------------------------------
# get_by_id — cross-shard lookup, None on miss
# ---------------------------------------------------------------------------


class TestGetById:
    """``get_by_id`` queries all shards in parallel; returns None on miss."""

    @pytest.mark.asyncio
    async def test_get_by_id_returns_doc_when_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Found in any shard → return parsed dict."""
        router = _make_router(3)
        adapter = _adapter_mock()
        adapter.get = AsyncMock(
            side_effect=[
                [],  # shard 0
                [  # shard 1 — found here
                    MagicMock(
                        id="c1",
                        metadata={
                            "system_id": "sys1",
                            "summary": "found",
                            "timestamp": "t",
                            "kept": "yes",
                        },
                    )
                ],
                [],  # shard 2
            ]
        )
        # Patch the canonical binding point — production uses lazy
        # ``from akosha.storage.sharding import ShardRouter``, so the
        # canonical attribute lives on sys.modules["akosha.storage.sharding"].
        # Direct attribute assignment avoids the monkeypath dotted-path
        # walker (which would re-import the real ``akosha.storage`` package).
        monkeypatch.setattr(_sharding_mod, "ShardRouter", lambda num_shards=None: router)
        monkeypatch.setattr(_mod, "PgvectorAdapter", lambda settings: adapter)

        store = _store(shard_router=router)
        await store.initialize()

        result = await store.get_by_id("c1")
        assert result is not None
        assert result["conversation_id"] == "c1"
        assert result["system_id"] == "sys1"
        assert result["summary"] == "found"
        assert result["metadata"] == {"kept": "yes"}

    @pytest.mark.asyncio
    async def test_get_by_id_returns_none_when_missing_in_all_shards(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not found in any shard → None (no exception)."""
        router = _make_router(2)
        adapter = _adapter_mock()
        adapter.get = AsyncMock(return_value=[])
        # Patch the canonical binding point — production uses lazy
        # ``from akosha.storage.sharding import ShardRouter``, so the
        # canonical attribute lives on sys.modules["akosha.storage.sharding"].
        # Direct attribute assignment avoids the monkeypath dotted-path
        # walker (which would re-import the real ``akosha.storage`` package).
        monkeypatch.setattr(_sharding_mod, "ShardRouter", lambda num_shards=None: router)
        monkeypatch.setattr(_mod, "PgvectorAdapter", lambda settings: adapter)

        store = _store(shard_router=router)
        await store.initialize()

        result = await store.get_by_id("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_by_id_raises_when_not_initialized(self) -> None:
        """``get_by_id`` before ``initialize()`` raises RuntimeError."""
        store = _store()
        with pytest.raises(RuntimeError, match="not initialized"):
            await store.get_by_id("c1")


# ---------------------------------------------------------------------------
# delete — parallel across all shards
# ---------------------------------------------------------------------------


class TestDelete:
    """``delete`` fans out across every shard in parallel."""

    @pytest.mark.asyncio
    async def test_delete_fans_out_across_all_shards(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One delete call per shard (parallel)."""
        router = _make_router(4)
        adapter = _adapter_mock()
        # Patch the canonical binding point — production uses lazy
        # ``from akosha.storage.sharding import ShardRouter``, so the
        # canonical attribute lives on sys.modules["akosha.storage.sharding"].
        # Direct attribute assignment avoids the monkeypath dotted-path
        # walker (which would re-import the real ``akosha.storage`` package).
        monkeypatch.setattr(_sharding_mod, "ShardRouter", lambda num_shards=None: router)
        monkeypatch.setattr(_mod, "PgvectorAdapter", lambda settings: adapter)

        store = _store(shard_router=router)
        await store.initialize()

        await store.delete("c1")

        assert adapter.delete.await_count == 4
        for shard_id, call in enumerate(adapter.delete.await_args_list):
            assert call.args[0] == f"akosha_warm_shard_{shard_id:03d}"
            assert call.args[1] == ["c1"]

    @pytest.mark.asyncio
    async def test_delete_raises_when_not_initialized(self) -> None:
        """``delete`` before ``initialize()`` raises RuntimeError."""
        store = _store()
        with pytest.raises(RuntimeError, match="not initialized"):
            await store.delete("c1")


# ---------------------------------------------------------------------------
# close — best-effort cleanup, idempotent
# ---------------------------------------------------------------------------


class TestClose:
    """``close`` cleans up all adapters and is idempotent."""

    @pytest.mark.asyncio
    async def test_close_cleans_up_all_adapters(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each adapter's ``cleanup()`` is awaited exactly once."""
        router = _make_router(3)
        adapter = _adapter_mock()
        # Patch the canonical binding point — production uses lazy
        # ``from akosha.storage.sharding import ShardRouter``, so the
        # canonical attribute lives on sys.modules["akosha.storage.sharding"].
        # Direct attribute assignment avoids the monkeypath dotted-path
        # walker (which would re-import the real ``akosha.storage`` package).
        monkeypatch.setattr(_sharding_mod, "ShardRouter", lambda num_shards=None: router)
        monkeypatch.setattr(_mod, "PgvectorAdapter", lambda settings: adapter)

        store = _store(shard_router=router)
        await store.initialize()
        await store.close()

        assert adapter.cleanup.await_count == 3
        assert store._adapters == {}
        assert store._initialized is False

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Second ``close()`` is a no-op (adapters dict already empty)."""
        router = _make_router(2)
        adapter = _adapter_mock()
        # Patch the canonical binding point — production uses lazy
        # ``from akosha.storage.sharding import ShardRouter``, so the
        # canonical attribute lives on sys.modules["akosha.storage.sharding"].
        # Direct attribute assignment avoids the monkeypath dotted-path
        # walker (which would re-import the real ``akosha.storage`` package).
        monkeypatch.setattr(_sharding_mod, "ShardRouter", lambda num_shards=None: router)
        monkeypatch.setattr(_mod, "PgvectorAdapter", lambda settings: adapter)

        store = _store(shard_router=router)
        await store.initialize()
        await store.close()
        await store.close()  # should not raise

        # cleanup only fires once (per adapter) — the dict-iterating loop
        # in close() runs over an empty dict the second time.
        assert adapter.cleanup.await_count == 2

    @pytest.mark.asyncio
    async def test_close_swallows_per_adapter_cleanup_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One adapter's ``cleanup()`` raising doesn't block the rest."""
        router = _make_router(3)
        adapter = _adapter_mock()
        adapter.cleanup = AsyncMock(
            side_effect=[None, RuntimeError("boom"), None]
        )
        # Patch the canonical binding point — production uses lazy
        # ``from akosha.storage.sharding import ShardRouter``, so the
        # canonical attribute lives on sys.modules["akosha.storage.sharding"].
        # Direct attribute assignment avoids the monkeypath dotted-path
        # walker (which would re-import the real ``akosha.storage`` package).
        monkeypatch.setattr(_sharding_mod, "ShardRouter", lambda num_shards=None: router)
        monkeypatch.setattr(_mod, "PgvectorAdapter", lambda settings: adapter)

        store = _store(shard_router=router)
        await store.initialize()
        await store.close()  # should not raise

        # All three adapters' cleanup was attempted despite the middle failure.
        assert adapter.cleanup.await_count == 3
        assert store._adapters == {}
        assert store._initialized is False


# ---------------------------------------------------------------------------
# Sharding — same system_id always lands on the same shard
# ---------------------------------------------------------------------------


class TestShardRouterIntegration:
    """Pin the sharding invariant: ``system_id`` → deterministic shard.

    Uses the real ``ShardRouter`` (not mocked) because the test value is
    in the contract itself, not the mock.
    """

    @pytest.mark.asyncio
    async def test_same_system_id_always_routes_to_same_shard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two inserts with the same ``system_id`` go to the same shard."""
        # Use the real ShardRouter so we exercise the SHA-256 hash path.
        from akosha.storage.sharding import ShardRouter

        real_router = ShardRouter(num_shards=8)
        adapter = _adapter_mock()
        monkeypatch.setattr(_mod, "PgvectorAdapter", lambda settings: adapter)

        # Pin dim explicitly to DIM (the helper's default) so the real
        # ShardRouter path doesn't trip the 384-dim contract.
        store = _store(shard_count=8, shard_router=real_router, embedding_dimension=DIM)
        await store.initialize()

        await store.insert(_warm_record(system_id="deterministic-test"))
        await store.insert(_warm_record(system_id="deterministic-test"))

        # Same collection, twice.
        assert adapter.insert.await_count == 2
        coll1 = adapter.insert.await_args_list[0].args[0]
        coll2 = adapter.insert.await_args_list[1].args[0]
        assert coll1 == coll2

    @pytest.mark.asyncio
    async def test_different_system_ids_can_span_shards(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Across 10 random system IDs with 4 shards, we hit ≥2 distinct shards."""
        from akosha.storage.sharding import ShardRouter

        real_router = ShardRouter(num_shards=4)
        adapter = _adapter_mock()
        monkeypatch.setattr(_mod, "PgvectorAdapter", lambda settings: adapter)

        store = _store(shard_count=4, shard_router=real_router, embedding_dimension=DIM)
        await store.initialize()

        for i in range(10):
            await store.insert(_warm_record(system_id=f"sys-{i}"))

        # 4 shards × 10 IDs ⇒ should hit at least 2 distinct shards
        # (uniform hashing guarantees >1 with very high probability).
        distinct_shards = {
            call.args[0] for call in adapter.insert.await_args_list
        }
        assert len(distinct_shards) >= 2
        assert len(distinct_shards) <= 4
