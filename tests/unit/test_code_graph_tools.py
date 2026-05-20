"""Tests for akosha.mcp.tools.code_graph_tools — code graph analysis tools and similarity."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestComputeGraphSimilarity:
    """Test _compute_graph_similarity async helper function."""

    @pytest.mark.asyncio
    async def test_identical_graphs(self):
        from akosha.mcp.tools.code_graph_tools import _compute_graph_similarity

        graph = {
            "nodes": {
                "n1": {"type": "function", "name": "foo"},
                "n2": {"type": "class", "name": "Bar"},
            },
            "edges": [],
        }
        result = await _compute_graph_similarity(graph, graph)
        assert result == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_different_graphs(self):
        from akosha.mcp.tools.code_graph_tools import _compute_graph_similarity

        g1 = {"nodes": {"n1": {"type": "function"}, "n2": {"type": "function"}}, "edges": []}
        g2 = {"nodes": {"n1": {"type": "class"}, "n2": {"type": "class"}}, "edges": []}
        result = await _compute_graph_similarity(g1, g2)
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_partially_similar(self):
        from akosha.mcp.tools.code_graph_tools import _compute_graph_similarity

        # g1: 2 functions, 1 class; g2: 2 classes, 1 function
        g1 = {
            "nodes": {
                "n1": {"type": "function"},
                "n2": {"type": "class"},
                "n3": {"type": "function"},
            }
        }
        g2 = {
            "nodes": {"n1": {"type": "function"}, "n2": {"type": "class"}, "n3": {"type": "class"}}
        }
        result = await _compute_graph_similarity(g1, g2)
        assert 0.0 < result < 1.0

    @pytest.mark.asyncio
    async def test_empty_nodes_one_side(self):
        from akosha.mcp.tools.code_graph_tools import _compute_graph_similarity

        g1 = {"nodes": {}}
        g2 = {"nodes": {"n1": {"type": "function"}}}
        result = await _compute_graph_similarity(g1, g2)
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_both_empty(self):
        from akosha.mcp.tools.code_graph_tools import _compute_graph_similarity

        g = {"nodes": {}}
        result = await _compute_graph_similarity(g, g)
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_no_nodes_key(self):
        from akosha.mcp.tools.code_graph_tools import _compute_graph_similarity

        g = {"edges": []}
        result = await _compute_graph_similarity(g, g)
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_non_dict_nodes(self):
        from akosha.mcp.tools.code_graph_tools import _compute_graph_similarity

        g = {"nodes": {"n1": "not_a_dict"}}
        result = await _compute_graph_similarity(g, g)
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_exception_returns_zero(self):
        from akosha.mcp.tools.code_graph_tools import _compute_graph_similarity

        result = await _compute_graph_similarity(None, {"nodes": {}})
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_same_type_distribution(self):
        from akosha.mcp.tools.code_graph_tools import _compute_graph_similarity

        g1 = {"nodes": {"a": {"type": "func"}, "b": {"type": "func"}, "c": {"type": "func"}}}
        g2 = {"nodes": {"x": {"type": "func"}, "y": {"type": "func"}, "z": {"type": "func"}}}
        result = await _compute_graph_similarity(g1, g2)
        assert result == 1.0

    @pytest.mark.asyncio
    async def test_unknown_type_treated_uniformly(self):
        from akosha.mcp.tools.code_graph_tools import _compute_graph_similarity

        # Nodes without "type" key default to "unknown"
        g1 = {"nodes": {"a": {"name": "foo"}}}
        g2 = {"nodes": {"b": {"name": "bar"}}}
        result = await _compute_graph_similarity(g1, g2)
        assert result == 1.0


class TestRegisterCodeGraphAnalysisTools:
    """Test register_code_graph_analysis_tools function."""

    def test_invalid_registry(self):
        from akosha.mcp.tools.code_graph_tools import register_code_graph_analysis_tools

        register_code_graph_analysis_tools("not a registry", None)

    def test_tools_registered(self):
        from akosha.mcp.tools.code_graph_tools import register_code_graph_analysis_tools

        mock_registry = MagicMock()
        mock_mcp = MagicMock()

        with patch("akosha.mcp.tools.tool_registry.FastMCPToolRegistry", type(mock_registry)):
            mock_registry.app = mock_mcp
            register_code_graph_analysis_tools(mock_registry, MagicMock())
            assert mock_mcp.tool.call_count >= 3

    def test_tool_names(self):
        from akosha.mcp.tools.code_graph_tools import register_code_graph_analysis_tools

        registered = []

        def mock_tool(fn):
            registered.append(fn.__name__)
            return fn

        mock_registry = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.tool.return_value = mock_tool

        with patch("akosha.mcp.tools.tool_registry.FastMCPToolRegistry", type(mock_registry)):
            mock_registry.app = mock_mcp
            register_code_graph_analysis_tools(mock_registry, MagicMock())
            assert "list_ingested_code_graphs" in registered
            assert "get_code_graph_details" in registered
            assert "find_similar_repositories" in registered


class TestCodeGraphToolsRuntime:
    @pytest.mark.asyncio
    async def test_tool_runtime_branches(self):
        from akosha.mcp.tools.code_graph_tools import register_code_graph_analysis_tools
        from akosha.mcp.tools.tool_registry import FastMCPToolRegistry

        class FakeApp:
            def __init__(self) -> None:
                self.tools: dict[str, object] = {}

            def tool(self, *args, **kwargs):
                def decorator(func):
                    self.tools[func.__name__] = func
                    return func

                return decorator

        app = FakeApp()
        registry = FastMCPToolRegistry(app)  # type: ignore[arg-type]
        registry.app = registry._app

        hot_store = MagicMock()
        hot_store.list_code_graphs = AsyncMock(
            return_value=[
                {"repo_path": "/repo1", "commit_hash": "abc", "nodes_count": 3},
                {"repo_path": "/repo2", "commit_hash": "def", "nodes_count": 2},
                {"repo_path": "/repo3", "commit_hash": "ghi", "nodes_count": 1},
            ]
        )

        async def get_code_graph(repo_path: str, commit_hash: str):
            if repo_path == "/repo1" and commit_hash == "deadbeef":
                return None
            if repo_path == "/repo1" and commit_hash == "abc":
                return {
                    "repo_path": "/repo1",
                    "commit_hash": "abc",
                    "nodes_count": 3,
                    "graph_data": {
                        "nodes": {
                            "n1": {
                                "type": "function",
                                "name": "my_func",
                                "file_path": "a.py",
                                "start_line": 10,
                            },
                            "n2": {"type": "class", "name": "Other"},
                        }
                    },
                }
            if repo_path == "/repo2" and commit_hash == "def":
                return {
                    "repo_path": "/repo2",
                    "commit_hash": "def",
                    "nodes_count": 2,
                    "graph_data": {
                        "nodes": {
                            "n1": {"type": "function", "name": "other_func", "file_path": "b.py"},
                            "n2": {"type": "class", "name": "Baz"},
                        }
                    },
                }
            if repo_path == "/repo3" and commit_hash == "ghi":
                return None
            return None

        hot_store.get_code_graph = AsyncMock(side_effect=get_code_graph)

        register_code_graph_analysis_tools(registry, hot_store)

        list_code_graphs = app.tools["list_ingested_code_graphs"]
        details = app.tools["get_code_graph_details"]
        similar = app.tools["find_similar_repositories"]
        usage = app.tools["get_cross_repo_function_usage"]

        listed = await list_code_graphs()
        assert listed["status"] == "success"
        assert listed["count"] == 3

        missing = await details(repo_path="/repo1", commit_hash="deadbeef")
        assert missing["status"] == "not_found"

        found = await details(repo_path="/repo1", commit_hash="abc")
        assert found["status"] == "success"
        assert found["repo_path"] == "/repo1"

        similar_result = await similar(repo_path="/repo1", min_similarity=0.5, limit=5)
        assert similar_result["status"] == "success"
        assert similar_result["count"] == 1
        assert similar_result["repositories"][0]["repo_path"] == "/repo2"

        usage_result = await usage(function_name="my_func", limit=5)
        assert usage_result["status"] == "success"
        assert usage_result["count"] == 1
        assert usage_result["repositories"][0]["repo_path"] == "/repo1"
