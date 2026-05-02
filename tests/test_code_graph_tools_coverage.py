"""Tests for akosha/mcp/tools/code_graph_tools.py — code graph analysis tools."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from akosha.mcp.tools.code_graph_tools import (
    _compute_graph_similarity,
    register_code_graph_analysis_tools,
)
from akosha.mcp.tools.tool_registry import FastMCPToolRegistry


def _make_registry():
    """Create a real FastMCPToolRegistry with a mock app.

    code_graph_tools.py registers via @mcp.tool() directly (not
    registry.register), so we capture tools from app.tool() calls.
    """
    app = MagicMock()
    captured = []

    def tool_decorator(*args, **kwargs):
        def deco(func):
            captured.append(func)
            return func

        return deco

    app.tool = tool_decorator
    registry = FastMCPToolRegistry(app)
    registry.app = registry._app
    return registry, captured


class TestComputeGraphSimilarity:
    @pytest.mark.asyncio
    async def test_identical_graphs(self):
        graph = {"nodes": {"a": {"type": "function"}, "b": {"type": "class"}}}
        result = await _compute_graph_similarity(graph, graph)
        assert result == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_different_graphs(self):
        g1 = {"nodes": {"a": {"type": "function"}}}
        g2 = {"nodes": {"b": {"type": "class"}}}
        result = await _compute_graph_similarity(g1, g2)
        assert result == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_partial_overlap(self):
        g1 = {
            "nodes": {"a": {"type": "function"}, "b": {"type": "function"}, "c": {"type": "class"}}
        }
        g2 = {"nodes": {"a": {"type": "function"}, "c": {"type": "class"}}}
        result = await _compute_graph_similarity(g1, g2)
        assert 0.0 < result < 1.0

    @pytest.mark.asyncio
    async def test_empty_nodes(self):
        result = await _compute_graph_similarity({"nodes": {}}, {"nodes": {"a": {"type": "f"}}})
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_both_empty(self):
        result = await _compute_graph_similarity({"nodes": {}}, {"nodes": {}})
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_non_dict_nodes_ignored(self):
        g1 = {"nodes": {"a": "not a dict"}}
        g2 = {"nodes": {"b": "not a dict"}}
        result = await _compute_graph_similarity(g1, g2)
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_missing_nodes_key(self):
        g1 = {}
        g2 = {"nodes": {"a": {"type": "f"}}}
        result = await _compute_graph_similarity(g1, g2)
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_mixed_valid_and_invalid_nodes(self):
        g1 = {"nodes": {"a": {"type": "function"}, "b": "string"}}
        g2 = {"nodes": {"a": {"type": "function"}, "c": 42}}
        result = await _compute_graph_similarity(g1, g2)
        assert 0.0 < result <= 1.0

    @pytest.mark.asyncio
    async def test_exception_returns_zero(self):
        result = await _compute_graph_similarity(None, None)
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_same_type_distribution(self):
        g1 = {
            "nodes": {"a": {"type": "function"}, "b": {"type": "function"}, "c": {"type": "class"}}
        }
        g2 = {
            "nodes": {"x": {"type": "function"}, "y": {"type": "function"}, "z": {"type": "class"}}
        }
        result = await _compute_graph_similarity(g1, g2)
        assert result == pytest.approx(1.0)


class TestListIngestedCodeGraphs:
    @pytest.mark.asyncio
    async def test_success(self):
        registry, captured = _make_registry()
        hot_store = AsyncMock()
        hot_store.list_code_graphs = AsyncMock(
            return_value=[{"repo_path": "/repo1", "commit_hash": "abc", "nodes_count": 10}]
        )

        register_code_graph_analysis_tools(registry, hot_store)
        tool = captured[0]

        result = await tool(repo_path=None, limit=100)
        assert result["status"] == "success"
        assert result["count"] == 1
        assert len(result["code_graphs"]) == 1

    @pytest.mark.asyncio
    async def test_with_repo_filter(self):
        registry, captured = _make_registry()
        hot_store = AsyncMock()
        hot_store.list_code_graphs = AsyncMock(
            return_value=[{"repo_path": "/repo1", "commit_hash": "abc", "nodes_count": 5}]
        )

        register_code_graph_analysis_tools(registry, hot_store)
        tool = captured[0]

        result = await tool(repo_path="/repo1", limit=10)
        assert result["status"] == "success"
        hot_store.list_code_graphs.assert_called_once_with(repo_path="/repo1", limit=10)

    @pytest.mark.asyncio
    async def test_empty(self):
        registry, captured = _make_registry()
        hot_store = AsyncMock()
        hot_store.list_code_graphs = AsyncMock(return_value=[])

        register_code_graph_analysis_tools(registry, hot_store)
        tool = captured[0]

        result = await tool()
        assert result["status"] == "success"
        assert result["count"] == 0
        assert result["code_graphs"] == []

    @pytest.mark.asyncio
    async def test_exception(self):
        registry, captured = _make_registry()
        hot_store = AsyncMock()
        hot_store.list_code_graphs = AsyncMock(side_effect=RuntimeError("db error"))

        register_code_graph_analysis_tools(registry, hot_store)
        tool = captured[0]

        result = await tool()
        assert result["status"] == "error"
        assert "db error" in result["message"]
        assert result["count"] == 0
        assert result["code_graphs"] == []


class TestGetCodeGraphDetails:
    @pytest.mark.asyncio
    async def test_found(self):
        registry, captured = _make_registry()
        hot_store = AsyncMock()
        hot_store.get_code_graph = AsyncMock(
            return_value={
                "repo_path": "/repo1",
                "commit_hash": "abc123",
                "nodes_count": 10,
                "graph_data": {"nodes": {}},
                "metadata": {},
            }
        )

        register_code_graph_analysis_tools(registry, hot_store)
        tool = captured[1]

        result = await tool(repo_path="/repo1", commit_hash="abc123")
        assert result["status"] == "success"
        assert result["repo_path"] == "/repo1"
        assert result["nodes_count"] == 10

    @pytest.mark.asyncio
    async def test_not_found(self):
        registry, captured = _make_registry()
        hot_store = AsyncMock()
        hot_store.get_code_graph = AsyncMock(return_value=None)

        register_code_graph_analysis_tools(registry, hot_store)
        tool = captured[1]

        result = await tool(repo_path="/repo1", commit_hash="abc123")
        assert result["status"] == "not_found"
        assert "abc123" in result["message"]

    @pytest.mark.asyncio
    async def test_exception(self):
        registry, captured = _make_registry()
        hot_store = AsyncMock()
        hot_store.get_code_graph = AsyncMock(side_effect=RuntimeError("err"))

        register_code_graph_analysis_tools(registry, hot_store)
        tool = captured[1]

        result = await tool(repo_path="/repo1", commit_hash="abc")
        assert result["status"] == "error"
        assert "err" in result["message"]


class TestFindSimilarRepositories:
    @pytest.mark.asyncio
    async def test_success(self):
        registry, captured = _make_registry()
        hot_store = AsyncMock()

        graphs_list = [
            {"repo_path": "/repo1", "commit_hash": "abc", "nodes_count": 10},
            {"repo_path": "/repo2", "commit_hash": "def", "nodes_count": 15},
        ]
        hot_store.list_code_graphs = AsyncMock(return_value=graphs_list)
        hot_store.get_code_graph = AsyncMock(
            side_effect=[
                {
                    "repo_path": "/repo1",
                    "nodes_count": 10,
                    "graph_data": {"nodes": {"a": {"type": "function"}}},
                },
                {
                    "repo_path": "/repo2",
                    "nodes_count": 15,
                    "graph_data": {"nodes": {"a": {"type": "function"}}},
                },
            ]
        )

        register_code_graph_analysis_tools(registry, hot_store)
        tool = captured[2]

        result = await tool(repo_path="/repo1", min_similarity=0.3, limit=10)
        assert result["status"] == "success"
        assert result["reference_repo"] == "/repo1"
        assert result["reference_nodes"] == 10

    @pytest.mark.asyncio
    async def test_reference_not_found(self):
        registry, captured = _make_registry()
        hot_store = AsyncMock()
        hot_store.list_code_graphs = AsyncMock(
            return_value=[{"repo_path": "/repo2", "commit_hash": "def", "nodes_count": 5}]
        )
        hot_store.get_code_graph = AsyncMock(return_value=None)

        register_code_graph_analysis_tools(registry, hot_store)
        tool = captured[2]

        result = await tool(repo_path="/repo1")
        assert result["status"] == "not_found"
        assert result["repositories"] == []

    @pytest.mark.asyncio
    async def test_below_threshold(self):
        registry, captured = _make_registry()
        hot_store = AsyncMock()

        graphs_list = [
            {"repo_path": "/repo1", "commit_hash": "abc", "nodes_count": 10},
            {"repo_path": "/repo2", "commit_hash": "def", "nodes_count": 5},
        ]
        hot_store.list_code_graphs = AsyncMock(return_value=graphs_list)

        def get_graph_side_effect(repo_path, commit_hash):
            if repo_path == "/repo1":
                return {
                    "repo_path": "/repo1",
                    "nodes_count": 10,
                    "graph_data": {"nodes": {"a": {"type": "function"}}},
                }
            return {
                "repo_path": "/repo2",
                "nodes_count": 5,
                "graph_data": {"nodes": {"b": {"type": "class"}}},
            }

        hot_store.get_code_graph = AsyncMock(side_effect=get_graph_side_effect)

        register_code_graph_analysis_tools(registry, hot_store)
        tool = captured[2]

        result = await tool(repo_path="/repo1", min_similarity=0.99)
        assert result["status"] == "success"
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_exception(self):
        registry, captured = _make_registry()
        hot_store = AsyncMock()
        hot_store.list_code_graphs = AsyncMock(side_effect=RuntimeError("fail"))

        register_code_graph_analysis_tools(registry, hot_store)
        tool = captured[2]

        result = await tool(repo_path="/repo1")
        assert result["status"] == "error"
        assert result["repositories"] == []

    @pytest.mark.asyncio
    async def test_comparison_exception_skipped(self):
        registry, captured = _make_registry()
        hot_store = AsyncMock()

        graphs_list = [
            {"repo_path": "/repo1", "commit_hash": "abc", "nodes_count": 10},
            {"repo_path": "/repo2", "commit_hash": "def", "nodes_count": 5},
        ]
        hot_store.list_code_graphs = AsyncMock(return_value=graphs_list)

        def get_graph_side_effect(repo_path, commit_hash):
            if repo_path == "/repo1":
                return {
                    "repo_path": "/repo1",
                    "nodes_count": 10,
                    "graph_data": {"nodes": {"a": {"type": "function"}}},
                }
            raise RuntimeError("comparison error")

        hot_store.get_code_graph = AsyncMock(side_effect=get_graph_side_effect)

        register_code_graph_analysis_tools(registry, hot_store)
        tool = captured[2]

        result = await tool(repo_path="/repo1", min_similarity=0.0)
        assert result["status"] == "success"
        assert result["count"] == 0


class TestGetCrossRepoFunctionUsage:
    @pytest.mark.asyncio
    async def test_found(self):
        registry, captured = _make_registry()
        hot_store = AsyncMock()

        graphs_list = [
            {"repo_path": "/repo1", "commit_hash": "abc"},
            {"repo_path": "/repo2", "commit_hash": "def"},
        ]
        hot_store.list_code_graphs = AsyncMock(return_value=graphs_list)

        graph_data_1 = {
            "nodes": {
                "n1": {
                    "name": "my_function",
                    "type": "function",
                    "file_path": "a.py",
                    "start_line": 10,
                },
                "n2": {"name": "other", "type": "class", "file_path": "b.py", "start_line": 5},
            }
        }
        graph_data_2 = {
            "nodes": {
                "n3": {"name": "my_function", "type": "function", "file": "c.py", "start_line": 20},
            }
        }

        hot_store.get_code_graph = AsyncMock(
            side_effect=[
                {"repo_path": "/repo1", "graph_data": graph_data_1},
                {"repo_path": "/repo2", "graph_data": graph_data_2},
            ]
        )

        register_code_graph_analysis_tools(registry, hot_store)
        tool = captured[3]

        result = await tool(function_name="my_function", limit=20)
        assert result["status"] == "success"
        assert result["function_name"] == "my_function"
        assert result["count"] == 2

    @pytest.mark.asyncio
    async def test_not_found(self):
        registry, captured = _make_registry()
        hot_store = AsyncMock()
        hot_store.list_code_graphs = AsyncMock(
            return_value=[{"repo_path": "/repo1", "commit_hash": "abc"}]
        )
        hot_store.get_code_graph = AsyncMock(
            return_value={
                "repo_path": "/repo1",
                "graph_data": {"nodes": {"n1": {"name": "other_func", "type": "function"}}},
            }
        )

        register_code_graph_analysis_tools(registry, hot_store)
        tool = captured[3]

        result = await tool(function_name="my_function")
        assert result["status"] == "success"
        assert result["count"] == 0
        assert result["repositories"] == []

    @pytest.mark.asyncio
    async def test_graph_not_found(self):
        registry, captured = _make_registry()
        hot_store = AsyncMock()
        hot_store.list_code_graphs = AsyncMock(
            return_value=[{"repo_path": "/repo1", "commit_hash": "abc"}]
        )
        hot_store.get_code_graph = AsyncMock(return_value=None)

        register_code_graph_analysis_tools(registry, hot_store)
        tool = captured[3]

        result = await tool(function_name="my_func")
        assert result["status"] == "success"
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_exception(self):
        registry, captured = _make_registry()
        hot_store = AsyncMock()
        hot_store.list_code_graphs = AsyncMock(side_effect=RuntimeError("fail"))

        register_code_graph_analysis_tools(registry, hot_store)
        tool = captured[3]

        result = await tool(function_name="my_func")
        assert result["status"] == "error"
        assert result["repositories"] == []

    @pytest.mark.asyncio
    async def test_case_insensitive_search(self):
        registry, captured = _make_registry()
        hot_store = AsyncMock()
        hot_store.list_code_graphs = AsyncMock(
            return_value=[{"repo_path": "/repo1", "commit_hash": "abc"}]
        )
        hot_store.get_code_graph = AsyncMock(
            return_value={
                "repo_path": "/repo1",
                "graph_data": {
                    "nodes": {
                        "n1": {
                            "name": "MY_FUNCTION",
                            "type": "function",
                            "file_path": "a.py",
                            "start_line": 1,
                        }
                    }
                },
            }
        )

        register_code_graph_analysis_tools(registry, hot_store)
        tool = captured[3]

        result = await tool(function_name="my_function")
        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_node_without_file_path_skipped(self):
        registry, captured = _make_registry()
        hot_store = AsyncMock()
        hot_store.list_code_graphs = AsyncMock(
            return_value=[{"repo_path": "/repo1", "commit_hash": "abc"}]
        )
        hot_store.get_code_graph = AsyncMock(
            return_value={
                "repo_path": "/repo1",
                "graph_data": {
                    "nodes": {"n1": {"name": "my_func", "type": "function", "start_line": 1}}
                },
            }
        )

        register_code_graph_analysis_tools(registry, hot_store)
        tool = captured[3]

        result = await tool(function_name="my_func")
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_limit_applied(self):
        registry, captured = _make_registry()
        hot_store = AsyncMock()

        graphs_list = [{"repo_path": f"/repo{i}", "commit_hash": f"h{i}"} for i in range(5)]
        hot_store.list_code_graphs = AsyncMock(return_value=graphs_list)

        def get_graph_side_effect(repo_path, commit_hash):
            return {
                "repo_path": repo_path,
                "graph_data": {
                    "nodes": {
                        "n1": {
                            "name": "my_func",
                            "type": "function",
                            "file_path": "a.py",
                            "start_line": 1,
                        }
                    }
                },
            }

        hot_store.get_code_graph = AsyncMock(side_effect=get_graph_side_effect)

        register_code_graph_analysis_tools(registry, hot_store)
        tool = captured[3]

        result = await tool(function_name="my_func", limit=2)
        assert len(result["repositories"]) == 2


class TestRegisterCodeGraphAnalysisTools:
    def test_invalid_registry(self):
        hot_store = MagicMock()
        register_code_graph_analysis_tools("not a registry", hot_store)

    def test_valid_registry(self):
        registry, captured = _make_registry()
        hot_store = MagicMock()
        register_code_graph_analysis_tools(registry, hot_store)

        assert len(captured) == 4
