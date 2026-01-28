"""Tests for Akasha MCP server integration."""

from __future__ import annotations

import pytest

from akasha.processing.embeddings import get_embedding_service
from akasha.processing.analytics import TimeSeriesAnalytics
from akasha.processing.knowledge_graph import KnowledgeGraphBuilder


class TestMCPIntegration:
    """Test suite for MCP server integration."""

    @pytest.mark.asyncio
    async def test_mcp_server_initialization(self) -> None:
        """Test that MCP server initializes with Phase 2 services."""
        from akasha_mcp.main import create_app

        app = create_app()

        assert app is not None
        assert app._mcp_server is not None

    @pytest.mark.asyncio
    async def test_embedding_service_initialization(self) -> None:
        """Test that embedding service initializes correctly."""
        service = get_embedding_service()
        await service.initialize()

        assert service._initialized

    @pytest.mark.asyncio
    async def test_analytics_service_initialization(self) -> None:
        """Test that analytics service initializes correctly."""
        analytics = TimeSeriesAnalytics()

        assert analytics is not None
        assert analytics.get_metric_names() == []

    @pytest.mark.asyncio
    async def test_knowledge_graph_initialization(self) -> None:
        """Test that knowledge graph builder initializes correctly."""
        graph = KnowledgeGraphBuilder()

        assert graph is not None
        assert graph.entities == {}
        assert graph.edges == []

    @pytest.mark.asyncio
    async def test_tool_registration(self) -> None:
        """Test that MCP tools are registered correctly."""
        from akasha_mcp.main import create_app
        from akasha_mcp.tools.tool_registry import FastMCPToolRegistry

        app = create_app()
        registry = FastMCPToolRegistry(app)

        # Register tools
        embedding_service = get_embedding_service()
        await embedding_service.initialize()

        analytics_service = TimeSeriesAnalytics()
        graph_builder = KnowledgeGraphBuilder()

        from akasha_mcp.tools.akasha_tools import register_akasha_tools

        register_akasha_tools(
            registry,
            embedding_service=embedding_service,
            analytics_service=analytics_service,
            graph_builder=graph_builder,
        )

        # Check tools were registered
        tools = registry.tools
        assert len(tools) >= 9  # At least 9 tools should be registered

        # Check for specific tools
        tool_names = list(tools.keys())
        assert "generate_embedding" in tool_names
        assert "search_all_systems" in tool_names
        assert "analyze_trends" in tool_names
        assert "detect_anomalies" in tool_names
        assert "correlate_systems" in tool_names
        assert "query_knowledge_graph" in tool_names
        assert "find_path" in tool_names
        assert "get_graph_statistics" in tool_names

    @pytest.mark.asyncio
    async def test_generate_embedding_tool(self) -> None:
        """Test generate_embedding tool."""
        from akasha_mcp.tools.akasha_tools import register_embedding_tools
        from akasha_mcp.tools.tool_registry import FastMCPToolRegistry
        from akasha_mcp.main import create_app

        app = create_app()
        registry = FastMCPToolRegistry(app)

        embedding_service = get_embedding_service()
        await embedding_service.initialize()

        register_embedding_tools(registry, embedding_service)

        # Get the tool
        tools = registry.tools
        assert "generate_embedding" in tools

        # Call the tool coroutine
        result = await tools["generate_embedding"].coroutine(
            text="test conversation about Python"
        )

        assert result["text"] == "test conversation about Python"
        assert result["embedding_dim"] == 384
        assert len(result["embedding"]) == 384
        assert result["mode"] in ["real", "fallback"]

    @pytest.mark.asyncio
    async def test_analytics_tools(self) -> None:
        """Test analytics tools with sample data."""
        from akasha_mcp.tools.akasha_tools import register_analytics_tools
        from akasha_mcp.tools.tool_registry import FastMCPToolRegistry
        from akasha_mcp.main import create_app
        from datetime import UTC, datetime, timedelta

        app = create_app()
        registry = FastMCPToolRegistry(app)

        analytics_service = TimeSeriesAnalytics()

        # Add sample data
        now = datetime.now(UTC)
        for i in range(20):
            await analytics_service.add_metric(
                metric_name="test_metric",
                value=10.0 + i,
                system_id="test-system",
                timestamp=now + timedelta(hours=i),
            )

        register_analytics_tools(registry, analytics_service)

        # Test analyze_trends tool
        tools = registry.tools
        assert "analyze_trends" in tools

        result = await tools["analyze_trends"].coroutine(
            metric_name="test_metric",
            system_id="test-system",
            time_window_days=1,
        )

        assert result["metric_name"] == "test_metric"
        assert result["trend_direction"] == "increasing"
        assert result["trend_strength"] > 0.5

    @pytest.mark.asyncio
    async def test_graph_tools(self) -> None:
        """Test knowledge graph tools."""
        from akasha_mcp.tools.akasha_tools import register_graph_tools
        from akasha_mcp.tools.tool_registry import FastMCPToolRegistry
        from akasha_mcp.main import create_app

        app = create_app()
        registry = FastMCPToolRegistry(app)

        graph_builder = KnowledgeGraphBuilder()

        register_graph_tools(registry, graph_builder)

        # Test get_graph_statistics tool
        tools = registry.tools
        assert "get_graph_statistics" in tools

        result = await tools["get_graph_statistics"].coroutine()

        assert result["total_entities"] == 0
        assert result["total_edges"] == 0
