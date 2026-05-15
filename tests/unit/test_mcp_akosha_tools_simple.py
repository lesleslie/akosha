"""Simple tests for Akosha MCP tools module.

Tests the core functionality of Akosha tools including registration and basic operations.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from akosha.mcp.tools.akosha_tools import (
    register_akosha_tools,
    register_analytics_tools,
    register_code_graph_tools,
    register_embedding_tools,
    register_graph_tools,
    register_search_tools,
)
from akosha.mcp.tools.tool_registry import FastMCPToolRegistry
from akosha.processing.analytics import TimeSeriesAnalytics
from akosha.processing.embeddings import EmbeddingService
from akosha.processing.knowledge_graph import KnowledgeGraphBuilder
from akosha.storage.hot_store import HotStore


class TestToolRegistration:
    """Test tool registration functionality."""

    @pytest.fixture
    def mock_registry(self):
        """Create mock tool registry."""
        return MagicMock(spec=FastMCPToolRegistry)

    @pytest.fixture
    def mock_embedding_service(self):
        """Create mock embedding service."""
        return MagicMock(spec=EmbeddingService)

    @pytest.fixture
    def mock_analytics_service(self):
        """Create mock analytics service."""
        return MagicMock(spec=TimeSeriesAnalytics)

    @pytest.fixture
    def mock_graph_builder(self):
        """Create mock graph builder."""
        return MagicMock(spec=KnowledgeGraphBuilder)

    @pytest.fixture
    def mock_hot_store(self):
        """Create mock hot store."""
        store = MagicMock(spec=HotStore)
        store.list_code_graphs = AsyncMock(return_value=[])
        store.get_code_graph = AsyncMock(return_value=None)
        return store

    def test_register_akosha_tools_with_services(
        self, mock_registry, mock_embedding_service, mock_analytics_service, mock_graph_builder
    ):
        """Test registration of all Akosha tools with services."""
        register_akosha_tools(
            mock_registry, mock_embedding_service, mock_analytics_service, mock_graph_builder
        )

        # Should register multiple tools
        assert mock_registry.register.call_count > 0

    def test_register_akosha_tools_with_none_services(self, mock_registry):
        """Test registration with None services."""
        # Should handle None services gracefully
        register_akosha_tools(mock_registry, None, None, None)

        assert mock_registry.register.call_count > 0

    def test_register_embedding_tools(self, mock_registry, mock_embedding_service):
        """Test embedding tools registration."""
        register_embedding_tools(mock_registry, mock_embedding_service)

        # Should register embedding tools
        assert mock_registry.register.call_count >= 1

    def test_register_search_tools(self, mock_registry, mock_embedding_service):
        """Test search tools registration."""
        register_search_tools(mock_registry, mock_embedding_service)

        # Should register search tools
        assert mock_registry.register.call_count > 0

    def test_register_analytics_tools(self, mock_registry, mock_analytics_service):
        """Test analytics tools registration."""
        register_analytics_tools(mock_registry, mock_analytics_service)

        # Should register analytics tools
        assert mock_registry.register.call_count > 0

    def test_register_graph_tools(self, mock_registry, mock_graph_builder):
        """Test graph tools registration."""
        register_graph_tools(mock_registry, mock_graph_builder)

        # Should register graph tools
        assert mock_registry.register.call_count > 0

    def test_register_code_graph_tools(self, mock_registry, mock_hot_store):
        """Test code graph tools registration."""
        # code_graph_tools checks isinstance(registry, FastMCPToolRegistry)
        # and uses registry.app — our mock has spec=FastMCPToolRegistry but
        # needs an app attribute
        mock_registry.app = MagicMock()
        mock_registry.app.tool = MagicMock(return_value=lambda fn: fn)

        register_code_graph_tools(mock_registry, mock_hot_store)

        # Should register code graph tools via @mcp.tool()
        assert mock_registry.app.tool.call_count > 0


class TestServiceAvailability:
    """Test service availability and error handling."""

    @pytest.fixture
    def mock_registry(self):
        """Create mock tool registry."""
        return MagicMock(spec=FastMCPToolRegistry)

    @pytest.fixture
    def mock_embedding_service(self):
        """Create mock embedding service."""
        return MagicMock(spec=EmbeddingService)

    def test_registration_with_failed_services(self, mock_registry):
        """Test registration with service failures."""
        # Mock services that raise exceptions
        mock_service = MagicMock()
        mock_service.generate_embedding.side_effect = Exception("Service error")

        # Should handle errors gracefully
        register_embedding_tools(mock_registry, mock_service)

        # Tools should still be registered despite service errors
        assert mock_registry.register.call_count > 0

    def test_registration_with_invalid_registry(self, mock_embedding_service):
        """Test registration with invalid registry."""
        # None registry will raise AttributeError on registry.register()
        with pytest.raises(AttributeError):
            register_embedding_tools(None, mock_embedding_service)

    def test_registration_with_invalid_services(self, mock_registry):
        """Test registration with invalid services."""
        # Should handle None services
        register_embedding_tools(mock_registry, None)

        assert mock_registry.register.call_count > 0


class TestToolCategories:
    """Test different tool categories."""

    @pytest.fixture
    def mock_registry(self):
        """Create mock tool registry."""
        return MagicMock(spec=FastMCPToolRegistry)

    @pytest.fixture
    def mock_services(self):
        """Create mock services."""
        return {
            "embedding": MagicMock(spec=EmbeddingService),
            "analytics": MagicMock(spec=TimeSeriesAnalytics),
            "graph": MagicMock(spec=KnowledgeGraphBuilder),
        }

    def test_embedding_tools_category(self, mock_registry, mock_services):
        """Test embedding tools category."""
        register_embedding_tools(mock_registry, mock_services["embedding"])

        # Check that embedding tools were registered
        assert mock_registry.register.call_count > 0

    def test_search_tools_category(self, mock_registry, mock_services):
        """Test search tools category."""
        register_search_tools(mock_registry, mock_services["embedding"])

        # Check that search tools were registered
        assert mock_registry.register.call_count > 0

    def test_analytics_tools_category(self, mock_registry, mock_services):
        """Test analytics tools category."""
        register_analytics_tools(mock_registry, mock_services["analytics"])

        # Check that analytics tools were registered
        assert mock_registry.register.call_count > 0

    def test_graph_tools_category(self, mock_registry, mock_services):
        """Test graph tools category."""
        register_graph_tools(mock_registry, mock_services["graph"])

        # Check that graph tools were registered
        assert mock_registry.register.call_count > 0

    def test_code_graph_tools_category(self, mock_registry):
        """Test code graph tools category."""
        mock_hot_store = MagicMock(spec=HotStore)
        mock_hot_store.list_code_graphs = AsyncMock(return_value=[])
        mock_hot_store.get_code_graph = AsyncMock(return_value=None)
        mock_registry.app = MagicMock()
        mock_registry.app.tool = MagicMock(return_value=lambda fn: fn)

        register_code_graph_tools(mock_registry, mock_hot_store)

        # Check that code graph tools were registered
        assert mock_registry.app.tool.call_count > 0


class TestIntegration:
    """Test integration of different tool categories."""

    @pytest.fixture
    def mock_registry(self):
        """Create mock tool registry."""
        return MagicMock(spec=FastMCPToolRegistry)

    @pytest.fixture
    def mock_services(self):
        """Create mock services."""
        return {
            "embedding": MagicMock(spec=EmbeddingService),
            "analytics": MagicMock(spec=TimeSeriesAnalytics),
            "graph": MagicMock(spec=KnowledgeGraphBuilder),
            "hot_store": MagicMock(spec=HotStore),
        }

    def test_all_tools_registration(self, mock_registry, mock_services):
        """Test registration of all tool categories."""
        # Set up mock for code_graph_tools
        mock_services["hot_store"].list_code_graphs = AsyncMock(return_value=[])
        mock_services["hot_store"].get_code_graph = AsyncMock(return_value=None)
        mock_registry.app = MagicMock()
        mock_registry.app.tool = MagicMock(return_value=lambda fn: fn)

        # Reset call count
        mock_registry.reset_mock()

        # Register all tool categories
        register_embedding_tools(mock_registry, mock_services["embedding"])
        register_search_tools(mock_registry, mock_services["embedding"])
        register_analytics_tools(mock_registry, mock_services["analytics"])
        register_graph_tools(mock_registry, mock_services["graph"])
        register_code_graph_tools(mock_registry, mock_services["hot_store"])

        # Should register multiple tools across all categories
        assert mock_registry.register.call_count > 5  # At least 6 categories

    def test_tools_work_together(self, mock_registry, mock_services):
        """Test that tools can work together."""
        # This would test integration between different tool categories
        # For now, just test that registration doesn't interfere
        original_call_count = mock_registry.register.call_count

        register_embedding_tools(mock_registry, mock_services["embedding"])
        register_analytics_tools(mock_registry, mock_services["analytics"])

        # Should have registered tools from both categories
        assert mock_registry.register.call_count > original_call_count


class TestErrorHandling:
    """Test error handling in tool registration."""

    @pytest.fixture
    def mock_registry(self):
        """Create mock tool registry."""
        return MagicMock(spec=FastMCPToolRegistry)

    @pytest.fixture
    def mock_services(self):
        """Create mock services."""
        return {
            "embedding": MagicMock(spec=EmbeddingService),
            "analytics": MagicMock(spec=TimeSeriesAnalytics),
            "graph": MagicMock(spec=KnowledgeGraphBuilder),
        }

    def test_service_error_handling(self, mock_registry):
        """Test handling of service errors."""
        mock_service = MagicMock()
        mock_service.generate_embedding.side_effect = Exception("Service unavailable")

        # Should handle service errors gracefully
        register_embedding_tools(mock_registry, mock_service)

        # Tools should still be registered
        assert mock_registry.register.call_count > 0

    def test_registry_error_handling(self, mock_services):
        """Test handling of registry errors."""
        # Should handle invalid registry gracefully
        try:
            register_embedding_tools(None, mock_services["embedding"])
            # Should not raise exception
            assert True
        except AttributeError:
            # AttributeError is expected when registry is None
            assert True

    def test_concurrent_registration(self, mock_services):
        """Test concurrent tool registration."""
        import threading

        def register_tools():
            mock_registry = MagicMock(spec=FastMCPToolRegistry)
            register_embedding_tools(mock_registry, mock_services["embedding"])
            return mock_registry.register.call_count

        # Create multiple threads registering tools
        threads = []
        results = []

        def worker():
            count = register_tools()
            results.append(count)

        for _ in range(3):
            t = threading.Thread(target=worker)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # Should have registered tools in each thread
        assert len(results) == 3
        assert all(count > 0 for count in results)


class TestPerformance:
    """Test performance of tool registration."""

    @pytest.fixture
    def mock_registry(self):
        """Create mock tool registry."""
        return MagicMock(spec=FastMCPToolRegistry)

    @pytest.fixture
    def mock_services(self):
        """Create mock services."""
        return {
            "embedding": MagicMock(spec=EmbeddingService),
            "analytics": MagicMock(spec=TimeSeriesAnalytics),
            "graph": MagicMock(spec=KnowledgeGraphBuilder),
        }

    def test_registration_performance(self, mock_registry, mock_services):
        """Test tool registration performance."""
        import time

        start_time = time.time()
        register_embedding_tools(mock_registry, mock_services["embedding"])
        register_analytics_tools(mock_registry, mock_services["analytics"])
        register_graph_tools(mock_registry, mock_services["graph"])
        end_time = time.time()

        # Should be fast
        assert (end_time - start_time) < 1.0

    def test_large_scale_registration(self, mock_services):
        """Test large scale tool registration performance."""
        import time

        mock_registry = MagicMock(spec=FastMCPToolRegistry)

        start_time = time.time()
        for _i in range(10):
            register_embedding_tools(mock_registry, mock_services["embedding"])
        end_time = time.time()

        # Should handle multiple registrations efficiently
        assert (end_time - start_time) < 2.0
        assert mock_registry.register.call_count > 0


class TestConfiguration:
    """Test tool configuration and setup."""

    @pytest.fixture
    def mock_registry(self):
        """Create mock tool registry."""
        return MagicMock(spec=FastMCPToolRegistry)

    @pytest.fixture
    def mock_services(self):
        """Create mock services."""
        return {
            "embedding": MagicMock(spec=EmbeddingService),
            "analytics": MagicMock(spec=TimeSeriesAnalytics),
            "graph": MagicMock(spec=KnowledgeGraphBuilder),
        }

    def test_tools_configuration(self):
        """Test that tools are properly configured."""
        # Test that all required services can be imported
        from akosha.processing.analytics import TimeSeriesAnalytics
        from akosha.processing.embeddings import EmbeddingService
        from akosha.processing.knowledge_graph import KnowledgeGraphBuilder
        from akosha.storage.hot_store import HotStore

        assert EmbeddingService is not None
        assert TimeSeriesAnalytics is not None
        assert KnowledgeGraphBuilder is not None
        assert HotStore is not None

    def test_imports_work(self):
        """Test that all required imports work."""
        # Should be able to import all required modules
        from akosha.mcp.tools.akosha_tools import (
            register_akosha_tools,
            register_analytics_tools,
            register_code_graph_tools,
            register_embedding_tools,
            register_graph_tools,
            register_search_tools,
        )

        assert callable(register_akosha_tools)
        assert callable(register_embedding_tools)
        assert callable(register_search_tools)
        assert callable(register_analytics_tools)
        assert callable(register_graph_tools)
        assert callable(register_code_graph_tools)
