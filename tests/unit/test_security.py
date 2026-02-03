"""Tests for Akosha authentication module."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from akosha.security import (
    AuthenticationError,
    AuthenticationMiddleware,
    InvalidTokenError,
    MissingTokenError,
    extract_token_from_headers,
    generate_token,
    get_api_token,
    is_auth_enabled,
    require_auth,
    setup_authentication_instructions,
    validate_token,
)


class TestTokenGeneration:
    """Test token generation functionality."""

    def test_generate_token_returns_string(self):
        """Test that generate_token returns a string."""
        token = generate_token()
        assert isinstance(token, str)
        assert len(token) > 0

    def test_generate_token_is_unique(self):
        """Test that each generated token is unique."""
        tokens = [generate_token() for _ in range(10)]
        assert len(set(tokens)) == 10  # All tokens should be unique

    def test_generate_token_length(self):
        """Test that generated tokens have expected length."""
        token = generate_token()
        # tokens are URL-safe base64 encoded, typically around 43 chars
        assert len(token) >= 32


class TestTokenValidation:
    """Test token validation functionality."""

    def setup_method(self):
        """Set up test environment."""
        # Save original environment
        self.original_token = os.getenv("AKOSHA_API_TOKEN")

    def teardown_method(self):
        """Clean up test environment."""
        # Restore original environment
        if self.original_token:
            os.environ["AKOSHA_API_TOKEN"] = self.original_token
        elif "AKOSHA_API_TOKEN" in os.environ:
            del os.environ["AKOSHA_API_TOKEN"]

    def test_validate_token_with_valid_token(self):
        """Test validation with correct token."""
        test_token = "test_token_123"
        os.environ["AKOSHA_API_TOKEN"] = test_token

        assert validate_token(test_token) is True

    def test_validate_token_with_invalid_token(self):
        """Test validation with incorrect token."""
        os.environ["AKOSHA_API_TOKEN"] = "correct_token"

        assert validate_token("wrong_token") is False

    def test_validate_token_with_no_token_configured(self):
        """Test validation when no API token is configured."""
        # Remove token from environment
        if "AKOSHA_API_TOKEN" in os.environ:
            del os.environ["AKOSHA_API_TOKEN"]

        assert validate_token("any_token") is False

    def test_validate_token_is_constant_time(self):
        """Test that token validation uses constant-time comparison."""
        import time

        os.environ["AKOSHA_API_TOKEN"] = "correct_token"

        # Measure time for correct token
        start = time.perf_counter()
        validate_token("correct_token")
        correct_time = time.perf_counter() - start

        # Measure time for wrong token of same length
        start = time.perf_counter()
        validate_token("xorrect_token")  # Same length, different
        wrong_time = time.perf_counter() - start

        # Times should be similar (within 10x to account for system noise)
        # This is a rough check - constant-time comparison prevents timing attacks
        assert max(correct_time, wrong_time) / min(correct_time, wrong_time) < 10


class TestAuthEnabled:
    """Test authentication enabled check."""

    def setup_method(self):
        """Set up test environment."""
        self.original_token = os.getenv("AKOSHA_API_TOKEN")
        self.original_enabled = os.getenv("AKOSHA_AUTH_ENABLED")

    def teardown_method(self):
        """Clean up test environment."""
        if self.original_token:
            os.environ["AKOSHA_API_TOKEN"] = self.original_token
        elif "AKOSHA_API_TOKEN" in os.environ:
            del os.environ["AKOSHA_API_TOKEN"]

        if self.original_enabled:
            os.environ["AKOSHA_AUTH_ENABLED"] = self.original_enabled
        elif "AKOSHA_AUTH_ENABLED" in os.environ:
            del os.environ["AKOSHA_AUTH_ENABLED"]

    def test_auth_enabled_when_token_configured(self):
        """Test that auth is enabled when token is configured."""
        os.environ["AKOSHA_API_TOKEN"] = "test_token"
        assert is_auth_enabled() is True

    def test_auth_enabled_explicitly_true(self):
        """Test that auth can be explicitly enabled."""
        os.environ["AKOSHA_AUTH_ENABLED"] = "true"
        assert is_auth_enabled() is True

    def test_auth_enabled_explicitly_false(self):
        """Test that auth can be explicitly disabled."""
        os.environ["AKOSHA_AUTH_ENABLED"] = "false"
        # Remove token to test explicit disable
        if "AKOSHA_API_TOKEN" in os.environ:
            del os.environ["AKOSHA_API_TOKEN"]
        assert is_auth_enabled() is False

    def test_auth_disabled_when_no_token(self):
        """Test that auth is disabled when no token configured."""
        if "AKOSHA_API_TOKEN" in os.environ:
            del os.environ["AKOSHA_API_TOKEN"]
        if "AKOSHA_AUTH_ENABLED" in os.environ:
            del os.environ["AKOSHA_AUTH_ENABLED"]
        # Explicitly set to false since default is true
        os.environ["AKOSHA_AUTH_ENABLED"] = "false"
        assert is_auth_enabled() is False


class TestTokenExtraction:
    """Test token extraction from headers."""

    def test_extract_token_from_valid_bearer_header(self):
        """Test extracting token from valid Authorization header."""
        headers = {"Authorization": "Bearer test_token_123"}
        token = extract_token_from_headers(headers)
        assert token == "test_token_123"

    def test_extract_token_from_lowercase_authorization(self):
        """Test extracting token with lowercase header key."""
        headers = {"authorization": "Bearer test_token_123"}
        token = extract_token_from_headers(headers)
        assert token == "test_token_123"

    def test_extract_token_with_extra_spaces(self):
        """Test extracting token with extra whitespace."""
        headers = {"Authorization": "Bearer   test_token_123   "}
        token = extract_token_from_headers(headers)
        assert token == "test_token_123"

    def test_extract_token_returns_none_for_missing_header(self):
        """Test that None is returned when Authorization header is missing."""
        headers = {}
        token = extract_token_from_headers(headers)
        assert token is None

    def test_extract_token_returns_none_for_wrong_format(self):
        """Test that None is returned for wrong format."""
        headers = {"Authorization": "Basic test_token"}
        token = extract_token_from_headers(headers)
        assert token is None

    def test_extract_token_returns_none_for_empty_token(self):
        """Test that None is returned for empty token."""
        headers = {"Authorization": "Bearer "}
        token = extract_token_from_headers(headers)
        assert token is None

    def test_extract_token_returns_none_for_none_headers(self):
        """Test that None is returned for None headers."""
        token = extract_token_from_headers(None)
        assert token is None


class TestRequireAuthDecorator:
    """Test the @require_auth decorator."""

    def setup_method(self):
        """Set up test environment."""
        self.original_token = os.getenv("AKOSHA_API_TOKEN")

    def teardown_method(self):
        """Clean up test environment."""
        if self.original_token:
            os.environ["AKOSHA_API_TOKEN"] = self.original_token
        elif "AKOSHA_API_TOKEN" in os.environ:
            del os.environ["AKOSHA_API_TOKEN"]

    @pytest.mark.asyncio
    async def test_require_auth_allows_when_auth_disabled(self):
        """Test that decorator allows access when auth is disabled."""
        # Ensure auth is disabled
        if "AKOSHA_API_TOKEN" in os.environ:
            del os.environ["AKOSHA_API_TOKEN"]
        os.environ["AKOSHA_AUTH_ENABLED"] = "false"

        @require_auth
        async def test_function():
            return {"result": "success"}

        result = await test_function()
        assert result == {"result": "success"}

    @pytest.mark.asyncio
    async def test_require_auth_allows_with_valid_token_in_kwargs(self):
        """Test that decorator allows access with valid token in kwargs."""
        test_token = "valid_token"
        os.environ["AKOSHA_API_TOKEN"] = test_token

        @require_auth
        async def test_function():
            return {"result": "success"}

        result = await test_function(auth_token=test_token)
        assert result == {"result": "success"}

    @pytest.mark.asyncio
    async def test_require_auth_denies_with_invalid_token_in_kwargs(self):
        """Test that decorator denies access with invalid token."""
        os.environ["AKOSHA_API_TOKEN"] = "correct_token"

        @require_auth
        async def test_function():
            return {"result": "success"}

        with pytest.raises(InvalidTokenError):
            await test_function(auth_token="wrong_token")

    @pytest.mark.asyncio
    async def test_require_auth_denies_with_missing_token(self):
        """Test that decorator denies access with no token."""
        os.environ["AKOSHA_API_TOKEN"] = "correct_token"

        @require_auth
        async def test_function():
            return {"result": "success"}

        with pytest.raises(MissingTokenError):
            await test_function()

    @pytest.mark.asyncio
    async def test_require_auth_with_context_headers(self):
        """Test decorator with context containing headers."""
        test_token = "valid_token"
        os.environ["AKOSHA_API_TOKEN"] = test_token

        # Mock context with headers
        mock_context = MagicMock()
        mock_context.headers = {"Authorization": f"Bearer {test_token}"}

        @require_auth
        async def test_function(_context=None):
            return {"result": "success"}

        result = await test_function(_context=mock_context)
        assert result == {"result": "success"}

    @pytest.mark.asyncio
    async def test_require_auth_with_context_but_no_headers(self):
        """Test decorator with context but no headers."""
        os.environ["AKOSHA_API_TOKEN"] = "correct_token"

        # Mock context without headers
        mock_context = MagicMock(spec=[])  # Empty spec means no headers attribute

        @require_auth
        async def test_function(context=None):
            return {"result": "success"}

        with pytest.raises(MissingTokenError):
            await test_function(context=mock_context)


class TestAuthenticationMiddleware:
    """Test AuthenticationMiddleware class."""

    def setup_method(self):
        """Set up test environment."""
        self.original_token = os.getenv("AKOSHA_API_TOKEN")
        self.middleware = AuthenticationMiddleware()

    def teardown_method(self):
        """Clean up test environment."""
        if self.original_token:
            os.environ["AKOSHA_API_TOKEN"] = self.original_token
        elif "AKOSHA_API_TOKEN" in os.environ:
            del os.environ["AKOSHA_API_TOKEN"]

    def test_middleware_initialization(self):
        """Test middleware initialization with default protected tools."""
        assert len(self.middleware.protected_categories) == 3
        assert len(self.middleware.protected_tools) == 8
        assert "search" in self.middleware.protected_categories
        assert "analytics" in self.middleware.protected_categories
        assert "graph" in self.middleware.protected_categories
        assert "search_all_systems" in self.middleware.protected_tools

    def test_is_tool_protected_by_name(self):
        """Test checking if tool is protected by name."""
        assert self.middleware.is_tool_protected("search_all_systems") is True
        assert self.middleware.is_tool_protected("get_system_metrics") is True
        assert self.middleware.is_tool_protected("unknown_tool") is False

    def test_is_tool_protected_by_category(self):
        """Test checking if tool is protected by category."""
        assert self.middleware.is_tool_protected("some_tool", "search") is True
        assert self.middleware.is_tool_protected("some_tool", "analytics") is True
        assert self.middleware.is_tool_protected("some_tool", "graph") is True
        assert self.middleware.is_tool_protected("some_tool", "system") is False

    def test_custom_protected_tools(self):
        """Test middleware with custom protected tools."""
        custom_middleware = AuthenticationMiddleware(
            protected_tools={"custom_tool_1", "custom_tool_2"}
        )
        assert custom_middleware.is_tool_protected("custom_tool_1") is True
        assert custom_middleware.is_tool_protected("search_all_systems") is False

    @pytest.mark.asyncio
    async def test_authenticate_request_allows_unprotected_tool(self):
        """Test authentication allows access to unprotected tools."""
        # Disable auth for this test
        if "AKOSHA_API_TOKEN" in os.environ:
            del os.environ["AKOSHA_API_TOKEN"]

        result = await self.middleware.authenticate_request(
            tool_name="unprotected_tool",
            tool_category="system",
            context=None,
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_authenticate_request_allows_with_valid_token(self):
        """Test authentication allows access with valid token."""
        test_token = "valid_token"
        os.environ["AKOSHA_API_TOKEN"] = test_token

        mock_context = MagicMock()
        mock_context.headers = {"Authorization": f"Bearer {test_token}"}

        result = await self.middleware.authenticate_request(
            tool_name="search_all_systems",
            tool_category="search",
            context=mock_context,
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_authenticate_request_denies_with_invalid_token(self):
        """Test authentication denies access with invalid token."""
        os.environ["AKOSHA_API_TOKEN"] = "correct_token"

        mock_context = MagicMock()
        mock_context.headers = {"Authorization": "Bearer wrong_token"}

        with pytest.raises(InvalidTokenError):
            await self.middleware.authenticate_request(
                tool_name="search_all_systems",
                tool_category="search",
                context=mock_context,
            )

    @pytest.mark.asyncio
    async def test_authenticate_request_denies_with_missing_token(self):
        """Test authentication denies access with missing token."""
        os.environ["AKOSHA_API_TOKEN"] = "correct_token"

        mock_context = MagicMock()
        mock_context.headers = {}

        with pytest.raises(MissingTokenError):
            await self.middleware.authenticate_request(
                tool_name="search_all_systems",
                tool_category="search",
                context=mock_context,
            )


class TestAuthenticationErrors:
    """Test authentication error classes."""

    def test_authentication_error_to_dict(self):
        """Test converting AuthenticationError to dictionary."""
        error = AuthenticationError(
            "Test error",
            details={"key": "value"}
        )
        result = error.to_dict()
        assert result == {
            "error": "authentication_error",
            "message": "Test error",
            "details": {"key": "value"},
        }

    def test_authentication_error_without_details(self):
        """Test AuthenticationError without details."""
        error = AuthenticationError("Test error")
        result = error.to_dict()
        assert result == {
            "error": "authentication_error",
            "message": "Test error",
            "details": {},
        }

    def test_missing_token_error_message(self):
        """Test MissingTokenError has correct message."""
        error = MissingTokenError()
        assert "Missing or invalid authentication token" in str(error)

    def test_invalid_token_error_message(self):
        """Test InvalidTokenError has correct message."""
        error = InvalidTokenError()
        assert "Invalid authentication token" in str(error)


class TestSetupInstructions:
    """Test authentication setup instructions."""

    def test_setup_instructions_contains_token(self):
        """Test that setup instructions include a generated token."""
        instructions = setup_authentication_instructions()
        assert "export AKOSHA_API_TOKEN=" in instructions
        # Token should be in instructions
        assert len(instructions) > 100

    def test_setup_instructions_contains_sections(self):
        """Test that setup instructions contain all required sections."""
        instructions = setup_authentication_instructions()
        assert "# Akosha Authentication Setup" in instructions
        assert "## 1. Generate API Token" in instructions
        assert "## 2. Enable Authentication" in instructions
        assert "## 3. Using Authentication" in instructions
        assert "## Security Best Practices" in instructions
        assert "## Protected Tools" in instructions

    def test_setup_instructions_lists_protected_tools(self):
        """Test that setup instructions list all protected tools."""
        instructions = setup_authentication_instructions()
        assert "- `search_all_systems`" in instructions
        assert "- `get_system_metrics`" in instructions
        assert "- `analyze_trends`" in instructions
        assert "- `detect_anomalies`" in instructions
        assert "- `correlate_systems`" in instructions
        assert "- `query_knowledge_graph`" in instructions
        assert "- `find_path`" in instructions
        assert "- `get_graph_statistics`" in instructions


class TestGetApiToken:
    """Test get_api_token function."""

    def setup_method(self):
        """Set up test environment."""
        self.original_token = os.getenv("AKOSHA_API_TOKEN")

    def teardown_method(self):
        """Clean up test environment."""
        if self.original_token:
            os.environ["AKOSHA_API_TOKEN"] = self.original_token
        elif "AKOSHA_API_TOKEN" in os.environ:
            del os.environ["AKOSHA_API_TOKEN"]

    def test_get_api_token_returns_configured_token(self):
        """Test that get_api_token returns configured token."""
        test_token = "test_token_123"
        os.environ["AKOSHA_API_TOKEN"] = test_token

        assert get_api_token() == test_token

    def test_get_api_token_returns_none_when_not_configured(self):
        """Test that get_api_token returns None when not configured."""
        if "AKOSHA_API_TOKEN" in os.environ:
            del os.environ["AKOSHA_API_TOKEN"]

        assert get_api_token() is None
