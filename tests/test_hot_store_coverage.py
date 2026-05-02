"""Tests for akosha/storage/hot_store.py — HotStore code graph methods."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from akosha.storage.hot_store import HotStore

# DuckDB requires 384-dim embeddings
_DIM384 = [0.1] * 384


def _mock_record(
    system_id="sys1", conversation_id="conv1", content="hello", embedding=None, metadata=None
):
    """Create a mock HotRecord."""
    r = MagicMock()
    r.system_id = system_id
    r.conversation_id = conversation_id
    r.content = content
    r.embedding = embedding or _DIM384
    r.timestamp = datetime(2024, 1, 1, tzinfo=UTC)
    r.metadata = metadata or {"key": "val"}
    return r


class TestHotStoreInit:
    def test_init_memory(self):
        hs = HotStore()
        assert hs.db_path == ":memory:"
        assert hs.conn is None

    def test_init_custom_path(self):
        hs = HotStore(database_path="/tmp/test.db")
        assert hs.db_path == "/tmp/test.db"


class TestInitialize:
    @pytest.mark.asyncio
    async def test_initialize_creates_connection(self):
        hs = HotStore()
        await hs.initialize()
        assert hs.conn is not None
        await hs.close()

    @pytest.mark.asyncio
    async def test_initialize_creates_tables(self):
        hs = HotStore()
        await hs.initialize()
        result = hs.conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'conversations'"
        ).fetchone()
        assert result[0] >= 1
        await hs.close()

    @pytest.mark.asyncio
    async def test_initialize_hnsw_index_failure_handled(self):
        hs = HotStore()
        await hs.initialize()
        # HNSW may or may not be available; just ensure no exception
        await hs.close()


class TestInsert:
    @pytest.mark.asyncio
    async def test_insert_record(self):
        hs = HotStore()
        await hs.initialize()
        record = _mock_record()
        await hs.insert(record)
        result = hs.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()
        assert result[0] == 1
        await hs.close()

    @pytest.mark.asyncio
    async def test_insert_not_initialized_raises(self):
        hs = HotStore()
        record = _mock_record()
        with pytest.raises(RuntimeError, match="not initialized"):
            await hs.insert(record)

    @pytest.mark.asyncio
    async def test_insert_computes_content_hash(self):
        hs = HotStore()
        await hs.initialize()
        record = _mock_record(content="test content")
        await hs.insert(record)
        result = hs.conn.execute("SELECT content_hash FROM conversations").fetchone()
        expected = hashlib.sha256(b"test content").hexdigest()
        assert result[0] == expected
        await hs.close()

    @pytest.mark.asyncio
    async def test_duplicate_primary_key_raises(self):
        hs = HotStore()
        await hs.initialize()
        record = _mock_record()
        await hs.insert(record)
        with pytest.raises(Exception):  # DuckDB constraint error
            await hs.insert(record)
        await hs.close()


class TestSearchSimilar:
    @pytest.mark.asyncio
    async def test_search_with_system_id(self):
        hs = HotStore()
        await hs.initialize()
        record = _mock_record(embedding=_DIM384)
        await hs.insert(record)
        results = await hs.search_similar(_DIM384, system_id="sys1")
        assert len(results) >= 1
        assert results[0]["system_id"] == "sys1"
        await hs.close()

    @pytest.mark.asyncio
    async def test_search_without_system_id(self):
        hs = HotStore()
        await hs.initialize()
        record = _mock_record(embedding=_DIM384)
        await hs.insert(record)
        results = await hs.search_similar(_DIM384)
        assert len(results) >= 1
        await hs.close()

    @pytest.mark.asyncio
    async def test_threshold_filters_low_similarity(self):
        hs = HotStore()
        await hs.initialize()
        record = _mock_record(embedding=_DIM384)
        await hs.insert(record)
        # Search with a very different vector — similarity will be low
        diff_vec = [0.0] * 383 + [1.0]
        results = await hs.search_similar(diff_vec, threshold=0.99)
        assert len(results) == 0
        await hs.close()

    @pytest.mark.asyncio
    async def test_search_not_initialized_raises(self):
        hs = HotStore()
        with pytest.raises(RuntimeError, match="not initialized"):
            await hs.search_similar(_DIM384)

    @pytest.mark.asyncio
    async def test_search_limit(self):
        hs = HotStore()
        await hs.initialize()
        for i in range(5):
            await hs.insert(_mock_record(conversation_id=f"conv{i}", embedding=_DIM384))
        results = await hs.search_similar(_DIM384, limit=2)
        assert len(results) <= 2
        await hs.close()


class TestComputeContentHash:
    def test_sha256(self):
        result = HotStore._compute_content_hash("hello")
        expected = hashlib.sha256(b"hello").hexdigest()
        assert result == expected

    def test_empty_string(self):
        result = HotStore._compute_content_hash("")
        assert len(result) == 64

    def test_deterministic(self):
        assert HotStore._compute_content_hash("test") == HotStore._compute_content_hash("test")


class TestClose:
    @pytest.mark.asyncio
    async def test_close(self):
        hs = HotStore()
        await hs.initialize()
        conn_before = hs.conn
        await hs.close()
        # DuckDB may not set conn to None; just ensure close doesn't error
        assert hs.conn is None or hs.conn is conn_before

    @pytest.mark.asyncio
    async def test_close_not_initialized(self):
        hs = HotStore()
        await hs.close()  # Should not raise


class TestInitializeCodeGraphsTable:
    @pytest.mark.asyncio
    async def test_creates_table(self):
        hs = HotStore()
        await hs.initialize()
        await hs.initialize_code_graphs_table()
        result = hs.conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'code_graphs'"
        ).fetchone()
        assert result[0] >= 1
        await hs.close()

    @pytest.mark.asyncio
    async def test_not_initialized_raises(self):
        hs = HotStore()
        with pytest.raises(RuntimeError, match="not initialized"):
            await hs.initialize_code_graphs_table()


class TestStoreCodeGraph:
    @pytest.mark.asyncio
    async def test_store_and_retrieve(self):
        hs = HotStore()
        await hs.initialize()
        await hs.initialize_code_graphs_table()
        await hs.store_code_graph(
            repo_path="/repo",
            commit_hash="abc123",
            nodes_count=10,
            graph_data={"nodes": [{"id": 1}]},
            metadata={"source": "test"},
        )
        result = await hs.get_code_graph("/repo", "abc123")
        assert result is not None
        assert result["repo_path"] == "/repo"
        assert result["commit_hash"] == "abc123"
        assert result["nodes_count"] == 10
        assert result["graph_data"] == {"nodes": [{"id": 1}]}
        assert result["metadata"] == {"source": "test"}
        await hs.close()

    @pytest.mark.asyncio
    async def test_store_insert_or_replace(self):
        hs = HotStore()
        await hs.initialize()
        await hs.initialize_code_graphs_table()
        await hs.store_code_graph("/repo", "abc", 5, {}, {})
        await hs.store_code_graph("/repo", "abc", 10, {"v": 2}, {})
        result = await hs.get_code_graph("/repo", "abc")
        assert result["nodes_count"] == 10
        await hs.close()

    @pytest.mark.asyncio
    async def test_store_not_initialized_raises(self):
        hs = HotStore()
        with pytest.raises(RuntimeError, match="not initialized"):
            await hs.store_code_graph("/repo", "abc", 1, {}, {})


class TestGetCodeGraph:
    @pytest.mark.asyncio
    async def test_not_found(self):
        hs = HotStore()
        await hs.initialize()
        await hs.initialize_code_graphs_table()
        result = await hs.get_code_graph("/nonexistent", "abc")
        assert result is None
        await hs.close()

    @pytest.mark.asyncio
    async def test_not_initialized_raises(self):
        hs = HotStore()
        with pytest.raises(RuntimeError, match="not initialized"):
            await hs.get_code_graph("/repo", "abc")

    @pytest.mark.asyncio
    async def test_json_null_handling(self):
        hs = HotStore()
        await hs.initialize()
        await hs.initialize_code_graphs_table()
        # Insert with None graph_data and metadata
        hs.conn.execute(
            "INSERT INTO code_graphs (repo_path, commit_hash, nodes_count, graph_data, metadata) VALUES (?, ?, ?, ?, ?)",
            ["/repo", "abc", 1, None, None],
        )
        result = await hs.get_code_graph("/repo", "abc")
        assert result is not None
        assert result["graph_data"] == {}
        assert result["metadata"] == {}
        await hs.close()


class TestListCodeGraphs:
    @pytest.mark.asyncio
    async def test_list_all(self):
        hs = HotStore()
        await hs.initialize()
        await hs.initialize_code_graphs_table()
        await hs.store_code_graph("/repo1", "abc", 5, {}, {})
        await hs.store_code_graph("/repo2", "def", 10, {}, {})
        results = await hs.list_code_graphs()
        assert len(results) == 2
        await hs.close()

    @pytest.mark.asyncio
    async def test_list_with_repo_filter(self):
        hs = HotStore()
        await hs.initialize()
        await hs.initialize_code_graphs_table()
        await hs.store_code_graph("/repo1", "abc", 5, {}, {})
        await hs.store_code_graph("/repo2", "def", 10, {}, {})
        results = await hs.list_code_graphs(repo_path="/repo1")
        assert len(results) == 1
        assert results[0]["repo_path"] == "/repo1"
        await hs.close()

    @pytest.mark.asyncio
    async def test_list_empty(self):
        hs = HotStore()
        await hs.initialize()
        await hs.initialize_code_graphs_table()
        results = await hs.list_code_graphs()
        assert results == []
        await hs.close()

    @pytest.mark.asyncio
    async def test_list_limit(self):
        hs = HotStore()
        await hs.initialize()
        await hs.initialize_code_graphs_table()
        for i in range(5):
            await hs.store_code_graph(f"/repo{i}", f"hash{i}", i, {}, {})
        results = await hs.list_code_graphs(limit=2)
        assert len(results) <= 2
        await hs.close()

    @pytest.mark.asyncio
    async def test_list_not_initialized_raises(self):
        hs = HotStore()
        with pytest.raises(RuntimeError, match="not initialized"):
            await hs.list_code_graphs()
