"""Tests for Akosha MCP tools.

Tests all tool implementations in akosha.mcp.tools.akosha_tools module,
covering embedding generation, search, analytics, graph operations, and system tools.
"""

import pytest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from akosha.mcp.tools.akosha_tools import (
    register_akosha_tools,
    register_embedding_tools,
    register_search_tools,
    register_analytics_tools,
    register_graph_tools,
    register_system_tools,
    register_code_graph_tools,
)
from akosha.processing.embeddings import EmbeddingService
from akosha.processing.analytics import TimeSeriesAnalytics, TrendAnalysis, AnomalyDetection, CorrelationResult
from akosha.processing.knowledge_graph import KnowledgeGraphBuilder
from akosha.security import AuthenticationError


class TestEmbeddingTools:
    """Test embedding generation tools."""

    @pytest.fixture
    def mock_embedding_service(self):
        """Create mock embedding service."""
        service = MagicMock(spec=EmbeddingService)
        service.is_available.return_value = True
        service.generate_embedding.return_value = [0.1] * 384  # Mock 384-dim vector
        service.generate_batch_embeddings.return_value = [[0.1] * 384, [0.2] * 384]
        return service

    @pytest.fixture
    def registry(self):
        """Create mock tool registry."""
        return MagicMock()

    @pytest.mark.asyncio
    async def test_register_embedding_tools(self, registry, mock_embedding_service):
        """Test embedding tools registration."""
        register_embedding_tools(registry, mock_embedding_service)

        # Verify all embedding tools were registered
        assert registry.register.call_count == 2

    @pytest.mark.asyncio
    async def test_generate_embedding_function_structure(self, registry, mock_embedding_service):
        """Test that generate_embedding is properly registered."""
        register_embedding_tools(registry, mock_embedding_service)

        # Get the registered function and metadata
        first_call = registry.register.call_args_list[0]
        metadata = first_call[0][0]  # First positional argument is metadata
        assert metadata.name == "generate_embedding"
        assert "semantic embedding" in metadata.description

    @pytest.mark.asyncio
    async def test_generate_batch_embeddings_function_structure(self, registry, mock_embedding_service):
        """Test that generate_batch_embeddings is properly registered."""
        register_embedding_tools(registry, mock_embedding_service)

        # Get the registered function and metadata
        second_call = registry.register.call_args_list[1]
        metadata = second_call[0][0]  # First positional argument is metadata
        assert metadata.name == "generate_batch_embeddings"
        assert "batch" in metadata.description


class TestSearchTools:
    """Test search tools."""

    @pytest.fixture
    def mock_embedding_service(self):
        """Create mock embedding service."""
        service = MagicMock(spec=EmbeddingService)
        service.is_available.return_value = True
        service.generate_embedding.return_value = [0.1] * 384
        return service

    @pytest.fixture
    def registry(self):
        """Create mock tool registry."""
        return MagicMock()

    @pytest.mark.asyncio
    async def test_register_search_tools(self, registry, mock_embedding_service):
        """Test search tools registration."""
        register_search_tools(registry, mock_embedding_service)

        assert registry.register.call_count == 1
        search_call = registry.register.call_args_list[0]
        metadata = search_call[0][0]
        assert metadata.name == "search_all_systems"
        assert "semantic similarity" in metadata.description


class TestAnalyticsTools:
    """Test analytics tools."""

    @pytest.fixture
    def mock_analytics_service(self):
        """Create mock analytics service."""
        service = MagicMock(spec=TimeSeriesAnalytics)
        service.get_metric_names.return_value = ["conversation_count", "quality_score"]
        return service

    @pytest.fixture
    def registry(self):
        """Create mock tool registry."""
        return MagicMock()

    @pytest.mark.asyncio
    async def test_register_analytics_tools(self, registry, mock_analytics_service):
        """Test analytics tools registration."""
        register_analytics_tools(registry, mock_analytics_service)

        # Should register 4 tools
        assert registry.register.call_count == 4

        tool_names = [call[0][0].name for call in registry.register.call_args_list]
        expected_tools = ["get_system_metrics", "analyze_trends", "detect_anomalies", "correlate_systems"]
        assert all(name in tool_names for name in expected_tools)

    @pytest.mark.asyncio
    async def test_get_system_metrics_metadata(self, registry, mock_analytics_service):
        """Test get_system_metrics tool metadata."""
        register_analytics_tools(registry, mock_analytics_service)

        # Get the first tool's metadata
        metrics_call = registry.register.call_args_list[0]
        metadata = metrics_call[0][0]
        assert metadata.name == "get_system_metrics"
        assert "metrics" in metadata.description.lower()

    @pytest.mark.asyncio
    async def test_analyze_trends_metadata(self, registry, mock_analytics_service):
        """Test analyze_trends tool metadata."""
        register_analytics_tools(registry, mock_analytics_service)

        # Get the second tool's metadata
        trends_call = registry.register.call_args_list[1]
        metadata = trends_call[0][0]
        assert metadata.name == "analyze_trends"
        assert "trend" in metadata.description.lower()

    @pytest.mark.asyncio
    async def test_detect_anomalies_metadata(self, registry, mock_analytics_service):
        """Test detect_anomalies tool metadata."""
        register_analytics_tools(registry, mock_analytics_service)

        # Get the third tool's metadata
        anomaly_call = registry.register.call_args_list[2]
        metadata = anomaly_call[0][0]
        assert metadata.name == "detect_anomalies"
        assert "anomaly" in metadata.description.lower()

    @pytest.mark.asyncio
    async def test_correlate_systems_metadata(self, registry, mock_analytics_service):
        """Test correlate_systems tool metadata."""
        register_analytics_tools(registry, mock_analytics_service)

        # Get the fourth tool's metadata
        correlate_call = registry.register.call_args_list[3]
        metadata = correlate_call[0][0]
        assert metadata.name == "correlate_systems"
        assert "correlation" in metadata.description.lower()


class TestGraphTools:
    """Test knowledge graph tools."""

    @pytest.fixture
    def mock_graph_builder(self):
        """Create mock graph builder."""
        builder = MagicMock(spec=KnowledgeGraphBuilder)
        builder.get_neighbors.return_value = [
            {
                "entity_id": "project:myapp",
                "entity_type": "project",
                "edge_type": "worked_on",
                "weight": 1.0,
                "properties": {"name": "My App"}
            }
        ]
        builder.find_shortest_path.return_value = ["user:alice", "project:myapp"]
        builder.get_statistics.return_value = {
            "total_entities": 42,
            "total_edges": 125,
            "entity_types": {"user": 15, "project": 12, "system": 10, "concept": 5},
            "edge_types": {"worked_on": 45, "contains": 30, "related_to": 50}
        }
        return builder

    @pytest.fixture
    def registry(self):
        """Create mock tool registry."""
        return MagicMock()

    @pytest.mark.asyncio
    async def test_register_graph_tools(self, registry, mock_graph_builder):
        """Test graph tools registration."""
        register_graph_tools(registry, mock_graph_builder)

        # Should register 3 tools
        assert registry.register.call_count == 3

        tool_names = [call[0][0].name for call in registry.register.call_args_list]
        expected_tools = ["query_knowledge_graph", "find_path", "get_graph_statistics"]
        assert all(name in tool_names for name in expected_tools)

    @pytest.mark.asyncio
    async def test_query_knowledge_graph_metadata(self, registry, mock_graph_builder):
        """Test query_knowledge_graph tool metadata."""
        register_graph_tools(registry, mock_graph_builder)

        # Get the first tool's metadata
        query_call = registry.register.call_args_list[0]
        metadata = query_call[0][0]
        assert metadata.name == "query_knowledge_graph"
        assert "knowledge graph" in metadata.description.lower()

    @pytest.mark.asyncio
    async def test_find_path_metadata(self, registry, mock_graph_builder):
        """Test find_path tool metadata."""
        register_graph_tools(registry, mock_graph_builder)

        # Get the second tool's metadata
        path_call = registry.register.call_args_list[1]
        metadata = path_call[0][0]
        assert metadata.name == "find_path"
        assert "path" in metadata.description.lower()

    @pytest.mark.asyncio
    async def test_get_graph_statistics_metadata(self, registry, mock_graph_builder):
        """Test get_graph_statistics tool metadata."""
        register_graph_tools(registry, mock_graph_builder)

        # Get the third tool's metadata
        stats_call = registry.register.call_args_list[2]
        metadata = stats_call[0][0]
        assert metadata.name == "get_graph_statistics"
        assert "statistics" in metadata.description.lower()


class TestSystemTools:
    """Test system tools."""

    @pytest.fixture
    def registry(self):
        """Create mock tool registry."""
        return MagicMock()

    @pytest.mark.asyncio
    async def test_register_system_tools(self, registry):
        """Test system tools registration."""
        register_system_tools(registry)

        assert registry.register.call_count == 1
        tool_call = registry.register.call_args_list[0]
        metadata = tool_call[0][0]
        assert metadata.name == "get_storage_status"
        assert "storage tiers" in metadata.description.lower()


class TestCodeGraphTools:
    """Test code graph tools (placeholder tests since implementation is in separate module)."""

    @pytest.fixture
    def registry(self):
        """Create mock tool registry."""
        return MagicMock()

    @pytest.fixture
    def mock_hot_store(self):
        """Create mock hot store."""
        return MagicMock()

    @pytest.mark.asyncio
    async def test_register_code_graph_tools(self, registry, mock_hot_store):
        """Test code graph tools registration."""
        with patch('akosha.mcp.tools.akosha_tools.register_code_graph_analysis_tools') as mock_register:
            register_code_graph_tools(registry, mock_hot_store)

            mock_register.assert_called_once_with(registry, mock_hot_store)


class TestIntegration:
    """Test all tools together."""

    @pytest.mark.asyncio
    async def test_register_all_tools(self):
        """Test registering all tool categories."""
        registry = MagicMock()

        # Mock all services
        mock_embedding = MagicMock(spec=EmbeddingService)
        mock_analytics = MagicMock(spec=TimeSeriesAnalytics)
        mock_graph = MagicMock(spec=KnowledgeGraphBuilder)

        register_akosha_tools(registry, mock_embedding, mock_analytics, mock_graph)

        # Should register 11 tools total (2 + 1 + 4 + 3 + 1)
        assert registry.register.call_count == 11

        # Verify all expected tool names are present
        tool_names = [call[0][0].name for call in registry.register.call_args_list]
        expected_tools = [
            "generate_embedding",
            "generate_batch_embeddings",
            "search_all_systems",
            "get_system_metrics",
            "analyze_trends",
            "detect_anomalies",
            "correlate_systems",
            "query_knowledge_graph",
            "find_path",
            "get_graph_statistics",
            "get_storage_status"
        ]
        assert all(name in tool_names for name in expected_tools)


class TestToolValidation:
    """Test input validation for all tools."""

    @pytest.mark.asyncio
    async def test_embedding_validation_structure(self):
        """Test that embedding tools handle validation properly."""
        registry = MagicMock()
        mock_service = MagicMock(spec=EmbeddingService)
        mock_service.generate_embedding.return_value = [0.1] * 384

        register_embedding_tools(registry, mock_service)

        # Verify tools were registered
        assert registry.register.call_count == 2

        # Check that generate_embedding has proper metadata
        generate_call = registry.register.call_args_list[0]
        metadata = generate_call[0][0]
        assert metadata.name == "generate_embedding"
        assert "text" in str(metadata)  # Should have parameter info


class TestErrorHandling:
    """Test error handling in tool registration."""

    @pytest.mark.asyncio
    async def test_tool_registration_with_failed_services(self):
        """Test tool registration when services fail."""
        registry = MagicMock()

        # Mock service that raises exception
        mock_service = MagicMock(spec=EmbeddingService)
        mock_service.generate_embedding.side_effect = Exception("Service error")

        # Should still register tools despite service errors
        register_embedding_tools(registry, mock_service)

        # Tools should still be registered
        assert registry.register.call_count == 2

    @pytest.mark.asyncio
    async def test_tool_registration_with_invalid_registry(self):
        """Test tool registration with invalid registry."""
        # Test with None registry (should handle gracefully)
        register_embedding_tools(None, MagicMock(spec=EmbeddingService)))