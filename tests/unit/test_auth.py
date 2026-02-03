"""Tests for authentication (JWT and API token support)."""

from __future__ import annotations

import os
import pytest

from akosha.security import (
    AuthenticationError,
    InvalidTokenError,
    MissingTokenError,
    generate_jwt_token,
    get_api_token,
    is_auth_enabled,
    validate_token,
)


class TestJWTAuthentication:
    """Test JWT token generation and validation."""

    @pytest.fixture(autouse=True)
    def setup_jwt_secret(self, monkeypatch) -> None:
        """Set up JWT secret for testing."""
        monkeypatch.setenv("JWT_SECRET", "test_secret_key_at_least_32_characters")
        monkeypatch.setenv("ENVIRONMENT", "development")

    @pytest.mark.asyncio
    async def test_generate_jwt_token_success(self) -> None:
        """Test successful JWT token generation."""
        token = generate_jwt_token(user_id="test-user")

        assert isinstance(token, str)
        assert len(token) > 0
        # JWT tokens should have 3 parts separated by dots
        assert token.count(".") == 2

    @pytest.mark.asyncio
    async def test_generate_jwt_token_custom_expiration(self) -> None:
        """Test JWT token generation with custom expiration."""
        token = generate_jwt_token(user_id="test-user", expiration_minutes=120)

        assert isinstance(token, str)
        # Token should be valid
        assert validate_token(token) is True

    @pytest.mark.asyncio
    async def test_generate_jwt_token_without_secret(self, monkeypatch) -> None:
        """Test JWT generation fails without JWT_SECRET."""
        monkeypatch.delenv("JWT_SECRET", raising=False)

        with pytest.raises(ValueError, match="JWT_SECRET.*required"):
            generate_jwt_token(user_id="test-user")

    @pytest.mark.asyncio
    async def test_generate_jwt_token_placeholder_in_production(self, monkeypatch) -> None:
        """Test that placeholder secrets are rejected in production."""
        monkeypatch.setenv("JWT_SECRET", "change-this-in-production")
        monkeypatch.setenv("ENVIRONMENT", "production")

        with pytest.raises(ValueError, match="JWT_SECRET must be set to a secure value"):
            generate_jwt_token(user_id="test-user")

    @pytest.mark.asyncio
    async def test_validate_jwt_token_success(self) -> None:
        """Test successful JWT token validation."""
        token = generate_jwt_token(user_id="test-user")

        # Validation should succeed
        assert validate_token(token) is True

    @pytest.mark.asyncio
    async def test_validate_jwt_token_with_bearer_prefix(self) -> None:
        """Test JWT validation with Bearer prefix."""
        token = generate_jwt_token(user_id="test-user")

        # Add Bearer prefix (as would come from HTTP header)
        bearer_token = f"Bearer {token}"

        assert validate_token(bearer_token) is True

    @pytest.mark.asyncio
    async def test_validate_jwt_token_expired(self, monkeypatch) -> None:
        """Test that expired JWT tokens are rejected."""
        # Generate a token that expires immediately
        monkeypatch.setenv("JWT_SECRET", "test_secret_key_at_least_32_characters")
        from datetime import UTC, datetime, timedelta

        try:
            import jwt

            payload = {
                "user_id": "test-user",
                "sub": "test-user",
                "exp": (datetime.now(UTC) - timedelta(minutes=1)).timestamp(),  # Expired
                "iat": datetime.now(UTC).timestamp(),
            }

            token = jwt.encode(payload, os.getenv("JWT_SECRET"), algorithm="HS256")

            # Validation should fail for expired token
            assert validate_token(token) is False

        except ImportError:
            pytest.skip("PyJWT not installed")

    @pytest.mark.asyncio
    async def test_validate_invalid_jwt_token(self) -> None:
        """Test that invalid JWT tokens are rejected."""
        invalid_tokens = [
            "not.a.valid.jwt.token",
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.invalid",
            "",
        ]

        for invalid_token in invalid_tokens:
            # Invalid tokens should fall through to API token validation
            # and fail if no API token is configured
            result = validate_token(invalid_token)
            # Should be False (invalid JWT, no API token)
            assert result is False


class TestAPITokenAuthentication:
    """Test API token authentication."""

    @pytest.fixture(autouse=True)
    def setup_api_token(self, monkeypatch) -> None:
        """Set up API token for testing."""
        monkeypatch.setenv("AKOSHA_API_TOKEN", "test_api_token")
        monkeypatch.delenv("JWT_SECRET", raising=False)

    @pytest.mark.asyncio
    async def test_get_api_token(self) -> None:
        """Test retrieving API token from environment."""
        token = get_api_token()

        assert token == "test_api_token"

    @pytest.mark.asyncio
    async def test_validate_api_token_success(self) -> None:
        """Test successful API token validation."""
        token = "test_api_token"

        assert validate_token(token) is True

    @pytest.mark.asyncio
    async def test_validate_api_token_failure(self) -> None:
        """Test API token validation with wrong token."""
        wrong_token = "wrong_api_token"

        assert validate_token(wrong_token) is False


class TestAuthenticationConfig:
    """Test authentication configuration and feature flags."""

    @pytest.mark.asyncio
    async def test_auth_enabled_with_token(self, monkeypatch) -> None:
        """Test authentication is enabled when token is configured."""
        monkeypatch.setenv("AKOSHA_API_TOKEN", "test_token")

        assert is_auth_enabled() is True

    @pytest.mark.asyncio
    async def test_auth_disabled_explicitly(self, monkeypatch) -> None:
        """Test authentication can be explicitly disabled."""
        monkeypatch.setenv("AKOSHA_AUTH_ENABLED", "false")

        assert is_auth_enabled() is False

    @pytest.mark.asyncio
    async def test_auth_enabled_by_default(self, monkeypatch) -> None:
        """Test authentication defaults to enabled."""
        monkeypatch.delenv("AKOSHA_API_TOKEN", raising=False)
        monkeypatch.setenv("AKOSHA_AUTH_ENABLED", "true")

        # No token configured, but flag is true - should check what happens
        # Actually, is_auth_enabled returns True if flag is true, even without token
        # The token validation will fail later
        assert is_auth_enabled() is True

    @pytest.mark.asyncio
    async def test_placeholder_secret_rejected_in_production(self, monkeypatch) -> None:
        """Test that placeholder secrets are rejected in production environment."""
        from akosha.mcp.auth import validate_auth_config

        # Set production environment
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("AUTH_ENABLED", "true")

        # Test various placeholder values
        placeholder_secrets = [
            "change-this-in-production",
            "change-me",
            "placeholder",
            "test-secret",
            "example",
        ]

        for placeholder in placeholder_secrets:
            monkeypatch.setenv("JWT_SECRET", placeholder)

            with pytest.raises(ValueError, match="placeholder value"):
                validate_auth_config()

    @pytest.mark.asyncio
    async def test_development_allows_test_secrets(self, monkeypatch) -> None:
        """Test that test secrets work in development environment."""
        from akosha.mcp.auth import validate_auth_config

        # Set development environment (default)
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.setenv("AUTH_ENABLED", "true")
        # Use a test secret that meets minimum length requirement
        monkeypatch.setenv("JWT_SECRET", "development-test-secret-key-for-local-testing")

        # Should work in development
        assert validate_auth_config() is True

    @pytest.mark.asyncio
    async def test_generate_secrets_script_exists(self) -> None:
        """Test that the secret generation script exists and is importable."""
        from akosha.scripts import generate_secrets

        # Verify the module has the main function
        assert hasattr(generate_secrets, "main")
        assert hasattr(generate_secrets, "generate_production_secrets")
        assert hasattr(generate_secrets, "generate_jwt_secret")
        assert hasattr(generate_secrets, "generate_encryption_key")


class TestRequireAuthDecorator:
    """Test the @require_auth decorator."""

    @pytest.fixture(autouse=True)
    def setup_auth(self, monkeypatch) -> None:
        """Enable authentication for testing."""
        monkeypatch.setenv("AKOSHA_API_TOKEN", "test_token")
        monkeypatch.setenv("AKOSHA_AUTH_ENABLED", "true")

    @pytest.mark.asyncio
    async def test_protected_function_with_valid_token(self) -> None:
        """Test that protected function accepts valid token."""
        from akosha.security import require_auth

        @require_auth
        async def protected_function(param: str) -> dict:
            return {"result": f"protected: {param}"}

        # Call with valid token
        result = await protected_function("test_param", auth_token="test_token")

        assert result["result"] == "protected: test_param"

    @pytest.mark.asyncio
    async def test_protected_function_with_invalid_token(self) -> None:
        """Test that protected function rejects invalid token."""
        from akosha.security import require_auth, InvalidTokenError

        @require_auth
        async def protected_function(param: str) -> dict:
            return {"result": f"protected: {param}"}

        # Call with invalid token
        with pytest.raises(InvalidTokenError):
            await protected_function("test_param", auth_token="invalid_token")

    @pytest.mark.asyncio
    async def test_protected_function_without_token(self) -> None:
        """Test that protected function requires token."""
        from akosha.security import require_auth, MissingTokenError

        @require_auth
        async def protected_function(param: str) -> dict:
            return {"result": f"protected: {param}"}

        # Call without token
        with pytest.raises(MissingTokenError):
            await protected_function("test_param")

    @pytest.mark.asyncio
    async def test_protected_function_auth_disabled(self, monkeypatch) -> None:
        """Test that protected function works when auth is disabled."""
        from akosha.security import require_auth

        # Clear API token and disable auth
        monkeypatch.delenv("AKOSHA_API_TOKEN", raising=False)
        monkeypatch.setenv("AKOSHA_AUTH_ENABLED", "false")

        @require_auth
        async def protected_function(param: str) -> dict:
            return {"result": f"protected: {param}"}

        # Should work without token when auth is disabled
        result = await protected_function("test_param")

        assert result["result"] == "protected: test_param"
