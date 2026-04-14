"""Tests for MCP authentication module.

Tests JWT authentication, rate limiting, and middleware functionality.
"""

import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch, Mock
from datetime import datetime, timedelta, UTC
from typing import Any

from akosha.mcp.auth import generate_jwt_token as generate_mcp_jwt_token, require_auth
from akosha.security import generate_jwt_token, AuthenticationError, InvalidTokenError as TokenError


class TestJWTAuthHandler:
    """Test JWT authentication handler."""

    @pytest.fixture
    def auth_handler(self):
        """Create JWT auth handler."""
        return JWTAuthHandler()

    def test_auth_handler_initialization(self, auth_handler):
        """Test JWT auth handler initialization."""
        assert auth_handler is not None
        assert hasattr(auth_handler, 'secret_key')
        assert hasattr(auth_handler, 'algorithm')
        assert hasattr(auth_handler, 'expire_minutes')

    def test_generate_token_success(self, auth_handler):
        """Test successful token generation."""
        user_id = "user123"
        token = auth_handler.generate_token(user_id)

        assert isinstance(token, str)
        assert len(token) > 0

    def test_generate_token_empty_user_id(self, auth_handler):
        """Test token generation with empty user ID."""
        with pytest.raises(ValueError):
            auth_handler.generate_token("")

    def test_validate_token_success(self, auth_handler):
        """Test successful token validation."""
        user_id = "user123"
        token = auth_handler.generate_token(user_id)

        result = auth_handler.validate_token(token)

        assert result is not None
        assert result["user_id"] == user_id
        assert "exp" in result

    def test_validate_token_invalid(self, auth_handler):
        """Test validation with invalid token."""
        with pytest.raises(TokenError):
            auth_handler.validate_token("invalid.token.here")

    def test_validate_token_expired(self, auth_handler):
        """Test validation with expired token."""
        # Generate token with short expiration
        auth_handler.expire_minutes = 0
        token = auth_handler.generate_token("user123")

        time.sleep(0.1)  # Small delay

        with pytest.raises(TokenError):
            auth_handler.validate_token(token)

    def test_token_expiry_time(self, auth_handler):
        """Test token expiration time calculation."""
        before_generate = time.time()
        token = auth_handler.generate_token("user123")
        after_generate = time.time()

        # Decode token to check expiration
        decoded = auth_handler.decode_token(token)
        expiry_time = decoded["exp"]

        # Expiry should be in the future
        assert expiry_time > after_generate
        # Expiry should be within configured minutes
        max_expiry = before_generate + (auth_handler.expire_minutes * 60)
        assert expiry_time <= max_expiry


class TestRateLimitMiddleware:
    """Test rate limiting middleware."""

    @pytest.fixture
    def mock_app(self):
        """Create mock application."""
        return MagicMock()

    @pytest.fixture
    def rate_limiter(self):
        """Create rate limiter."""
        return RateLimiter(rate_limit=10, time_window=60)

    def test_rate_limiter_initialization(self):
        """Test rate limiter initialization."""
        limiter = RateLimiter(rate_limit=5, time_window=30)

        assert limiter.rate_limit == 5
        assert limiter.time_window == 30
        assert limiter.requests == {}

    @pytest.mark.asyncio
    async def test_rate_limiter_allow_request(self, rate_limiter):
        """Test rate limiter allows valid requests."""
        user_id = "user123"
        result = await rate_limiter.check_limit(user_id)

        assert result["allowed"] is True
        assert result["remaining"] == 9  # 10 - 1

    @pytest.mark.asyncio
    async def test_rate_limiter_block_over_limit(self, rate_limiter):
        """Test rate limiter blocks requests over limit."""
        user_id = "user123"

        # Make 10 requests
        for _ in range(10):
            await rate_limiter.check_limit(user_id)

        # 11th request should be blocked
        result = await rate_limiter.check_limit(user_id)

        assert result["allowed"] is False
        assert result["remaining"] == 0

    @pytest.mark.asyncio
    async def test_rate_limiter_window_reset(self, rate_limiter):
        """Test rate limiter resets after time window."""
        user_id = "user123"

        # Make 10 requests
        for _ in range(10):
            await rate_limiter.check_limit(user_id)

        # Manually clear requests to simulate window reset
        rate_limiter.requests[user_id] = []

        # Next request should be allowed
        result = await rate_limiter.check_limit(user_id)

        assert result["allowed"] is True
        assert result["remaining"] == 9

    def test_rate_limit_middleware_initialization(self, mock_app, rate_limiter):
        """Test rate limit middleware initialization."""
        middleware = RateLimitMiddleware(mock_app, rate_limiter)

        assert middleware.app == mock_app
        assert middleware.rate_limiter == rate_limiter

    @pytest.mark.asyncio
    async def test_middleware_apply(self, mock_app, rate_limiter):
        """Test middleware application."""
        middleware = RateLimitMiddleware(mock_app, rate_limiter)

        # Mock a request
        mock_request = MagicMock()
        mock_request.headers = {}

        result = await middleware.apply(mock_request, "user123")

        assert result is not None

    @pytest.mark.asyncio
    async def test_middleware_block_over_limit(self, mock_app, rate_limiter):
        """Test middleware blocks over-limit requests."""
        middleware = RateLimitMiddleware(mock_app, rate_limiter)

        # Mock request
        mock_request = MagicMock()
        mock_request.headers = {}

        # Make 10 requests first
        for _ in range(10):
            await middleware.apply(mock_request, "user123")

        # 11th should raise exception
        with pytest.raises(Exception):  # Should raise rate limit exceeded
            await middleware.apply(mock_request, "user123")


class TestAuthenticationIntegration:
    """Test authentication integration with rate limiting."""

    @pytest.fixture
    def auth_handler(self):
        """Create auth handler."""
        return JWTAuthHandler()

    @pytest.fixture
    def rate_limiter(self):
        """Create rate limiter."""
        return RateLimiter(rate_limit=5, time_window=60)

    @pytest.mark.asyncio
    async def test_auth_and_rate_limit_integration(self, auth_handler, rate_limiter):
        """Test integration of authentication and rate limiting."""
        user_id = "user123"

        # Generate token
        token = auth_handler.generate_token(user_id)

        # Validate token
        decoded = auth_handler.validate_token(token)
        assert decoded["user_id"] == user_id

        # Apply rate limiting
        result = await rate_limiter.check_limit(user_id)
        assert result["allowed"] is True

    @pytest.mark.asyncio
    async def test_rate_limit_per_user(self, auth_handler, rate_limiter):
        """Test rate limiting per user."""
        user1 = "user1"
        user2 = "user2"

        # User1 makes requests
        for _ in range(5):
            await rate_limiter.check_limit(user1)

        # User2 should still be able to make requests
        result = await rate_limiter.check_limit(user2)
        assert result["allowed"] is True

        # User1 should be blocked
        result = await rate_limiter.check_limit(user1)
        assert result["allowed"] is False


class TestErrorHandling:
    """Test error handling in authentication and rate limiting."""

    @pytest.fixture
    def auth_handler(self):
        """Create auth handler."""
        return JWTAuthHandler()

    def test_auth_error_handling(self, auth_handler):
        """Test authentication error handling."""
        # Empty token
        with pytest.raises(TokenError):
            auth_handler.validate_token("")

        # Invalid token format
        with pytest.raises(TokenError):
            auth_handler.validate_token("not.a.jwt.token")

    @pytest.mark.asyncio
    async def test_rate_limit_error_handling(self, rate_limiter):
        """Test rate limit error handling."""
        # Non-string user ID
        with pytest.raises(ValueError):
            await rate_limiter.check_limit(None)

        # Empty user ID
        with pytest.raises(ValueError):
            await rate_limiter.check_limit("")

    @pytest.mark.asyncio
    async def test_concurrent_requests(self, rate_limiter):
        """Test rate limiting with concurrent requests."""
        import asyncio

        async def make_request(user_id):
            return await rate_limiter.check_limit(user_id)

        user_id = "user123"
        tasks = [make_request(user_id) for _ in range(10)]

        results = await asyncio.gather(*tasks)

        # First 10 should be allowed
        for i, result in enumerate(results):
            if i < 5:  # Rate limit is 5
                assert result["allowed"] is True
            else:
                assert result["allowed"] is False


class TestConfiguration:
    """Test configuration handling."""

    def test_auth_handler_configuration(self):
        """Test auth handler configuration."""
        handler = JWTAuthHandler(
            secret_key="test-secret",
            algorithm="HS256",
            expire_minutes=30
        )

        assert handler.secret_key == "test-secret"
        assert handler.algorithm == "HS256"
        assert handler.expire_minutes == 30

    def test_rate_limiter_configuration(self):
        """Test rate limiter configuration."""
        limiter = RateLimiter(rate_limit=100, time_window=300)

        assert limiter.rate_limit == 100
        assert limiter.time_window == 300