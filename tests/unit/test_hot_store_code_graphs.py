"""Branch-focused tests for hot store code graph helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from akosha.storage.hot_store import HotStore


class FakeConnection:
    """DuckDB-like connection for exercising index creation branches."""

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.closed = False
        self._index_attempts = 0

    def execute(self, sql: str, params: list[object] | None = None) -> FakeConnection:
        self.executed.append(sql)
        if "CREATE INDEX IF NOT EXISTS code_graphs_repo_index" in sql:
            self._index_attempts += 1
            raise RuntimeError("repo index failed")
        if "CREATE INDEX IF NOT EXISTS code_graphs_nodes_index" in sql:
            self._index_attempts += 1
            raise RuntimeError("nodes index failed")
        if "CREATE INDEX IF NOT EXISTS code_graphs_ingested_index" in sql:
            self._index_attempts += 1
            raise RuntimeError("ingested index failed")
        return self

    def fetchone(self) -> tuple[object, ...] | None:
        return None

    def fetchall(self) -> list[tuple[object, ...]]:
        return []

    def close(self) -> None:
        self.closed = True


class FakeInitConnection:
    """DuckDB-like connection for exercising hot-store initialization branches."""

    def __init__(self) -> None:
        self.executed: list[str] = []

    def execute(self, sql: str, params: list[object] | None = None) -> FakeInitConnection:
        self.executed.append(sql)
        if "CREATE INDEX IF NOT EXISTS system_id_index" in sql:
            raise RuntimeError("system index failed")
        if "CREATE INDEX IF NOT EXISTS timestamp_index" in sql:
            raise RuntimeError("timestamp index failed")
        if "CREATE INDEX IF NOT EXISTS system_timestamp_index" in sql:
            raise RuntimeError("composite index failed")
        return self

    def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_close_without_connection_is_noop() -> None:
    """Closing an uninitialized store should not raise."""
    store = HotStore()

    await store.close()


@pytest.mark.asyncio
async def test_initialize_code_graphs_table_without_initialization_raises() -> None:
    """Code graph initialization should require an active connection."""
    store = HotStore()

    with pytest.raises(RuntimeError, match="not initialized"):
        await store.initialize_code_graphs_table()


@pytest.mark.asyncio
async def test_store_get_and_list_code_graphs_round_trip() -> None:
    """Code graph helpers should round-trip through DuckDB."""
    store = HotStore()
    await store.initialize()
    await store.initialize_code_graphs_table()

    graph_data = {"nodes": [{"id": 1}], "edges": []}
    metadata = {"branch": "main"}
    await store.store_code_graph(
        repo_path="/repo",
        commit_hash="abc123",
        nodes_count=1,
        graph_data=graph_data,
        metadata=metadata,
    )

    retrieved = await store.get_code_graph("/repo", "abc123")
    assert retrieved is not None
    assert retrieved["repo_path"] == "/repo"
    assert retrieved["commit_hash"] == "abc123"
    assert retrieved["nodes_count"] == 1
    assert retrieved["graph_data"] == graph_data
    assert retrieved["metadata"] == metadata
    assert retrieved["ingested_at"] is not None

    all_graphs = await store.list_code_graphs()
    filtered_graphs = await store.list_code_graphs(repo_path="/repo", limit=5)

    assert len(all_graphs) == 1
    assert all_graphs[0]["commit_hash"] == "abc123"
    assert filtered_graphs == all_graphs

    await store.close()


@pytest.mark.asyncio
async def test_code_graph_helpers_require_initialization() -> None:
    """Code graph helpers should fail closed when no connection exists."""
    store = HotStore()

    with pytest.raises(RuntimeError, match="not initialized"):
        await store.store_code_graph("/repo", "abc123", 1, {}, {})

    with pytest.raises(RuntimeError, match="not initialized"):
        await store.get_code_graph("/repo", "abc123")

    with pytest.raises(RuntimeError, match="not initialized"):
        await store.list_code_graphs()


@pytest.mark.asyncio
async def test_initialize_code_graphs_table_logs_index_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Index creation failures should be logged but not stop initialization."""
    store = HotStore()
    fake_conn = FakeConnection()
    store.conn = fake_conn

    warnings: list[str] = []
    monkeypatch.setattr("akosha.storage.hot_store.logger.warning", lambda msg: warnings.append(msg))

    await store.initialize_code_graphs_table()

    assert any("repo_path index creation failed" in msg for msg in warnings)
    assert any("nodes_count index creation failed" in msg for msg in warnings)
    assert any("ingested_at index creation failed" in msg for msg in warnings)


@pytest.mark.asyncio
async def test_initialize_logs_conversation_index_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hot-store initialization should log and continue when conversation indexes fail."""
    store = HotStore()
    fake_conn = FakeInitConnection()
    monkeypatch.setattr(
        "akosha.storage.hot_store.duckdb.connect", lambda *_args, **_kwargs: fake_conn
    )

    warnings: list[str] = []
    monkeypatch.setattr("akosha.storage.hot_store.logger.warning", lambda msg: warnings.append(msg))

    await store.initialize()

    assert any("system_id index creation failed" in msg for msg in warnings)
    assert any("timestamp index creation failed" in msg for msg in warnings)
    assert any("Composite index creation failed" in msg for msg in warnings)


@pytest.mark.asyncio
async def test_get_code_graph_returns_none_for_short_row() -> None:
    """Rows shorter than the expected shape should be ignored."""
    store = HotStore()
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = ("repo", "commit", 1, "{}", "{}")
    store.conn = fake_conn

    assert await store.get_code_graph("/repo", "commit") is None


@pytest.mark.asyncio
async def test_close_closes_existing_connection() -> None:
    """Closing an initialized store should close the underlying connection."""
    store = HotStore()
    fake_conn = MagicMock()
    store.conn = fake_conn

    await store.close()

    fake_conn.close.assert_called_once()
