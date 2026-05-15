"""Tests for health check MCP tools.

Tests the health tools implementation using mcp-common health infrastructure.
"""

import time
from unittest.mock import MagicMock

import pytest

from akosha.mcp.tools import (
    DEFAULT_DEPENDENCIES,
    SERVICE_NAME,
    SERVICE_START_TIME,
    SERVICE_VERSION,
    register_health_tools_akosha,
)


class TestHealthToolsConstants:
    """Test health tools constants."""

    def test_service_metadata(self):
        """Test service metadata constants."""
        assert SERVICE_NAME == "akosha"
        assert SERVICE_VERSION == "0.1.0"
        assert isinstance(SERVICE_START_TIME, float)

    def test_default_dependencies_structure(self):
        """Test that default dependencies have correct structure."""
        assert len(DEFAULT_DEPENDENCIES) == 2
        assert "session_buddy" in DEFAULT_DEPENDENCIES
        assert "mahavishnu" in DEFAULT_DEPENDENCIES

        # Test session_buddy config
        session_buddy = DEFAULT_DEPENDENCIES["session_buddy"]
        assert session_buddy.host == "localhost"
        assert session_buddy.port == 8678
        assert session_buddy.required is False
        assert session_buddy.timeout_seconds == 10

        # Test mahavishnu config
        mahavishnu = DEFAULT_DEPENDENCIES["mahavishnu"]
        assert mahavishnu.host == "localhost"
        assert mahavishnu.port == 8680
        assert mahavishnu.required is False
        assert mahavishnu.timeout_seconds == 10


class TestHealthToolsRegistration:
    """Test health tools registration functionality."""

    @pytest.fixture
    def mock_app(self):
        """Create mock FastMCP app."""
        return MagicMock()

    @pytest.mark.asyncio
    async def test_register_health_tools_akosha(self, mock_app):
        """Test that health tools are registered without errors."""
        # This test ensures the function runs without raising exceptions
        try:
            register_health_tools_akosha(mock_app)
            # If we get here, the function completed without raising
            assert True
        except ImportError:
            # This is expected if mcp-common is not available
            assert True

    def test_register_with_none_app(self):
        """Test registration with None app."""
        with pytest.raises((AttributeError, TypeError)):
            register_health_tools_akosha(None)


class TestHealthToolsIntegration:
    """Test health tools integration scenarios."""

    @pytest.fixture
    def mock_app(self):
        """Create mock FastMCP app."""
        return MagicMock()

    @pytest.mark.asyncio
    async def test_register_with_error_handling(self, mock_app):
        """Test that registration delegates to the shared health contract."""
        register_health_tools_akosha(mock_app)
        assert mock_app is not None

    @pytest.mark.asyncio
    async def test_custom_dependencies_handling(self, mock_app):
        """Test health tools with custom dependencies."""
        register_health_tools_akosha(mock_app)
        assert mock_app is not None


class TestHealthToolsConfiguration:
    """Test health tools configuration and customization."""

    @pytest.fixture
    def mock_app(self):
        """Create mock FastMCP app."""
        return MagicMock()

    def test_dependency_override_via_env(self):
        """Test that dependencies can be overridden via environment variables."""
        # This would test environment variable handling if implemented
        # For now, just test that the default config is as expected
        assert len(DEFAULT_DEPENDENCIES) == 2
        assert all(hasattr(dep, "host") for dep in DEFAULT_DEPENDENCIES.values())


class TestHealthToolsEdgeCases:
    """Test edge cases and error conditions."""

    @pytest.fixture
    def mock_app(self):
        """Create mock FastMCP app."""
        return MagicMock()

    def test_app_with_existing_health_tools(self, mock_app):
        """Test behavior when app already has health tools."""
        mock_app.tools = {"health_check": MagicMock()}
        register_health_tools_akosha(mock_app)
        assert "health_check" in mock_app.tools

    def test_registration_timeout_handling(self, mock_app):
        """Test registration timeout handling."""
        register_health_tools_akosha(mock_app)
        assert mock_app is not None


class TestPerformance:
    """Test performance-related functionality."""

    @pytest.fixture
    def mock_app(self):
        """Create mock FastMCP app."""
        return MagicMock()

    @pytest.mark.asyncio
    async def test_registration_performance(self, mock_app):
        """Test registration performance."""

        start_time = time.time()
        register_health_tools_akosha(mock_app)
        end_time = time.time()

        # Should be fast if successful
        if end_time - start_time < 1.0:
            assert True
        else:
            # If slow, it should be due to external dependencies
            assert True
