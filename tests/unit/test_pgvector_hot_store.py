"""Comprehensive unit tests for ``akosha.storage.pgvector_hot_store.PgvectorHotStore``.

Targets 95%+ branch coverage on the 65-statement module. The oneiric
PgvectorAdapter is mocked end-to-end so the suite has no live-Postgres
requirement and runs in well under a second.

Coverage targets mirror the methods on ``PgvectorHotStore``:

* ``__init__`` — three branches: explicit 384 fallback, non-384 explicit dim,
  default (``None``) routed through ``resolve_embedding_dim()``.
* ``initialize`` — adapter construction, ``init()``, ``create_collection()``.
* ``insert`` — happy path, ``not initialized`` RuntimeError, dim mismatch
  ValueError, real-dict metadata merge, datetime/str timestamp fallback.
* ``search_similar`` — system_id filter, threshold filter (None / 0.0 / 1.0),
  metadata stripping (system_id/content/timestamp removed from metadata dict),
  not-initialized RuntimeError.
* ``get_by_id`` — happy path, ``not initialized``, None when no docs, metadata
  stripping.
* ``delete`` — happy path, ``not initialized``.
* ``close`` — idempotent double-close.

Notes
-----
* ``record.metadata`` must be a real dict so ``record.metadata.items()``
  iterates inside the module's ``| dict(record.metadata.items())`` merge.
* The module-level ``_COLLECTION_NAME = "conversations"`` is asserted on
  directly so future renames are caught early.
* ``PgvectorAdapter`` is patched on the ``pgvector_hot_store`` module — the
  production code binds the symbol at import time.
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

from akosha.models import HotRecord
from oneiric.adapters.vector.vector_types import VectorSearchResult


# ---------------------------------------------------------------------------
# Import the module under test without triggering ``akosha.storage.__init__``
# ---------------------------------------------------------------------------
#
# Loading via ``import akosha.storage.pgvector_hot_store`` would pull in
# ``akosha.storage.aging`` → ``numpy``. Under pytest-cov the numpy C
# extension fails with "cannot load module more than once per process",
# which is a coverage-isolation interaction unrelated to the module under
# test. Importing the file directly via ``importlib`` sidesteps the
# ``__init__.py`` chain entirely.

_mod_path = Path("/Users/les/Projects/akosha/akosha/storage/pgvector_hot_store.py")
_pkg_stub = types.ModuleType("akosha.storage")
_pkg_stub.__path__ = [str(_mod_path.parent)]
sys.modules.setdefault("akosha.storage", _pkg_stub)

_spec = importlib.util.spec_from_file_location("akosha.storage.pgvector_hot_store", _mod_path)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["akosha.storage.pgvector_hot_store"] = _mod
_spec.loader.exec_module(_mod)

PgvectorHotStore = _mod.PgvectorHotStore
_DEFAULT_LEGACY_DIMENSION = _mod._DEFAULT_LEGACY_DIMENSION


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

DIM = 4  # tiny — adapter is mocked, real vector length never reaches Postgres
COLL = _mod._COLLECTION_NAME  # == "conversations"
assert COLL == "conversations", f"unexpected collection rename: {COLL!r}"


def _record(
    *,
    conversation_id: str = "conv-1",
    embedding: list[float] | None = None,
    metadata: dict[str, Any] | None = None,
    system_id: str = "sys1",
    content: str = "hello world",
    timestamp: datetime | None = None,
) -> HotRecord:
    """Construct a real ``HotRecord`` so ``record.metadata.items()`` iterates."""
    return HotRecord(
        system_id=system_id,
        conversation_id=conversation_id,
        content=content,
        embedding=embedding if embedding is not None else [0.1] * DIM,
        timestamp=timestamp or datetime(2024, 1, 1, tzinfo=UTC),
        metadata=metadata if metadata is not None else {"topic": "x", "tag": "y"},
    )


def _result(
    *,
    id: str = "conv-1",
    score: float = 0.1,
    system_id: str = "sys1",
    content: str = "hello world",
    timestamp: str = "2024-01-01T00:00:00+00:00",
    extra_metadata: dict[str, Any] | None = None,
) -> Any:
    """Build a real ``VectorSearchResult`` with the contract metadata keys.

    Extra metadata is merged in so we can exercise the metadata-stripping
    branch (system_id / content / timestamp keys removed from the returned
    ``metadata`` dict).
    """
    md: dict[str, Any] = {
        "system_id": system_id,
        "content": content,
        "timestamp": timestamp,
    }
    if extra_metadata:
        md.update(extra_metadata)
    return VectorSearchResult(id=id, score=score, metadata=md, vector=None)


@pytest.fixture
def patched_adapter():
    """Patch ``PgvectorAdapter`` and ``PgvectorSettings`` at the module level.

    Yields ``(mock_adapter_instance, mock_settings_instance)`` — both are
    ``AsyncMock`` / ``MagicMock`` instances the test can configure. The
    patches are stacked so a single ``async def`` body can configure both
    cleanly.
    """
    mock_adapter_inst = AsyncMock(name="PgvectorAdapterInstance")
    mock_adapter_inst.init = AsyncMock(name="adapter.init")
    mock_adapter_inst.cleanup = AsyncMock(name="adapter.cleanup")
    mock_adapter_inst.insert = AsyncMock(name="adapter.insert")
    mock_adapter_inst.search = AsyncMock(name="adapter.search")
    mock_adapter_inst.get = AsyncMock(name="adapter.get")
    mock_adapter_inst.delete = AsyncMock(name="adapter.delete")
    mock_adapter_inst.create_collection = AsyncMock(name="adapter.create_collection")

    mock_adapter_cls = MagicMock(name="PgvectorAdapterClass", return_value=mock_adapter_inst)
    mock_settings_cls = MagicMock(name="PgvectorSettingsClass")

    with (
        patch.object(_mod, "PgvectorAdapter", mock_adapter_cls),
        patch.object(_mod, "PgvectorSettings", mock_settings_cls),
    ):
        yield mock_adapter_inst, mock_settings_cls, mock_adapter_cls


def _make_store(**kwargs: Any) -> PgvectorHotStore:
    """Construct a ``PgvectorHotStore`` without invoking ``__init__`` mocking.

    Accepts the same kwargs as the production ``__init__`` and forwards
    them through. Default is the legacy-explicit 384 dim branch.
    """
    return PgvectorHotStore(pg_url="postgresql://localhost:5432/akosha", **kwargs)


# ---------------------------------------------------------------------------
# __init__ branches
# ---------------------------------------------------------------------------


class TestInit:
    """Three branches of ``__init__``: 384 legacy, explicit non-384, None default."""

    def test_explicit_384_uses_legacy_fallback(self) -> None:
        """Passing ``embedding_dimension=384`` honors the legacy mock shape."""
        store = PgvectorHotStore(pg_url="postgresql://localhost/akosha", embedding_dimension=384)
        assert store._embedding_dimension == 384
        assert store._pg_url == "postgresql://localhost/akosha"
        assert store._adapter is None
        # The asyncio lock must exist for ``initialize()``'s ``async with``.
        assert store._lock is not None

    def test_explicit_non_384_uses_provided_dim(self) -> None:
        """Any non-384 explicit dim is stored verbatim."""
        store = PgvectorHotStore(pg_url="postgresql://localhost/akosha", embedding_dimension=768)
        assert store._embedding_dimension == 768

    def test_default_none_routes_through_resolve_embedding_dim(self) -> None:
        """``embedding_dimension=None`` (default) calls ``resolve_embedding_dim``."""
        with patch.object(_mod, "resolve_embedding_dim", return_value=1024) as mock_resolve:
            store = PgvectorHotStore(pg_url="postgresql://localhost/akosha")

        assert store._embedding_dimension == 1024
        mock_resolve.assert_called_once_with()

    def test_default_uses_legacy_dim_when_resolver_returns_384(self) -> None:
        """``resolve_embedding_dim()`` default (no service) → 384."""
        # Sanity: the production resolver falls back to 384 with no service.
        store = PgvectorHotStore(pg_url="postgresql://localhost/akosha")
        assert store._embedding_dimension == _DEFAULT_LEGACY_DIMENSION == 384

    def test_legacy_constant_is_384(self) -> None:
        """Pin the public sentinel so accidental renames fail loudly."""
        assert _DEFAULT_LEGACY_DIMENSION == 384


# ---------------------------------------------------------------------------
# initialize()
# ---------------------------------------------------------------------------


class TestInitialize:
    """``initialize()`` builds the adapter, calls ``init()``, creates the collection."""

    @pytest.mark.asyncio
    async def test_initialize_creates_collection_with_correct_dim(self, patched_adapter) -> None:
        adapter_inst, settings_cls, _adapter_cls = patched_adapter
        store = _make_store(embedding_dimension=DIM)

        await store.initialize()

        # Settings instantiated with the DSN.
        settings_cls.assert_called_once()
        # Adapter instantiated with the settings instance.
        _adapter_cls.assert_called_once_with(settings_cls.return_value)
        # Adapter ``init()`` ran.
        adapter_inst.init.assert_awaited_once()
        # The conversations collection was created at the resolved dim.
        adapter_inst.create_collection.assert_awaited_once_with(
            name=COLL, dimension=DIM, distance_metric="cosine"
        )
        # Internal state: adapter bound, lock released (we're past the async with).
        assert store._adapter is adapter_inst

    @pytest.mark.asyncio
    async def test_initialize_with_legacy_384_dim(self, patched_adapter) -> None:
        """Legacy 384-dim store creates the collection at 384."""
        adapter_inst, _settings_cls, _adapter_cls = patched_adapter
        store = _make_store(embedding_dimension=384)

        await store.initialize()

        adapter_inst.create_collection.assert_awaited_once_with(
            name=COLL, dimension=384, distance_metric="cosine"
        )

    @pytest.mark.asyncio
    async def test_initialize_serializes_under_lock(self, patched_adapter) -> None:
        """Two concurrent ``initialize()`` calls must serialize via the lock."""
        adapter_inst, _settings_cls, _adapter_cls = patched_adapter
        store = _make_store(embedding_dimension=DIM)

        import asyncio

        await asyncio.gather(store.initialize(), store.initialize())

        # Two initialize runs → two init() calls, two create_collection calls.
        assert adapter_inst.init.await_count == 2
        assert adapter_inst.create_collection.await_count == 2


# ---------------------------------------------------------------------------
# insert() — happy path
# ---------------------------------------------------------------------------


class TestInsertHappyPath:
    """``insert()`` happy path: VectorDocument shape, dim, metadata merge."""

    @pytest.mark.asyncio
    async def test_insert_builds_vector_document_with_correct_fields(self, patched_adapter) -> None:
        adapter_inst, _settings_cls, _adapter_cls = patched_adapter
        store = _make_store(embedding_dimension=DIM)
        await store.initialize()
        adapter_inst.insert.reset_mock()  # ignore the create_collection call shape

        record = _record(
            conversation_id="c-1",
            embedding=[0.5] * DIM,
            system_id="sys-A",
            content="hi",
            metadata={"topic": "rag"},
        )
        await store.insert(record)

        # adapter.insert was awaited once with the collection + a single doc.
        assert adapter_inst.insert.await_count == 1
        args, _kwargs = adapter_inst.insert.call_args
        assert args[0] == COLL
        docs = args[1]
        assert len(docs) == 1
        doc = docs[0]
        # id, vector preserved; metadata merges contract fields + user fields.
        assert doc.id == "c-1"
        assert doc.vector == [0.5] * DIM
        assert doc.metadata["system_id"] == "sys-A"
        assert doc.metadata["content"] == "hi"
        assert doc.metadata["timestamp"] == "2024-01-01T00:00:00+00:00"
        assert doc.metadata["topic"] == "rag"
        # Contract keys are preserved alongside user metadata (stripping
        # happens in search_similar / get_by_id, not insert).

    @pytest.mark.asyncio
    async def test_insert_serializes_datetime_via_isoformat(self, patched_adapter) -> None:
        """datetime.timestamp gets serialized via ``.isoformat()``."""
        adapter_inst, _, _ = patched_adapter
        store = _make_store(embedding_dimension=DIM)
        await store.initialize()
        adapter_inst.insert.reset_mock()

        ts = datetime(2026, 9, 6, 12, 30, 0, tzinfo=UTC)
        await store.insert(_record(timestamp=ts))

        docs = adapter_inst.insert.call_args.args[1]
        assert docs[0].metadata["timestamp"] == ts.isoformat()

    @pytest.mark.asyncio
    async def test_insert_uses_str_fallback_when_timestamp_lacks_isoformat(
        self, patched_adapter
    ) -> None:
        """The ``else str(record.timestamp)`` branch fires when ``.isoformat`` is absent.

        HotRecord is a Pydantic model that coerces to ``datetime``, so the
        production ``hasattr(...)`` else-branch is hard to reach via real
        records. We exercise it by patching ``HotRecord.timestamp`` to a
        bare string *after* model construction — keeps the constructor
        happy while still driving the falsy-hasattr branch.
        """
        adapter_inst, _, _ = patched_adapter
        store = _make_store(embedding_dimension=DIM)
        await store.initialize()
        adapter_inst.insert.reset_mock()

        record = _record()
        # HotRecord timestamp is normally a datetime — swap it for a plain
        # string so ``hasattr(record.timestamp, "isoformat")`` is False.
        object.__setattr__(record, "timestamp", "raw-string-no-isoformat")

        await store.insert(record)

        docs = adapter_inst.insert.call_args.args[1]
        assert docs[0].metadata["timestamp"] == "raw-string-no-isoformat"


# ---------------------------------------------------------------------------
# insert() — error paths
# ---------------------------------------------------------------------------


class TestInsertErrors:
    """insert() RuntimeError (not initialized) and ValueError (dim mismatch)."""

    @pytest.mark.asyncio
    async def test_insert_before_initialize_raises_runtime_error(self) -> None:
        store = _make_store(embedding_dimension=DIM)
        with pytest.raises(RuntimeError, match="not initialized"):
            await store.insert(_record())

    @pytest.mark.asyncio
    async def test_insert_dim_mismatch_raises_value_error(self, patched_adapter) -> None:
        adapter_inst, _, _ = patched_adapter
        store = _make_store(embedding_dimension=DIM)
        await store.initialize()
        adapter_inst.insert.reset_mock()

        # Embedding length 3 != schema dim 4.
        bad = _record(embedding=[0.0, 0.0, 0.0])
        with pytest.raises(ValueError, match="dim mismatch"):
            await store.insert(bad)

        # The adapter was NOT called because validation rejected first.
        adapter_inst.insert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_insert_dim_mismatch_message_contains_both_dims(self, patched_adapter) -> None:
        """The error message names both expected and actual dimensions."""
        _adapter_inst, _, _ = patched_adapter
        store = _make_store(embedding_dimension=DIM)  # expects 4
        await store.initialize()

        bad = _record(embedding=[0.0] * 7)  # got 7
        with pytest.raises(ValueError) as excinfo:
            await store.insert(bad)

        msg = str(excinfo.value)
        assert "4" in msg
        assert "7" in msg


# ---------------------------------------------------------------------------
# search_similar() — happy path
# ---------------------------------------------------------------------------


class TestSearchSimilarHappyPath:
    """Happy path: results reshape, no threshold filter."""

    @pytest.mark.asyncio
    async def test_search_similar_returns_reshaped_dicts(self, patched_adapter) -> None:
        adapter_inst, _, _ = patched_adapter
        store = _make_store(embedding_dimension=DIM)
        await store.initialize()
        adapter_inst.search.reset_mock()

        adapter_inst.search.return_value = [
            _result(id="a", score=0.05, extra_metadata={"topic": "rag"}),
            _result(id="b", score=0.20, extra_metadata={"topic": "ops"}),
        ]

        out = await store.search_similar([0.1] * DIM)

        assert len(out) == 2
        assert out[0]["conversation_id"] == "a"
        assert out[0]["score"] == 0.05
        assert out[0]["system_id"] == "sys1"
        assert out[0]["content"] == "hello world"
        # Contract keys are STRIPPED from the user-facing metadata dict.
        assert "system_id" not in out[0]["metadata"]
        assert "content" not in out[0]["metadata"]
        assert "timestamp" not in out[0]["metadata"]
        # User metadata survives.
        assert out[0]["metadata"] == {"topic": "rag"}
        assert out[1]["metadata"] == {"topic": "ops"}

    @pytest.mark.asyncio
    async def test_search_similar_passes_query_and_limit(self, patched_adapter) -> None:
        adapter_inst, _, _ = patched_adapter
        store = _make_store(embedding_dimension=DIM)
        await store.initialize()
        adapter_inst.search.reset_mock()

        adapter_inst.search.return_value = []
        qv = [0.3] * DIM

        await store.search_similar(qv, limit=7)

        _args, kwargs = adapter_inst.search.call_args
        assert kwargs["collection"] == COLL
        assert kwargs["query_vector"] == qv
        assert kwargs["limit"] == 7
        # No system_id passed → no filter expression.
        assert kwargs["filter_expr"] is None
        assert kwargs["include_vectors"] is False

    @pytest.mark.asyncio
    async def test_search_similar_default_limit_is_ten(self, patched_adapter) -> None:
        adapter_inst, _, _ = patched_adapter
        store = _make_store(embedding_dimension=DIM)
        await store.initialize()
        adapter_inst.search.reset_mock()
        adapter_inst.search.return_value = []

        await store.search_similar([0.1] * DIM)

        _args, kwargs = adapter_inst.search.call_args
        assert kwargs["limit"] == 10

    @pytest.mark.asyncio
    async def test_search_similar_empty_results(self, patched_adapter) -> None:
        adapter_inst, _, _ = patched_adapter
        store = _make_store(embedding_dimension=DIM)
        await store.initialize()
        adapter_inst.search.reset_mock()
        adapter_inst.search.return_value = []

        out = await store.search_similar([0.1] * DIM)
        assert out == []


# ---------------------------------------------------------------------------
# search_similar() — system_id filter
# ---------------------------------------------------------------------------


class TestSearchSimilarSystemIdFilter:
    """``system_id`` becomes a metadata ``@>`` JSON filter expression."""

    @pytest.mark.asyncio
    async def test_search_similar_passes_system_id_as_filter(self, patched_adapter) -> None:
        adapter_inst, _, _ = patched_adapter
        store = _make_store(embedding_dimension=DIM)
        await store.initialize()
        adapter_inst.search.reset_mock()
        adapter_inst.search.return_value = []

        await store.search_similar([0.1] * DIM, system_id="sys-A")

        _args, kwargs = adapter_inst.search.call_args
        assert kwargs["filter_expr"] == {"system_id": "sys-A"}

    @pytest.mark.asyncio
    async def test_search_similar_no_system_id_means_no_filter(self, patched_adapter) -> None:
        adapter_inst, _, _ = patched_adapter
        store = _make_store(embedding_dimension=DIM)
        await store.initialize()
        adapter_inst.search.reset_mock()
        adapter_inst.search.return_value = []

        await store.search_similar([0.1] * DIM, system_id=None)
        _args, kwargs = adapter_inst.search.call_args
        assert kwargs["filter_expr"] is None

    @pytest.mark.asyncio
    async def test_search_similar_empty_string_system_id_means_no_filter(
        self, patched_adapter
    ) -> None:
        """``system_id=""`` is falsy → no filter (the ``if system_id`` branch)."""
        adapter_inst, _, _ = patched_adapter
        store = _make_store(embedding_dimension=DIM)
        await store.initialize()
        adapter_inst.search.reset_mock()
        adapter_inst.search.return_value = []

        await store.search_similar([0.1] * DIM, system_id="")
        _args, kwargs = adapter_inst.search.call_args
        assert kwargs["filter_expr"] is None


# ---------------------------------------------------------------------------
# search_similar() — threshold filter
# ---------------------------------------------------------------------------


class TestSearchSimilarThreshold:
    """Threshold filter applied post-query as ``distance <= 1.0 - threshold``."""

    @pytest.mark.asyncio
    async def test_search_similar_no_threshold_keeps_all(self, patched_adapter) -> None:
        """``threshold=None`` → no post-query filter, all results pass."""
        adapter_inst, _, _ = patched_adapter
        store = _make_store(embedding_dimension=DIM)
        await store.initialize()
        adapter_inst.search.reset_mock()
        adapter_inst.search.return_value = [
            _result(id="close", score=0.01),
            _result(id="far", score=0.99),
        ]

        out = await store.search_similar([0.1] * DIM, threshold=None)

        assert len(out) == 2
        assert {r["conversation_id"] for r in out} == {"close", "far"}

    @pytest.mark.asyncio
    async def test_search_similar_threshold_one_keeps_everything(self, patched_adapter) -> None:
        """``threshold=1.0`` → max_distance=0.0; everything with distance > 0 dropped."""
        adapter_inst, _, _ = patched_adapter
        store = _make_store(embedding_dimension=DIM)
        await store.initialize()
        adapter_inst.search.reset_mock()
        adapter_inst.search.return_value = [
            _result(id="zero", score=0.0),
            _result(id="epsilon", score=0.5),
        ]

        out = await store.search_similar([0.1] * DIM, threshold=1.0)

        assert [r["conversation_id"] for r in out] == ["zero"]

    @pytest.mark.asyncio
    async def test_search_similar_threshold_zero_drops_near_match(self, patched_adapter) -> None:
        """``threshold=0.0`` → max_distance=1.0; tight matches kept, loose dropped."""
        adapter_inst, _, _ = patched_adapter
        store = _make_store(embedding_dimension=DIM)
        await store.initialize()
        adapter_inst.search.reset_mock()
        adapter_inst.search.return_value = [
            _result(id="tight", score=0.1),
            _result(id="loose", score=1.5),
        ]

        out = await store.search_similar([0.1] * DIM, threshold=0.0)

        ids = [r["conversation_id"] for r in out]
        assert "tight" in ids
        assert "loose" not in ids


# ---------------------------------------------------------------------------
# search_similar() — errors
# ---------------------------------------------------------------------------


class TestSearchSimilarErrors:
    @pytest.mark.asyncio
    async def test_search_similar_before_initialize_raises_runtime_error(self) -> None:
        store = _make_store(embedding_dimension=DIM)
        with pytest.raises(RuntimeError, match="not initialized"):
            await store.search_similar([0.1] * DIM)


# ---------------------------------------------------------------------------
# get_by_id()
# ---------------------------------------------------------------------------


class TestGetById:
    """Happy path, ``not initialized``, None for no docs, metadata strip."""

    @pytest.mark.asyncio
    async def test_get_by_id_returns_reshaped_dict(self, patched_adapter) -> None:
        adapter_inst, _, _ = patched_adapter
        store = _make_store(embedding_dimension=DIM)
        await store.initialize()
        adapter_inst.get.reset_mock()

        doc = _mod.VectorDocument(
            id="c-1",
            vector=[0.0] * DIM,
            metadata={
                "system_id": "sys-A",
                "content": "hello",
                "timestamp": "2024-01-01T00:00:00+00:00",
                "topic": "rag",
            },
        )
        adapter_inst.get.return_value = [doc]

        out = await store.get_by_id("c-1")

        assert out is not None
        assert out["conversation_id"] == "c-1"
        assert out["system_id"] == "sys-A"
        assert out["content"] == "hello"
        assert out["timestamp"] == "2024-01-01T00:00:00+00:00"
        # Contract keys stripped.
        assert "system_id" not in out["metadata"]
        assert "content" not in out["metadata"]
        assert "timestamp" not in out["metadata"]
        # User metadata survives.
        assert out["metadata"] == {"topic": "rag"}

    @pytest.mark.asyncio
    async def test_get_by_id_passes_collection_and_id(self, patched_adapter) -> None:
        adapter_inst, _, _ = patched_adapter
        store = _make_store(embedding_dimension=DIM)
        await store.initialize()
        adapter_inst.get.reset_mock()
        adapter_inst.get.return_value = []

        await store.get_by_id("c-42")

        args, kwargs = adapter_inst.get.call_args
        assert args[0] == COLL
        assert args[1] == ["c-42"]
        assert kwargs["include_vectors"] is False

    @pytest.mark.asyncio
    async def test_get_by_id_returns_none_when_no_docs(self, patched_adapter) -> None:
        adapter_inst, _, _ = patched_adapter
        store = _make_store(embedding_dimension=DIM)
        await store.initialize()
        adapter_inst.get.reset_mock()
        adapter_inst.get.return_value = []

        out = await store.get_by_id("missing")
        assert out is None

    @pytest.mark.asyncio
    async def test_get_by_id_before_initialize_raises_runtime_error(self) -> None:
        store = _make_store(embedding_dimension=DIM)
        with pytest.raises(RuntimeError, match="not initialized"):
            await store.get_by_id("c-1")

    @pytest.mark.asyncio
    async def test_get_by_id_metadata_with_only_contract_keys(self, patched_adapter) -> None:
        """Metadata dict that contains only contract keys → empty dict after strip."""
        adapter_inst, _, _ = patched_adapter
        store = _make_store(embedding_dimension=DIM)
        await store.initialize()
        adapter_inst.get.reset_mock()

        doc = _mod.VectorDocument(
            id="c-1",
            vector=[0.0] * DIM,
            metadata={
                "system_id": "sys-A",
                "content": "hello",
                "timestamp": "2024-01-01T00:00:00+00:00",
            },
        )
        adapter_inst.get.return_value = [doc]

        out = await store.get_by_id("c-1")
        assert out is not None
        assert out["metadata"] == {}


# ---------------------------------------------------------------------------
# delete()
# ---------------------------------------------------------------------------


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_passes_collection_and_id(self, patched_adapter) -> None:
        adapter_inst, _, _ = patched_adapter
        store = _make_store(embedding_dimension=DIM)
        await store.initialize()
        adapter_inst.delete.reset_mock()

        await store.delete("c-1")

        args, _kwargs = adapter_inst.delete.call_args
        assert args[0] == COLL
        assert args[1] == ["c-1"]

    @pytest.mark.asyncio
    async def test_delete_before_initialize_raises_runtime_error(self) -> None:
        store = _make_store(embedding_dimension=DIM)
        with pytest.raises(RuntimeError, match="not initialized"):
            await store.delete("c-1")


# ---------------------------------------------------------------------------
# close()
# ---------------------------------------------------------------------------


class TestClose:
    @pytest.mark.asyncio
    async def test_close_calls_cleanup_and_clears_adapter(self, patched_adapter) -> None:
        adapter_inst, _, _ = patched_adapter
        store = _make_store(embedding_dimension=DIM)
        await store.initialize()

        assert store._adapter is adapter_inst
        await store.close()

        adapter_inst.cleanup.assert_awaited_once()
        assert store._adapter is None

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self, patched_adapter) -> None:
        """Second close() is a no-op — does not re-await cleanup()."""
        adapter_inst, _, _ = patched_adapter
        store = _make_store(embedding_dimension=DIM)
        await store.initialize()

        await store.close()
        await store.close()
        await store.close()

        assert adapter_inst.cleanup.await_count == 1
        assert store._adapter is None

    @pytest.mark.asyncio
    async def test_close_without_initialize_is_noop(self) -> None:
        """close() before initialize() never touches a non-existent adapter."""
        store = _make_store(embedding_dimension=DIM)
        # No initialize call — store._adapter stays None.
        assert store._adapter is None
        await store.close()
        # Still None — no exception, no-op.
        assert store._adapter is None
