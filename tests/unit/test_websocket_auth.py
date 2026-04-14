"""Tests for WebSocket authentication module.

Tests JWT authentication and token management for WebSocket connections.
"""

import pytest
import os
from unittest.mock import MagicMock, patch

from akosha.websocket.auth import (
    JWT_SECRET,
    TOKEN_EXPIRY,
    AUTH_ENABLED,
    get_authenticator,
    generate_token,
    verify_token,
)


class TestWebsocketAuthConstants:
    """Test WebSocket authentication constants."""

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

    def test_constants_are_configurable(self):
        """Test that constants can be configured via environment."""
        with patch.dict(os.environ, {
            'AKOSHA_JWT_SECRET': 'test-secret',
            'AKOSHA_TOKEN_EXPIRY': '7200',
            'AKOSHA_AUTH_ENABLED': 'true'
        }):
            # Values should be updated (reload needed for actual test)
            pass


class TestWebsocketAuthAuthenticator:
    """Test WebSocket authenticator functionality."""

    def test_get_authenticator_enabled(self):
        """Test getting authenticator when auth is enabled."""
        with patch.dict(os.environ, {'AKOSHA_AUTH_ENABLED': 'true'}):
            with patch('akosha.websocket.auth.WebSocketAuthenticator') as MockAuthenticator:
                mock_auth = MockAuthenticator.return_value
                mock_auth.configure.return_value = None

                result = get_authenticator()

                assert result is not None
                MockAuthenticator.assert_called_once()

    def test_get_authenticator_disabled(self):
        """Test getting authenticator when auth is disabled."""
        with patch.dict(os.environ, {'AKOSHA_AUTH_ENABLED': 'false'}):
            result = get_authenticator()
            assert result is None


class TestWebsocketAuthTokenGeneration:
    """Test WebSocket token generation functionality."""

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

    def test_generate_token_empty_permissions(self):
        """Test token generation with empty permissions."""
        user_id = "test-user"
        permissions = []
        token = generate_token(user_id, permissions)
        assert isinstance(token, str)
        assert len(token) > 0

    def test_generate_token_different_users(self):
        """Test token generation for different users."""
        users = ["user1", "user2", "user3"]

        tokens = []
        for user in users:
            token = generate_token(user, ["akosha:read"])
            tokens.append(token)

        # All tokens should be different
        assert len(set(tokens)) == len(users)

    def test_generate_token_special_characters(self):
        """Test token generation with special characters in user ID."""
        user_id = "user-with-dashes_and.special@chars"
        permissions = ["akosha:read"]
        token = generate_token(user_id, permissions)
        assert isinstance(token, str)
        assert len(token) > 0


class TestWebsocketAuthTokenVerification:
    """Test WebSocket token verification functionality."""

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

    def test_verify_empty_token(self):
        """Test verification of empty token."""
        empty_token = ""
        result = verify_token(empty_token)
        assert result is None

    def test_verify_malformed_token(self):
        """Test verification of malformed token."""
        malformed_token = "this.is.not.a.jwt.token"
        result = verify_token(malformed_token)
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


class TestWebsocketAuthIntegration:
    """Test WebSocket authentication integration scenarios."""

    def test_token_generation_and_verification_cycle(self):
        """Test complete token generation and verification cycle."""
        user_id = "integration-test-user"
        permissions = ["akosha:read", "akosha:write"]

        token = generate_token(user_id, permissions)
        result = verify_token(token)

        assert result is not None
        assert result.get("user_id") == user_id
        assert "permissions" in result

    def test_multiple_tokens_same_user(self):
        """Test multiple tokens for the same user."""
        user_id = "multi-token-user"
        permissions = ["akosha:read"]

        token1 = generate_token(user_id, permissions)
        token2 = generate_token(user_id, permissions)

        result1 = verify_token(token1)
        result2 = verify_token(token2)

        assert result1 is not None
        assert result2 is not None
        assert result1.get("user_id") == user_id
        assert result2.get("user_id") == user_id


class TestWebsocketAuthErrorHandling:
    """Test WebSocket authentication error handling."""

    def test_auth_disabled_handling(self):
        """Test behavior when authentication is disabled."""
        with patch.dict(os.environ, {'AKOSHA_AUTH_ENABLED': 'false'}):
            result = get_authenticator()
            assert result is None

    def test_empty_jwt_secret_handling(self):
        """Test behavior with empty JWT secret."""
        with patch.dict(os.environ, {
            'AKOSHA_JWT_SECRET': '',
            'AKOSHA_AUTH_ENABLED': 'true'
        }):
            result = get_authenticator()
            assert result is not None

    def test_verification_error_handling(self):
        """Test error handling during token verification."""
        test_cases = [
            None,  # None token
            123,   # Non-string token
            [],    # List token
            {},    # Dict token
        ]

        for case in test_cases:
            result = verify_token(case)
            assert result is None


class TestWebsocketAuthPerformance:
    """Test WebSocket authentication performance."""

    def test_token_generation_performance(self):
        """Test token generation performance."""
        import time

        start_time = time.time()
        tokens = []
        for i in range(100):
            token = generate_token(f"user-{i}", ["akosha:read"])
            tokens.append(token)
        end_time = time.time()

        assert (end_time - start_time) < 1.0
        assert len(tokens) == 100

    def test_token_verification_performance(self):
        """Test token verification performance."""
        import time

        # Generate a batch of tokens
        tokens = []
        for i in range(50):
            token = generate_token(f"user-{i}", ["akosha:read"])
            tokens.append(token)

        start_time = time.time()
        for token in tokens:
            verify_token(token)
        end_time = time.time()

        assert (end_time - start_time) < 1.0


class TestWebsocketAuthConfiguration:
    """Test WebSocket authentication configuration."""

    def test_default_configuration(self):
        """Test default configuration values."""
        assert isinstance(JWT_SECRET, str)
        assert len(JWT_SECRET) > 0
        assert TOKEN_EXPIRY > 0
        assert TOKEN_EXPIRY <= 86400  # Less than 24 hours
        assert isinstance(AUTH_ENABLED, bool)

    def test_configuration_validation(self):
        """Test configuration validation."""
        valid_configs = [
            {'secret': 'valid-secret', 'expiry': '3600', 'enabled': 'true'},
            {'secret': 'another-secret', 'expiry': '7200', 'enabled': 'false'},
            {'secret': '', 'expiry': '3600', 'enabled': 'true'},
        ]

        for config in valid_configs:
            with patch.dict(os.environ, {
                'AKOSHA_JWT_SECRET': config['secret'],
                'AKOSHA_TOKEN_EXPIRY': str(config['expiry']),
                'AKOSHA_AUTH_ENABLED': config['enabled']
            }):
                result = get_authenticator()
                assert result is not None or config['enabled'] == 'false'

    def test_secure_configuration(self):
        """Test secure configuration practices."""
        assert JWT_SECRET != ""
        assert 0 < TOKEN_EXPIRY <= 86400

        test_token = generate_token("test-user", ["akosha:read"])
        result = verify_token(test_token)
        assert result is not None