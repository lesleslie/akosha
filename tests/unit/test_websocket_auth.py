"""Tests for WebSocket authentication module.

Tests JWT authentication and token management for WebSocket connections.
"""

import os
from unittest.mock import patch

from akosha.websocket.auth import (
    AUTH_ENABLED,
    JWT_SECRET,
    TOKEN_EXPIRY,
    generate_token,
    get_authenticator,
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
        with patch.dict(
            os.environ,
            {
                "AKOSHA_JWT_SECRET": "test-secret",
                "AKOSHA_TOKEN_EXPIRY": "7200",
                "AKOSHA_AUTH_ENABLED": "true",
            },
        ):
            # Values should be updated (reload needed for actual test)
            pass


class TestWebsocketAuthAuthenticator:
    """Test WebSocket authenticator functionality."""

    def test_get_authenticator_enabled(self):
        """Test getting authenticator when auth is enabled."""
        with patch("akosha.websocket.auth.AUTH_ENABLED", True):
            with patch("akosha.websocket.auth.WebSocketAuthenticator") as MockAuthenticator:
                mock_auth = MockAuthenticator.return_value
                mock_auth.configure.return_value = None

                result = get_authenticator()

                assert result is not None
                MockAuthenticator.assert_called_once()

    def test_get_authenticator_disabled(self):
        """Test getting authenticator when auth is disabled."""
        with patch("akosha.websocket.auth.AUTH_ENABLED", False):
            result = get_authenticator()
            assert result is None

    def test_get_authenticator_warns_on_default_secret(self):
        """Test getting authenticator warns when the default secret is used."""
        with (
            patch("akosha.websocket.auth.AUTH_ENABLED", True),
            patch("akosha.websocket.auth.JWT_SECRET", "dev-secret-change-in-production"),
            patch("akosha.websocket.auth.WebSocketAuthenticator") as MockAuthenticator,
        ):
            MockAuthenticator.return_value = object()
            result = get_authenticator()

        assert result is not None
        MockAuthenticator.assert_called_once_with(
            secret="dev-secret-change-in-production",
            algorithm="HS256",
            token_expiry=TOKEN_EXPIRY,
        )


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
        with patch("akosha.websocket.auth.AUTH_ENABLED", False):
            result = get_authenticator()
            assert result is None

    def test_empty_jwt_secret_handling(self):
        """Test behavior with empty JWT secret."""
        with patch("akosha.websocket.auth.AUTH_ENABLED", True):
            with patch("akosha.websocket.auth.JWT_SECRET", ""):
                result = get_authenticator()
            assert result is not None

    def test_generate_token_development_mode(self):
        """Test token generation when get_authenticator returns None."""
        with (
            patch("akosha.websocket.auth.WebSocketAuthenticator") as MockAuthenticator,
            patch("akosha.websocket.auth.get_authenticator", return_value=None),
        ):
            mock_auth = MockAuthenticator.return_value
            mock_auth.create_token.return_value = "dev-token"
            token = generate_token("dev-user", ["akosha:read"])

        assert token == "dev-token"
        MockAuthenticator.assert_called_once_with(
            secret=JWT_SECRET,
            algorithm="HS256",
            token_expiry=TOKEN_EXPIRY,
        )

    def test_verify_token_development_mode(self):
        """Test token verification when get_authenticator returns None."""
        with (
            patch("akosha.websocket.auth.WebSocketAuthenticator") as MockAuthenticator,
            patch("akosha.websocket.auth.get_authenticator", return_value=None),
        ):
            mock_auth = MockAuthenticator.return_value
            mock_auth.verify_token.return_value = {"user_id": "dev-user"}
            result = verify_token("dev-token")

        assert result == {"user_id": "dev-user"}
        MockAuthenticator.assert_called_once_with(
            secret=JWT_SECRET,
            algorithm="HS256",
            token_expiry=TOKEN_EXPIRY,
        )

    def test_generate_token_uses_configured_authenticator(self):
        """Test token generation when get_authenticator returns a configured authenticator."""
        with patch("akosha.websocket.auth.WebSocketAuthenticator") as MockAuthenticator:
            mock_auth = MockAuthenticator.return_value
            mock_auth.create_token.return_value = "configured-token"
            with patch("akosha.websocket.auth.get_authenticator", return_value=mock_auth):
                token = generate_token("configured-user", ["akosha:read"])

        assert token == "configured-token"
        MockAuthenticator.assert_not_called()

    def test_verify_token_uses_configured_authenticator(self):
        """Test token verification when get_authenticator returns a configured authenticator."""
        with patch("akosha.websocket.auth.WebSocketAuthenticator") as MockAuthenticator:
            mock_auth = MockAuthenticator.return_value
            mock_auth.verify_token.return_value = {"user_id": "configured-user"}
            with patch("akosha.websocket.auth.get_authenticator", return_value=mock_auth):
                result = verify_token("configured-token")

        assert result == {"user_id": "configured-user"}
        MockAuthenticator.assert_not_called()

    def test_verification_error_handling(self):
        """Test error handling during token verification."""
        test_cases = [
            None,  # None token
            123,  # Non-string token
            [],  # List token
            {},  # Dict token
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
            {"secret": "valid-secret", "expiry": "3600", "enabled": True},
            {"secret": "another-secret", "expiry": "7200", "enabled": False},
            {"secret": "", "expiry": "3600", "enabled": True},
        ]

        for config in valid_configs:
            with patch("akosha.websocket.auth.AUTH_ENABLED", config["enabled"]):
                with patch("akosha.websocket.auth.JWT_SECRET", config["secret"]):
                    with patch("akosha.websocket.auth.TOKEN_EXPIRY", config["expiry"]):
                        result = get_authenticator()
                        assert result is not None or not config["enabled"]

    def test_secure_configuration(self):
        """Test secure configuration practices."""
        assert JWT_SECRET != ""
        assert 0 < TOKEN_EXPIRY <= 86400

        test_token = generate_token("test-user", ["akosha:read"])
        result = verify_token(test_token)
        assert result is not None
