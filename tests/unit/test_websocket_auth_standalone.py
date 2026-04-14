"""Standalone tests for WebSocket authentication module.

Tests JWT authentication without importing the full akosha package.
"""

import pytest
import os
import sys
from unittest.mock import patch, MagicMock

# Add current directory to path
sys.path.insert(0, '.')

# Import directly from the module
from akosha.websocket.auth import (
    JWT_SECRET,
    TOKEN_EXPIRY,
    AUTH_ENABLED,
    get_authenticator,
    generate_token,
    verify_token,
)


class TestWebsocketAuthStandalone:
    """Test WebSocket authentication functionality."""

    def test_jwt_secret_constant(self):
        """Test JWT secret constant."""
        assert isinstance(JWT_SECRET, str)
        assert len(JWT_SECRET) > 0

    def test_token_expiry_constant(self):
        """Test token expiry constant."""
        assert isinstance(TOKEN_EXPIRY, int)
        assert TOKEN_EXPIRY > 0

    def test_auth_enabled_constant(self):
        """Test auth enabled constant."""
        assert isinstance(AUTH_ENABLED, bool)

    @patch.dict(os.environ, {'AKOSHA_AUTH_ENABLED': 'true'})
    def test_get_authenticator_enabled(self):
        """Test getting authenticator when auth is enabled."""
        with patch('akosha.websocket.auth.WebSocketAuthenticator') as MockAuthenticator:
            mock_auth = MockAuthenticator.return_value
            mock_auth.configure.return_value = None

            result = get_authenticator()

            assert result is not None
            MockAuthenticator.assert_called_once()

    @patch.dict(os.environ, {'AKOSHA_AUTH_ENABLED': 'false'})
    def test_get_authenticator_disabled(self):
        """Test getting authenticator when auth is disabled."""
        result = get_authenticator()
        assert result is None

    def test_generate_token_basic(self):
        """Test basic token generation."""
        user_id = "test-user"
        permissions = ["akosha:read", "akosha:write"]

        token = generate_token(user_id, permissions)

        assert isinstance(token, str)
        assert len(token) > 0

    def test_generate_token_default_permissions(self):
        """Test token generation with default permissions."""
        user_id = "test-user"
        token = generate_token(user_id)
        assert isinstance(token, str)
        assert len(token) > 0

    def test_verify_valid_token(self):
        """Test verification of valid token."""
        user_id = "test-user"
        permissions = ["akosha:read"]

        token = generate_token(user_id, permissions)
        result = verify_token(token)

        assert result is not None
        assert result.get("user_id") == user_id
        assert "permissions" in result

    def test_verify_invalid_token(self):
        """Test verification of invalid token."""
        invalid_token = "invalid.token.string"
        result = verify_token(invalid_token)
        assert result is None

    def test_verify_token_different_users(self):
        """Test verification of tokens for different users."""
        users_and_permissions = [
            ("user1", ["akosha:read"]),
            ("user2", ["akosha:write"]),
            ("user3", ["akosha:read", "akosha:write"]),
        ]

        for user_id, permissions in users_and_permissions:
            token = generate_token(user_id, permissions)
            result = verify_token(token)
            assert result is not None
            assert result.get("user_id") == user_id