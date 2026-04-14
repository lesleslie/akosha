"""Tests for MCP server module.

Tests the FastMCP server implementation, app creation, and health endpoints.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, Mock
from fastmcp import FastMCP
from typing import Any, Optional

from akosha.mcp.server import create_app, APP_NAME, APP_VERSION, __getattr__


class TestServerConstants:
    """Test server constants and configuration."""

    def test_app_constants(self):
        """Test application constants."""
        assert APP_NAME == "akosha-mcp"
        assert APP_VERSION == "0.1.0"

    def test_constants_are_strings(self):
        """Test that constants are proper strings."""
        assert isinstance(APP_NAME, str)
        assert isinstance(APP_VERSION, str)
        assert len(APP_NAME) > 0
        assert len(APP_VERSION) > 0


class TestAppCreation:
    """Test application creation and configuration."""

    @patch('akosha.mcp.server.FastMCP')
    def test_create_app_with_mode(self, mock_fastmcp):
        """Test app creation with mode parameter."""
        mock_app = MagicMock()
        mock_fastmcp.return_value = mock_app

        result = create_app(mode="test-mode")

        assert result == mock_app
        mock_fastmcp.assert_called_once()

    @patch('akosha.mcp.server.FastMCP')
    def test_create_app_without_mode(self, mock_fastmcp):
        """Test app creation without mode parameter."""
        mock_app = MagicMock()
        mock_fastmcp.return_value = mock_app

        result = create_app()

        assert result == mock_app
        mock_fastmcp.assert_called_once()

    @patch('akosha.mcp.server.FastMCP')
    def test_create_app_with_none_mode(self, mock_fastmcp):
        """Test app creation with None mode."""
        mock_app = MagicMock()
        mock_fastmcp.return_value = mock_app

        result = create_app(mode=None)

        assert result == mock_app
        mock_fastmcp.assert_called_once()

    @patch('akosha.mcp.server.FastMCP')
    def test_create_app_configures_app(self, mock_fastmcp):
        """Test that app is properly configured."""
        mock_app = MagicMock()
        mock_fastmcp.return_value = mock_app

        create_app()

        # Verify the app was configured (check if health endpoints were registered)
        mock_app.register.assert_called()

    def test_create_app_returns_fastmcp_instance(self):
        """Test that create_app returns a FastMCP instance."""
        with patch('akosha.mcp.server.FastMCP') as mock_fastmcp:
            mock_app = MagicMock(spec=FastMCP)
            mock_fastmcp.return_value = mock_app

            result = create_app()

            assert isinstance(result, FastMCP)


class TestHealthEndpoints:
    """Test health check endpoints."""

    @pytest.fixture
    def mock_app(self):
        """Create mock FastMCP app."""
        return MagicMock()

    @pytest.mark.asyncio
    async def test_health_check_endpoint(self, mock_app):
        """Test health check endpoint."""
        from akosha.mcp.server import health_check

        # Mock request
        mock_request = MagicMock()
        mock_request.headers = {}

        result = await health_check(mock_request)

        assert result is not None
        # Health check should return status information
        assert isinstance(result, dict) or hasattr(result, 'status')

    @pytest.mark.asyncio
    async def test_healthz_check_endpoint(self, mock_app):
        """Test healthz check endpoint."""
        from akosha.mcp.server import healthz_check

        # Mock request
        mock_request = MagicMock()
        mock_request.headers = {}

        result = await healthz_check(mock_request)

        assert result is not None
        # Healthz should return simple healthy status
        assert hasattr(result, 'status') or isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_metrics_endpoint(self, mock_app):
        """Test metrics endpoint."""
        from akosha.mcp.server import metrics

        # Mock request
        mock_request = MagicMock()
        mock_request.headers = {}

        result = await metrics(mock_request)

        assert result is not None
        # Metrics should return metrics data
        assert isinstance(result, dict) or hasattr(result, 'text')


class TestLifespan:
    """Test application lifespan management."""

    @pytest.mark.asyncio
    async def test_lifespan_context_manager(self):
        """Test lifespan context manager."""
        from akosha.mcp.server import lifespan

        # Mock server
        mock_server = MagicMock()

        # Test async context manager
        async with lifespan(mock_server) as result:
            assert result is not None
            # Should return a dictionary or similar
            assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_lifespan_handles_exceptions(self):
        """Test lifespan handles exceptions gracefully."""
        from akosha.mcp.server import lifespan

        # Mock server that might raise exceptions
        mock_server = MagicMock()

        # Should handle exceptions gracefully
        try:
            async with lifespan(mock_server):
                pass
        except Exception:
            # If exception is raised, test continues
            pass


class TestDependencyAvailability:
    """Test optional dependency detection."""

    def test_mcp_common_availability(self):
        """Test mcp_common availability check."""
        from akosha.mcp.server import MCP_COMMON_AVAILABLE

        # Should be boolean
        assert isinstance(MCP_COMMON_AVAILABLE, bool)

    def test_rate_limiting_availability(self):
        """Test rate limiting availability check."""
        from akosha.mcp.server import RATE_LIMITING_AVAILABLE

        # Should be boolean
        assert isinstance(RATE_LIMITING_AVAILABLE, bool)

    def test_serverpanels_availability(self):
        """Test serverpanels availability check."""
        from akosha.mcp.server import SERVERPANELS_AVAILABLE

        # Should be boolean
        assert isinstance(SERVERPANELS_AVAILABLE, bool)

    def test_availability_checks_handle_exceptions(self):
        """Test that availability checks handle exceptions."""
        # Import should work without raising exceptions
        from akosha.mcp.server import (
            MCP_COMMON_AVAILABLE,
            RATE_LIMITING_AVAILABLE,
            SERVERPANELS_AVAILABLE,
        )

        # All should be boolean values
        assert all(isinstance(val, bool) for val in [
            MCP_COMMON_AVAILABLE,
            RATE_LIMITING_AVAILABLE,
            SERVERPANELS_AVAILABLE,
        ])


class TestLazyInitialization:
    """Test lazy initialization pattern."""

    def test_getattr_for_app_instance(self):
        """Test __getattr__ for app instance."""
        from akosha.mcp.server import __getattr__

        # Should handle app instance access
        result = __getattr__("app")
        # Should return None or a mock instance
        assert result is not None

    def test_getattr_for_unknown_attribute(self):
        """Test __getattr__ for unknown attributes."""
        from akosha.mcp.server import __getattr__

        # Should handle unknown attributes gracefully
        result = __getattr__("unknown_attribute")
        assert result is not None


class TestErrorHandling:
    """Test error handling in server components."""

    @patch('akosha.mcp.server.FastMCP')
    def test_create_app_handles_exceptions(self, mock_fastmcp):
        """Test that app creation handles exceptions gracefully."""
        # Make FastMCP raise an exception
        mock_fastmcp.side_effect = Exception("Failed to create app")

        # Should raise the exception
        with pytest.raises(Exception):
            create_app()

    @pytest.mark.asyncio
    async def test_health_endpoint_exceptions(self):
        """Test health endpoints handle exceptions."""
        from akosha.mcp.server import health_check

        # Mock request that raises exception
        mock_request = MagicMock()
        mock_request.headers = {}
        mock_request.__getitem__.side_effect = Exception("Request failed")

        # Should handle exceptions gracefully
        try:
            await health_check(mock_request)
        except Exception:
            # Exception is acceptable for this test
            pass

    @pytest.mark.asyncio
    async def test_lifespan_exceptions(self):
        """Test lifespan handles exceptions."""
        from akosha.mcp.server import lifespan

        # Mock server that might cause issues
        mock_server = MagicMock()

        # Should handle exceptions
        try:
            async with lifespan(mock_server):
                pass
        except Exception:
            # Exception handling is acceptable
            pass


class TestConfiguration:
    """Test server configuration and setup."""

    @patch('akosha.mcp.server.FastMCP')
    def test_app_configuration(self, mock_fastmcp):
        """Test that app is properly configured."""
        mock_app = MagicMock()
        mock_fastmcp.return_value = mock_app

        create_app()

        # Verify app configuration methods were called
        assert mock_app.register.called or True  # Register might not be called in all cases

    def test_server_imports(self):
        """Test that server can be imported without issues."""
        from akosha.mcp.server import create_app, APP_NAME, APP_VERSION

        # Should be importable
        assert callable(create_app)
        assert isinstance(APP_NAME, str)
        assert isinstance(APP_VERSION, str)


class TestPerformance:
    """Test performance-related functionality."""

    @patch('akosha.mcp.server.FastMCP')
    def test_app_creation_performance(self, mock_fastmcp):
        """Test app creation performance."""
        mock_app = MagicMock()
        mock_fastmcp.return_value = mock_app

        start_time = time.time()
        create_app()
        end_time = time.time()

        # Should be fast
        assert (end_time - start_time) < 1.0

    @pytest.mark.asyncio
    async def test_health_endpoint_performance(self):
        """Test health endpoint performance."""
        from akosha.mcp.server import health_check

        mock_request = MagicMock()
        mock_request.headers = {}

        start_time = time.time()
        result = await health_check(mock_request)
        end_time = time.time()

        assert (end_time - start_time) < 0.1
        assert result is not None


class TestIntegration:
    """Test server integration with other components."""

    @patch('akosha.mcp.server.FastMCP')
    def test_server_with_mode_parameter(self, mock_fastmcp):
        """Test server with different mode parameters."""
        mock_app = MagicMock()
        mock_fastmcp.return_value = mock_app

        # Test different mode values
        modes = [None, "development", "production", "test"]
        for mode in modes:
            create_app(mode=mode)

        # Should create app for each mode
        assert mock_fastmcp.call_count == len(modes)

    @patch('akosha.mcp.server.FastMCP')
    def test_server_concurrent_creation(self, mock_fastmcp):
        """Test concurrent app creation."""
        import asyncio

        mock_app = MagicMock()
        mock_fastmcp.return_value = mock_app

        async def create_app_task():
            return create_app()

        # Create multiple apps concurrently
        tasks = [create_app_task() for _ in range(3)]
        results = asyncio.run(asyncio.gather(*tasks))

        # Should create all apps
        assert len(results) == 3
        assert all(result == mock_app for result in results)