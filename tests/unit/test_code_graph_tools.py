"""Tests for akosha.mcp.tools.code_graph_tools — code graph analysis tools and similarity."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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
