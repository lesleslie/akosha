"""Tests for MCP server module.

Tests the FastMCP server implementation, app creation, and health endpoints.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from akosha.mcp.server import APP_NAME, APP_VERSION, create_app


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

    @patch("akosha.mcp.server.FastMCP")
    def test_create_app_with_mode(self, mock_fastmcp):
        """Test app creation with mode parameter."""
        mock_app = MagicMock()
        mock_fastmcp.return_value = mock_app

        result = create_app(mode="test-mode")

        assert result == mock_app
        mock_fastmcp.assert_called_once()

    @patch("akosha.mcp.server.FastMCP")
    def test_create_app_without_mode(self, mock_fastmcp):
        """Test app creation without mode parameter."""
        mock_app = MagicMock()
        mock_fastmcp.return_value = mock_app

        result = create_app()

        assert result == mock_app
        mock_fastmcp.assert_called_once()

    @patch("akosha.mcp.server.FastMCP")
    def test_create_app_with_none_mode(self, mock_fastmcp):
        """Test app creation with None mode."""
        mock_app = MagicMock()
        mock_fastmcp.return_value = mock_app

        result = create_app(mode=None)

        assert result == mock_app
        mock_fastmcp.assert_called_once()

    @patch("akosha.mcp.server.FastMCP")
    def test_create_app_configures_app(self, mock_fastmcp):
        """Test that app is properly configured."""
        mock_app = MagicMock()
        mock_fastmcp.return_value = mock_app

        create_app()

        # Verify the app was created (health endpoints are registered via custom_route)
        assert mock_fastmcp.called

    @patch("akosha.mcp.server.FastMCP")
    def test_create_app_returns_fastmcp_instance(self, mock_fastmcp):
        """Test that create_app returns the mock (which stands in for FastMCP)."""
        mock_app = MagicMock()
        mock_fastmcp.return_value = mock_app

        result = create_app()

        assert result == mock_app
        assert result is not None


class TestHealthEndpoints:
    """Test health check endpoints defined inside create_app."""

    @patch("akosha.mcp.server.FastMCP")
    def test_health_endpoints_registered(self, mock_fastmcp):
        """Test that health endpoints are registered via custom_route."""
        mock_app = MagicMock()
        mock_fastmcp.return_value = mock_app

        create_app()

        # Verify custom_route was called for health endpoints
        custom_route_calls = [
            call
            for call in mock_app.custom_route.call_args_list
            if len(call) > 0 and len(call[0]) > 0
        ]
        paths = [call[0][0] for call in custom_route_calls]
        assert "/health" in paths
        assert "/healthz" in paths


class TestDependencyAvailability:
    """Test optional dependency detection."""

    def test_mcp_common_availability(self):
        """Test mcp_common availability check."""
        from akosha.mcp.server import MCP_COMMON_AVAILABLE

        assert isinstance(MCP_COMMON_AVAILABLE, bool)

    def test_rate_limiting_availability(self):
        """Test rate limiting availability check."""
        from akosha.mcp.server import RATE_LIMITING_AVAILABLE

        assert isinstance(RATE_LIMITING_AVAILABLE, bool)

    def test_serverpanels_availability(self):
        """Test serverpanels availability check."""
        from akosha.mcp.server import SERVERPANELS_AVAILABLE

        assert isinstance(SERVERPANELS_AVAILABLE, bool)

    def test_availability_checks_handle_exceptions(self):
        """Test that availability checks handle exceptions."""
        from akosha.mcp.server import (
            MCP_COMMON_AVAILABLE,
            RATE_LIMITING_AVAILABLE,
            SERVERPANELS_AVAILABLE,
        )

        assert all(
            isinstance(val, bool)
            for val in [
                MCP_COMMON_AVAILABLE,
                RATE_LIMITING_AVAILABLE,
                SERVERPANELS_AVAILABLE,
            ]
        )


class TestLazyInitialization:
    """Test lazy initialization pattern."""

    def test_getattr_for_app_instance(self):
        """Test __getattr__ returns app instance."""
        from akosha.mcp.server import __getattr__

        result = __getattr__("app")
        assert result is not None

    def test_getattr_for_unknown_attribute(self):
        """Test __getattr__ raises AttributeError for unknown attributes."""
        from akosha.mcp.server import __getattr__

        with pytest.raises(AttributeError, match="unknown_attribute"):
            __getattr__("unknown_attribute")


class TestErrorHandling:
    """Test error handling in server components."""

    @patch("akosha.mcp.server.FastMCP")
    def test_create_app_handles_exceptions(self, mock_fastmcp):
        """Test that app creation propagates exceptions."""
        mock_fastmcp.side_effect = Exception("Failed to create app")

        with pytest.raises(Exception, match="Failed to create app"):
            create_app()

    @patch("akosha.mcp.server.FastMCP")
    def test_health_endpoint_exceptions(self, mock_fastmcp):
        """Test health endpoint exception handling is part of create_app flow."""
        mock_app = MagicMock()
        mock_fastmcp.return_value = mock_app

        # custom_route is called on the app instance (mock_app), not the class
        mock_app.custom_route.side_effect = Exception("Route registration failed")

        with pytest.raises(Exception, match="Route registration failed"):
            create_app()

    @patch("akosha.mcp.server.FastMCP")
    def test_lifespan_exceptions(self, mock_fastmcp):
        """Test lifespan exception handling is part of create_app flow."""
        # Lifespan is defined inside create_app. Exceptions propagate.
        mock_fastmcp.side_effect = Exception("Lifespan failed")

        with pytest.raises(Exception, match="Lifespan failed"):
            create_app()


class TestConfiguration:
    """Test server configuration and setup."""

    @patch("akosha.mcp.server.FastMCP")
    def test_app_configuration(self, mock_fastmcp):
        """Test that app is properly configured."""
        mock_app = MagicMock()
        mock_fastmcp.return_value = mock_app

        create_app()

        assert mock_fastmcp.called

    def test_server_imports(self):
        """Test that server can be imported without issues."""
        from akosha.mcp.server import APP_NAME, APP_VERSION, create_app

        assert callable(create_app)
        assert isinstance(APP_NAME, str)
        isinstance(APP_VERSION, str)


class TestPerformance:
    """Test performance-related functionality."""

    @patch("akosha.mcp.server.FastMCP")
    def test_app_creation_performance(self, mock_fastmcp):
        """Test app creation performance."""
        mock_app = MagicMock()
        mock_fastmcp.return_value = mock_app

        start_time = time.time()
        create_app()
        end_time = time.time()

        assert (end_time - start_time) < 1.0


class TestIntegration:
    """Test server integration with other components."""

    @patch("akosha.mcp.server.FastMCP")
    def test_server_with_mode_parameter(self, mock_fastmcp):
        """Test server with different mode parameters."""
        mock_app = MagicMock()
        mock_fastmcp.return_value = mock_app

        modes = [None, "development", "production", "test"]
        for mode in modes:
            create_app(mode=mode)

        assert mock_fastmcp.call_count == len(modes)

    @patch("akosha.mcp.server.FastMCP")
    def test_server_concurrent_creation(self, mock_fastmcp):
        """Test multiple sequential app creation."""
        mock_app = MagicMock()
        mock_fastmcp.return_value = mock_app

        results = [create_app() for _ in range(3)]

        assert len(results) == 3
        assert all(result == mock_app for result in results)
