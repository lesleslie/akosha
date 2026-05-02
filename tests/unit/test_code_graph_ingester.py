"""Tests for akosha.ingestion.code_graph_ingester — CodeGraphIngester pull worker."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


class TestCodeGraphIngesterInit:
    """Test CodeGraphIngester construction and defaults."""

    def test_default_params(self):
        from akosha.ingestion.code_graph_ingester import CodeGraphIngester

        mock_store = MagicMock()
        ingester = CodeGraphIngester(hot_store=mock_store)
        assert ingester.hot_store is mock_store
        assert ingester.session_buddy_endpoint == "http://localhost:8678/mcp"
        assert ingester.poll_interval_seconds == 60
        assert ingester.max_concurrent_ingests == 10
        assert ingester._running is False
        assert ingester._poll_task is None
        assert ingester._http_client is None
        assert ingester._known_graph_ids == set()

    def test_custom_params(self):
        from akosha.ingestion.code_graph_ingester import CodeGraphIngester

        mock_store = MagicMock()
        ingester = CodeGraphIngester(
            hot_store=mock_store,
            session_buddy_endpoint="http://custom:9000/mcp",
            poll_interval_seconds=30,
            max_concurrent_ingests=5,
        )
        assert ingester.session_buddy_endpoint == "http://custom:9000/mcp"
        assert ingester.poll_interval_seconds == 30
        assert ingester.max_concurrent_ingests == 5


class TestCodeGraphIngesterStartStop:
    """Test start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_sets_running_and_creates_task(self):
        from akosha.ingestion.code_graph_ingester import CodeGraphIngester

        mock_store = MagicMock()
        ingester = CodeGraphIngester(hot_store=mock_store, poll_interval_seconds=999)
        await ingester.start()
        assert ingester._running is True
        assert ingester._poll_task is not None
        assert ingester._http_client is not None
        # Clean up
        await ingester.stop()

    @pytest.mark.asyncio
    async def test_start_when_already_running(self):
        from akosha.ingestion.code_graph_ingester import CodeGraphIngester

        mock_store = MagicMock()
        ingester = CodeGraphIngester(hot_store=mock_store, poll_interval_seconds=999)
        await ingester.start()
        await ingester.start()  # second start should be no-op
        assert ingester._running is True
        await ingester.stop()

    @pytest.mark.asyncio
    async def test_stop_when_not_running(self):
        from akosha.ingestion.code_graph_ingester import CodeGraphIngester

        mock_store = MagicMock()
        ingester = CodeGraphIngester(hot_store=mock_store)
        await ingester.stop()  # should not raise
        assert ingester._running is False

    @pytest.mark.asyncio
    async def test_stop_cancels_poll_task(self):
        from akosha.ingestion.code_graph_ingester import CodeGraphIngester

        mock_store = MagicMock()
        ingester = CodeGraphIngester(hot_store=mock_store, poll_interval_seconds=999)
        await ingester.start()
        task = ingester._poll_task
        await ingester.stop()
        assert ingester._running is False
        assert task.cancelled() or task.done()


class TestGetIngestionStatus:
    """Test get_ingestion_status method."""

    @pytest.mark.asyncio
    async def test_status_when_not_running(self):
        from akosha.ingestion.code_graph_ingester import CodeGraphIngester

        mock_store = MagicMock()
        ingester = CodeGraphIngester(hot_store=mock_store)
        status = await ingester.get_ingestion_status()
        assert status["running"] is False
        assert status["known_graphs"] == 0
        assert status["session_buddy_endpoint"] == "http://localhost:8678/mcp"
        assert status["poll_interval_seconds"] == 60
        assert "timestamp" in status

    @pytest.mark.asyncio
    async def test_status_when_running(self):
        from akosha.ingestion.code_graph_ingester import CodeGraphIngester

        mock_store = MagicMock()
        ingester = CodeGraphIngester(hot_store=mock_store, poll_interval_seconds=999)
        ingester._known_graph_ids = {"graph1", "graph2"}
        ingester._running = True
        status = await ingester.get_ingestion_status()
        assert status["running"] is True
        assert status["known_graphs"] == 2


class TestDiscoverCodeGraphs:
    """Test _discover_code_graphs method."""

    @pytest.mark.asyncio
    async def test_no_http_client(self):
        from akosha.ingestion.code_graph_ingester import CodeGraphIngester

        mock_store = MagicMock()
        ingester = CodeGraphIngester(hot_store=mock_store)
        ingester._http_client = None
        result = await ingester._discover_code_graphs()
        assert result == []

    @pytest.mark.asyncio
    async def test_successful_discovery(self):
        from akosha.ingestion.code_graph_ingester import CodeGraphIngester

        mock_store = MagicMock()
        ingester = CodeGraphIngester(hot_store=mock_store)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "success",
            "code_graphs": [
                {"id": "g1", "repo_path": "/repo1", "commit_hash": "abc"},
                {"id": "g2", "repo_path": "/repo2", "commit_hash": "def"},
            ],
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        ingester._http_client = mock_client

        result = await ingester._discover_code_graphs()
        assert len(result) == 2
        assert result[0]["id"] == "g1"
        assert "g1" in ingester._known_graph_ids
        assert "g2" in ingester._known_graph_ids

    @pytest.mark.asyncio
    async def test_filters_known_graphs(self):
        from akosha.ingestion.code_graph_ingester import CodeGraphIngester

        mock_store = MagicMock()
        ingester = CodeGraphIngester(hot_store=mock_store)
        ingester._known_graph_ids = {"g1"}

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "success",
            "code_graphs": [
                {"id": "g1", "repo_path": "/repo1", "commit_hash": "abc"},
                {"id": "g2", "repo_path": "/repo2", "commit_hash": "def"},
            ],
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        ingester._http_client = mock_client

        result = await ingester._discover_code_graphs()
        assert len(result) == 1
        assert result[0]["id"] == "g2"

    @pytest.mark.asyncio
    async def test_filters_graphs_without_id(self):
        from akosha.ingestion.code_graph_ingester import CodeGraphIngester

        mock_store = MagicMock()
        ingester = CodeGraphIngester(hot_store=mock_store)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "success",
            "code_graphs": [
                {"repo_path": "/repo1", "commit_hash": "abc"},
                {"id": "g1", "repo_path": "/repo2", "commit_hash": "def"},
            ],
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        ingester._http_client = mock_client

        result = await ingester._discover_code_graphs()
        assert len(result) == 1
        assert result[0]["id"] == "g1"

    @pytest.mark.asyncio
    async def test_non_success_status(self):
        from akosha.ingestion.code_graph_ingester import CodeGraphIngester

        mock_store = MagicMock()
        ingester = CodeGraphIngester(hot_store=mock_store)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "error",
            "message": "not found",
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        ingester._http_client = mock_client

        result = await ingester._discover_code_graphs()
        assert result == []

    @pytest.mark.asyncio
    async def test_http_error(self):
        import httpx

        from akosha.ingestion.code_graph_ingester import CodeGraphIngester

        mock_store = MagicMock()
        ingester = CodeGraphIngester(hot_store=mock_store)

        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.HTTPError("connection failed")
        ingester._http_client = mock_client

        result = await ingester._discover_code_graphs()
        assert result == []

    @pytest.mark.asyncio
    async def test_json_decode_error(self):
        from akosha.ingestion.code_graph_ingester import CodeGraphIngester

        mock_store = MagicMock()
        ingester = CodeGraphIngester(hot_store=mock_store)

        mock_response = MagicMock()
        mock_response.json.side_effect = ValueError("bad json")
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        ingester._http_client = mock_client

        result = await ingester._discover_code_graphs()
        assert result == []


class TestIngestCodeGraph:
    """Test _ingest_code_graph method."""

    @pytest.mark.asyncio
    async def test_no_http_client(self):
        from akosha.ingestion.code_graph_ingester import CodeGraphIngester

        mock_store = MagicMock()
        ingester = CodeGraphIngester(hot_store=mock_store)
        ingester._http_client = None
        result = await ingester._ingest_code_graph({"repo_path": "/r", "commit_hash": "abc"})
        assert result is False

    @pytest.mark.asyncio
    async def test_successful_ingestion(self):
        from akosha.ingestion.code_graph_ingester import CodeGraphIngester

        mock_store = MagicMock()
        mock_store.store_code_graph = AsyncMock()
        ingester = CodeGraphIngester(hot_store=mock_store)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "success",
            "repo_path": "/repo",
            "commit_hash": "abc123",
            "indexed_at": "2026-01-01T00:00:00Z",
            "nodes_count": 42,
            "graph_data": {"nodes": {}, "edges": []},
            "metadata": {},
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        ingester._http_client = mock_client

        result = await ingester._ingest_code_graph(
            {
                "repo_path": "/repo",
                "commit_hash": "abc123",
            }
        )
        assert result is True
        mock_store.store_code_graph.assert_called_once()
        call_kwargs = mock_store.store_code_graph.call_args
        assert call_kwargs.kwargs["repo_path"] == "/repo"
        assert call_kwargs.kwargs["commit_hash"] == "abc123"
        assert call_kwargs.kwargs["nodes_count"] == 42

    @pytest.mark.asyncio
    async def test_non_success_status(self):
        from akosha.ingestion.code_graph_ingester import CodeGraphIngester

        mock_store = MagicMock()
        ingester = CodeGraphIngester(hot_store=mock_store)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "not_found",
            "message": "graph not found",
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        ingester._http_client = mock_client

        result = await ingester._ingest_code_graph(
            {
                "repo_path": "/repo",
                "commit_hash": "abc",
            }
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_http_error(self):
        import httpx

        from akosha.ingestion.code_graph_ingester import CodeGraphIngester

        mock_store = MagicMock()
        ingester = CodeGraphIngester(hot_store=mock_store)

        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.HTTPError("timeout")
        ingester._http_client = mock_client

        result = await ingester._ingest_code_graph(
            {
                "repo_path": "/repo",
                "commit_hash": "abc",
            }
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_generic_exception(self):
        from akosha.ingestion.code_graph_ingester import CodeGraphIngester

        mock_store = MagicMock()
        ingester = CodeGraphIngester(hot_store=mock_store)

        mock_client = AsyncMock()
        mock_client.post.side_effect = RuntimeError("unexpected")
        ingester._http_client = mock_client

        result = await ingester._ingest_code_graph(
            {
                "repo_path": "/repo",
                "commit_hash": "abc",
            }
        )
        assert result is False
