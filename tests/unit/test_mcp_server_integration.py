"""Integration tests for MCP server components.

Tests the integration between server components including auth, validation,
and tool registration.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import FastAPI

from akosha.mcp.server import create_app
from akosha.mcp.auth import JWTAuthHandler
from akosha.mcp.validation import validate_request, GenerateEmbeddingRequest
from akosha.security import ValidationError


class TestServerIntegration:
    """Test server integration components."""

    def test_create_app_fastapi_instance(self):
        """Test that create_app returns a FastAPI instance."""
        app = create_app()

        assert isinstance(app, FastAPI)
        assert app.title == "Akosha MCP Server"
        assert app.version == "0.4.0"

    def test_app_with_custom_configuration(self):
        """Test app creation with custom configuration."""
        # This would test custom server configuration
        app = create_app()

        # Verify app has expected configuration
        assert hasattr(app, 'routes')
        assert len(app.routes) > 0


class TestToolRegistrationIntegration:
    """Test tool registration integration."""

    @pytest.fixture
    def mock_embedding_service(self):
        """Create mock embedding service."""
        service = MagicMock()
        service.generate_embedding.return_value = [0.1] * 384
        service.is_available.return_value = True
        return service

    @pytest.fixture
    def mock_analytics_service(self):
        """Create mock analytics service."""
        service = MagicMock()
        service.get_metric_names.return_value = ["test_metric"]
        service.analyze_trend.return_value = None
        return service

    @pytest.fixture
    def mock_graph_builder(self):
        """Create mock graph builder."""
        builder = MagicMock()
        builder.get_neighbors.return_value = []
        builder.find_shortest_path.return_value = None
        return builder

    @pytest.mark.asyncio
    async def test_tool_registration_with_services(self, mock_embedding_service, mock_analytics_service, mock_graph_builder):
        """Test tool registration with all services."""
        app = create_app()

        # Mock FastMCP and tool registration
        with patch('akosha.mcp.server.FastMCP') as mock_fastmcp:
            mock_registry = MagicMock()
            mock_fastmcp.return_value = mock_registry

            # Test that all tool categories can be registered
            from akosha.mcp.tools.akosha_tools import register_akosha_tools

            register_akosha_tools(mock_registry, mock_embedding_service, mock_analytics_service, mock_graph_builder)

            # Verify tools were registered
            assert mock_registry.register.call_count > 0

    @pytest.mark.asyncio
    async def test_tool_registration_error_handling(self, mock_embedding_service):
        """Test tool registration with error handling."""
        app = create_app()

        with patch('akosha.mcp.server.FastMCP') as mock_fastmcp:
            mock_registry = MagicMock()
            mock_fastmcp.return_value = mock_registry

            # Mock service that raises exception
            mock_service = MagicMock()
            mock_service.generate_embedding.side_effect = Exception("Service error")

            # Should handle errors gracefully
            from akosha.mcp.tools.akosha_tools import register_embedding_tools

            register_embedding_tools(mock_registry, mock_service)

            # Tools should still be registered despite service errors
            assert mock_registry.register.call_count > 0


class TestAuthValidationIntegration:
    """Test authentication and validation integration."""

    @pytest.fixture
    def auth_handler(self):
        """Create auth handler."""
        return JWTAuthHandler()

    @pytest.mark.asyncio
    async def test_authenticated_request_validation(self, auth_handler):
        """Test request validation with authentication."""
        # Generate token
        token = auth_handler.generate_token("user123")

        # Validate token
        decoded = auth_handler.validate_token(token)
        assert decoded["user_id"] == "user123"

        # Validate request with authenticated user
        request_data = {"text": "hello world", "user_id": decoded["user_id"]}
        result = validate_request(GenerateEmbeddingRequest, **request_data)

        assert result["text"] == "hello world"
        assert result["user_id"] == "user123"

    @pytest.mark.asyncio
    async def test_unauthenticated_request(self):
        """Test request validation without authentication."""
        # Request without authentication
        request_data = {"text": "hello world"}

        # Should still validate data structure
        result = validate_request(GenerateEmbeddingRequest, **request_data)

        assert result["text"] == "hello world"


class TestErrorHandlingIntegration:
    """Test integrated error handling."""

    def test_validation_error_propagation(self):
        """Test that validation errors are properly propagated."""
        request_data = {"text": ""}

        # Should raise validation error
        with pytest.raises(ValidationError):
            validate_request(GenerateEmbeddingRequest, **request_data)

    @pytest.mark.asyncio
    async def test_middleware_error_handling(self, mock_embedding_service):
        """Test middleware error handling."""
        app = create_app()

        # Mock middleware that raises exception
        with patch('akosha.mcp.server.FastMCP') as mock_fastmcp:
            mock_registry = MagicMock()
            mock_fastmcp.return_value = mock_registry

            # Should handle middleware errors gracefully
            from akosha.mcp.auth import RateLimiter

            rate_limiter = RateLimiter(rate_limit=1, time_window=60)

            # Test rate limiting
            result = await rate_limiter.check_limit("user123")
            assert result["allowed"] is True

            # Second request should be blocked
            result = await rate_limiter.check_limit("user123")
            assert result["allowed"] is False

    def test_app_startup_error_handling(self):
        """Test app startup error handling."""
        # Test that app creation handles various scenarios
        app = create_app()

        # App should be created despite any internal issues
        assert app is not None
        assert hasattr(app, 'routes')


class TestPerformanceIntegration:
    """Test performance of integrated components."""

    @pytest.mark.asyncio
    async def test_request_validation_performance(self):
        """Test request validation performance."""
        import time

        request_data = {"text": "hello world", "limit": 10}

        start_time = time.time()
        for _ in range(100):
            validate_request(GenerateEmbeddingRequest, **request_data)
        end_time = time.time()

        # Should be fast
        assert (end_time - start_time) < 1.0

    @pytest.mark.asyncio
    async def test_tool_registration_performance(self, mock_embedding_service):
        """Test tool registration performance."""
        import time

        with patch('akosha.mcp.server.FastMCP') as mock_fastmcp:
            mock_registry = MagicMock()
            mock_fastmcp.return_value = mock_registry

            start_time = time.time()
            from akosha.mcp.tools.akosha_tools import register_embedding_tools

            register_embedding_tools(mock_registry, mock_embedding_service)
            end_time = time.time()

            # Should be fast
            assert (end_time - start_time) < 1.0
            assert mock_registry.register.call_count > 0


class TestConfigurationIntegration:
    """Test configuration integration."""

    def test_server_configuration(self):
        """Test server configuration integration."""
        app = create_app()

        # Verify server configuration
        assert app.title == "Akosha MCP Server"
        assert app.version == "0.4.0"


class TestLoadIntegration:
    """Test load handling in integration."""

    @pytest.mark.asyncio
    async def test_concurrent_requests(self):
        """Test concurrent request handling."""
        import asyncio

        async def make_request():
            request_data = {"text": "hello world", "limit": 10}
            return validate_request(GenerateEmbeddingRequest, **request_data)

        # Make multiple concurrent requests
        tasks = [make_request() for _ in range(10)]
        results = await asyncio.gather(*tasks)

        # All requests should succeed
        assert len(results) == 10
        for result in results:
            assert result["text"] == "hello world"

    @pytest.mark.asyncio
    async def test_rate_limiting_under_load(self):
        """Test rate limiting under load."""
        from akosha.mcp.auth import RateLimiter

        rate_limiter = RateLimiter(rate_limit=5, time_window=60)

        async def make_request(user_id):
            return await rate_limiter.check_limit(user_id)

        # Make many requests
        tasks = [make_request("user123") for _ in range(10)]
        results = await asyncio.gather(*tasks)

        # First 5 should be allowed, rest blocked
        allowed_count = sum(1 for r in results if r["allowed"])
        assert allowed_count == 5