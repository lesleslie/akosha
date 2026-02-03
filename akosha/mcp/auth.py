"""MCP authentication and authorization middleware.

This module provides JWT-based authentication for Akosha's MCP server,
ensuring that all tools are protected by default with configurable authentication.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import TYPE_CHECKING, Any

import jwt

if TYPE_CHECKING:
    from collections.abc import Callable

    pass

logger = logging.getLogger(__name__)

# JWT configuration
JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_MINUTES = int(os.getenv("JWT_EXPIRATION_MINUTES", "60"))


class MCPAuthError(Exception):
    """Authentication error."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def require_auth(func: Callable) -> Callable:
    """Decorator to require authentication for MCP tools.

    This decorator validates JWT tokens from the request context and injects
    the authenticated user_id into the function kwargs.

    Args:
        func: The function to protect with authentication

    Returns:
        Wrapped function that requires valid JWT token

    Raises:
        MCPAuthError: If authentication fails or token is invalid
    """

    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        # Check if authentication is enabled via feature flag
        auth_enabled = os.getenv("AUTH_ENABLED", "true").lower() == "true"

        if not auth_enabled:
            # Authentication disabled - allow anonymous access
            logger.debug("Authentication disabled, allowing anonymous access")
            kwargs["authenticated_user_id"] = "anonymous"
            return await func(*args, **kwargs)

        # Extract token from context
        # Note: FastMCP passes context via kwargs in some versions
        # We'll check for __auth_token__ or Authorization header equivalent
        request_context = kwargs.get("__request_context__", {})
        auth_token = request_context.get("auth_token") or kwargs.get("__auth_token__")

        if not auth_token:
            logger.warning("Missing authentication token")
            raise MCPAuthError(
                "Authentication required. Please provide a valid JWT token.",
                retry_after=60.0,
            )

        # Validate token format
        if not isinstance(auth_token, str):
            logger.warning(f"Invalid token type: {type(auth_token)}")
            raise MCPAuthError("Invalid token format")

        # Check for Bearer prefix
        if auth_token.startswith("Bearer "):
            auth_token = auth_token.removeprefix("Bearer ")

        try:
            # Decode and validate JWT
            payload = jwt.decode(
                auth_token,
                _get_jwt_secret(),
                algorithms=[JWT_ALGORITHM],
                options={
                    "require": ["exp", "sub", "user_id"],
                    "verify_exp": True,
                },
            )

            # Extract user_id from payload
            user_id = payload.get("user_id")
            if not user_id:
                logger.warning("Token missing user_id claim")
                raise MCPAuthError("Invalid token: missing user_id")

            # Inject user_id into kwargs
            kwargs["authenticated_user_id"] = user_id

            logger.debug(f"Authenticated user: {user_id}")

            return await func(*args, **kwargs)

        except jwt.ExpiredSignatureError:
            logger.warning("Expired JWT token")
            raise MCPAuthError("Token has expired. Please refresh your token.") from None
        except jwt.InvalidTokenError as e:
            logger.warning(f"Invalid JWT token: {e}")
            raise MCPAuthError("Invalid token. Please provide a valid JWT token.") from None
        except Exception as e:
            logger.error(f"Authentication error: {e}")
            raise MCPAuthError(f"Authentication failed: {e!s}") from e

    return wrapper


def _get_jwt_secret() -> str:
    """Get JWT secret from environment.

    Returns:
        JWT secret key

    Raises:
        ValueError: If JWT_SECRET is not set or is a placeholder
    """
    secret = os.getenv("JWT_SECRET")

    if not secret:
        raise ValueError("JWT_SECRET environment variable required for authentication")

    # Prevent placeholder secrets in production
    environment = os.getenv("ENVIRONMENT", "development")
    if environment == "production":
        placeholder_values = ["change-this-in-production", "", "none", "null"]
        if secret.lower() in placeholder_values:
            raise ValueError(
                "JWT_SECRET must be set to a secure value in production. "
                "Generate with: openssl rand -base64 32"
            )

    return secret


def generate_jwt_token(
    user_id: str,
    expiration_minutes: int | None = None,
    additional_claims: dict[str, Any] | None = None,
) -> str:
    """Generate a JWT token for testing or development.

    Args:
        user_id: User identifier
        expiration_minutes: Token expiration in minutes (default from env)
        additional_claims: Additional claims to include in token

    Returns:
        JWT token string

    Raises:
        ValueError: If JWT_SECRET is not configured
    """
    secret = _get_jwt_secret()

    expiration = expiration_minutes or JWT_EXPIRATION_MINUTES
    payload = {
        "user_id": user_id,
        "sub": user_id,  # JWT standard subject claim
        "exp": datetime.now(UTC) + timedelta(minutes=expiration),
        "iat": datetime.now(UTC),  # Issued at
        **(additional_claims or {}),
    }

    token = jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)
    return token


def validate_auth_config() -> bool:
    """Validate authentication configuration.

    Returns:
        True if authentication is properly configured

    Raises:
        ValueError: If configuration is invalid
    """
    auth_enabled = os.getenv("AUTH_ENABLED", "true").lower() == "true"

    if auth_enabled:
        # Ensure JWT_SECRET is set
        secret = os.getenv("JWT_SECRET")
        if not secret:
            raise ValueError(
                "AUTH_ENABLED=true but JWT_SECRET is not set. "
                "Set JWT_SECRET or disable authentication with AUTH_ENABLED=false"
            )

        # Reject placeholder secrets in production FIRST (before length check)
        environment = os.getenv("ENVIRONMENT", "development")
        if environment == "production":
            placeholder_values = [
                "change-this-in-production",
                "change-me",
                "placeholder",
                "",
                "none",
                "null",
                "example",
                "test-secret",
                "jwt-secret-placeholder",
            ]
            if secret.lower() in placeholder_values:
                raise ValueError(
                    "JWT_SECRET is using a placeholder value. "
                    "Generate secure secrets with: python -m akosha.scripts.generate_secrets"
                )

        # Validate secret length (minimum 32 characters for HS256)
        if len(secret) < 32:
            raise ValueError(
                f"JWT_SECRET too short ({len(secret)} chars). "
                "Must be at least 32 characters for HS256 algorithm. "
                "Generate with: python -m akosha.scripts.generate_secrets"
            )

        logger.info("✅ Authentication configuration validated")
        return True
    else:
        logger.warning("⚠️  Authentication disabled (AUTH_ENABLED=false)")
        return True


__all__ = [
    "MCPAuthError",
    "generate_jwt_token",
    "require_auth",
    "validate_auth_config",
]
