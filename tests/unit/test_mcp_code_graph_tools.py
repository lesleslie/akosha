"""Tests for code graph MCP tools.

Tests the register_code_graph_analysis_tools function which is the main
entry point for code graph analysis tools.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from akosha.mcp.tools.code_graph_tools import register_code_graph_analysis_tools
from akosha.storage.hot_store import HotStore
from akosha.mcp.tools.tool_registry import FastMCPToolRegistry


class TestCodeGraphToolsRegistration:
    """Test code graph tools registration."""

    @pytest.fixture
    def mock_hot_store(self):
        """Create mock hot store."""
        store = MagicMock(spec=HotStore)
        store.search_similar.return_value = []
        return store

    @pytest.fixture
    def registry(self):
        """Create mock tool registry."""
        return MagicMock(spec=FastMCPToolRegistry)

    def test_register_code_graph_analysis_tools(self, registry, mock_hot_store):
        """Test code graph tools registration."""
        register_code_graph_analysis_tools(registry, mock_hot_store)

        # Should register tools (actual count depends on implementation)
        assert registry.register.call_count >= 1

        # Verify the registration call includes the right arguments
        call_args = registry.register.call_args_list[0]
        assert call_args[0][0] is not None  # Should have tool metadata

    @pytest.mark.asyncio
    async def test_register_with_error_handling(self, registry, mock_hot_store):
        """Test that registration handles errors gracefully."""
        # Make hot_store raise an exception
        mock_hot_store.search_similar.side_effect = Exception("Search failed")

        register_code_graph_analysis_tools(registry, mock_hot_store)

        # Registration should complete despite search errors
        assert registry.register.call_count >= 1

    def test_register_with_invalid_registry(self, mock_hot_store):
        """Test registration with invalid registry."""
        # Test with None registry (should handle gracefully)
        register_code_graph_analysis_tools(None, mock_hot_store)

    def test_register_with_invalid_store(self, registry):
        """Test registration with invalid store."""
        # Test with None store (should handle gracefully)
        register_code_graph_analysis_tools(registry, None)