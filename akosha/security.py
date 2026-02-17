"""Akosha authentication and authorization module.

This module provides Bearer token authentication for protecting aggregation
endpoints in the Akosha MCP server.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

__all__ = [
    "AuthenticationError",
    "AuthenticationMiddleware",
    "InvalidTokenError",
    "MissingTokenError",
    "extract_token_from_headers",
    "generate_jwt_token",
    "get_api_token",
    "is_auth_enabled",
    "require_auth",
    "validate_token",
]


class AuthenticationError(Exception):
    """Base exception for authentication errors."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        """Initialize authentication error.

        Args:
            message: Error message
            details: Additional error details
        """
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        """Convert error to dictionary.

        Returns:
            Error dictionary with message and details
        """
        return {
            "error": "authentication_error",
            "message": self.message,
            "details": self.details,
        }


class MissingTokenError(AuthenticationError):
    """Raised when authentication token is missing."""

    def __init__(self, details: dict[str, Any] | None = None):
        """Initialize missing token error.

        Args:
            details: Additional error details
        """
        super().__init__(
            "Missing or invalid authentication token. "
            "Provide Authorization header with Bearer token.",
            details,
        )


class InvalidTokenError(AuthenticationError):
    """Raised when authentication token is invalid."""

    def __init__(self, details: dict[str, Any] | None = None):
        """Initialize invalid token error.

        Args:
            details: Additional error details
        """
        super().__init__(
            "Invalid authentication token. Access denied.",
            details,
        )


def get_api_token() -> str | None:
    """Get API token from environment variable.

    Returns:
        API token if configured, None otherwise
    """
    return os.getenv("AKOSHA_API_TOKEN")


def generate_jwt_token(
    user_id: str,
    expiration_minutes: int = 60,
    additional_claims: dict[str, Any] | None = None,
) -> str:
    """Generate a JWT token for authentication.

    Args:
        user_id: User identifier
        expiration_minutes: Token expiration in minutes (default: 60)
        additional_claims: Additional claims to include in token

    Returns:
        JWT token string

    Raises:
        ValueError: If JWT_SECRET is not configured
    """
    jwt_secret = os.getenv("JWT_SECRET")
    if not jwt_secret:
        raise ValueError(
            "JWT_SECRET environment variable required for JWT token generation. "
            "Generate with: openssl rand -base64 32"
        )

    # Prevent placeholder secrets in production
    environment = os.getenv("ENVIRONMENT", "development")
    if environment == "production":
        placeholder_values = ["change-this-in-production", "", "none", "null"]
        if jwt_secret.lower() in placeholder_values:
            raise ValueError(
                "JWT_SECRET must be set to a secure value in production. "
                "Generate with: openssl rand -base64 32"
            )

    try:
        import jwt

        payload = {
            "user_id": user_id,
            "sub": user_id,  # JWT standard subject claim
            "exp": datetime.now(UTC) + timedelta(minutes=expiration_minutes),
            "iat": datetime.now(UTC),  # Issued at
            **(additional_claims or {}),
        }

        token = jwt.encode(payload, jwt_secret, algorithm="HS256")
        logger.info(f"Generated JWT token for user: {user_id}")
        return token

    except ImportError as e:
        raise ValueError("PyJWT library not installed. Install with: pip install pyjwt") from e


def is_auth_enabled() -> bool:
    """Check if authentication is enabled.

    Returns:
        True if authentication is enabled, False otherwise
    """
    # Default to enabled if token is configured
    api_token = get_api_token()
    if api_token:
        return True

    # Check explicit enable/disable flag
    # Default to disabled for local development, enable in production
    enabled = os.getenv("AKOSHA_AUTH_ENABLED", "false").lower()
    return enabled == "true"


def validate_token(token: str) -> bool:
    """Validate authentication token (supports both API tokens and JWT).

    Args:
        token: Token to validate

    Returns:
        True if token is valid, False otherwise
    """
    # First try JWT validation (if JWT_SECRET is configured)
    jwt_secret = os.getenv("JWT_SECRET")
    if jwt_secret:
        try:
            import jwt

            # Remove Bearer prefix if present
            if token.startswith("Bearer "):
                token = token.removeprefix("Bearer ")

            # Decode JWT
            payload = jwt.decode(
                token,
                jwt_secret,
                algorithms=["HS256"],
                options={"verify_exp": True},
            )

            # Check if token has expired
            if payload.get("exp", 0) < datetime.now(UTC).timestamp():
                logger.warning("JWT token has expired")
                return False

            logger.debug("JWT token validated successfully")
            return True

        except jwt.ExpiredSignatureError:
            logger.warning("JWT token has expired")
            return False
        except jwt.InvalidTokenError as e:
            logger.debug(f"JWT validation failed: {e}")
            # Fall through to API token validation
        except Exception as e:
            logger.error(f"Token validation error: {e}")
            return False

    # Fallback to API token validation
    api_token = get_api_token()

    # If no token configured, reject all requests
    if not api_token:
        logger.warning("Authentication check attempted but no API token configured")
        return False

    # Constant-time comparison to prevent timing attacks
    try:
        return secrets.compare_digest(token, api_token)
    except Exception as e:
        logger.error(f"API token validation error: {e}")
        return False


def extract_token_from_headers(headers: dict[str, str] | None) -> str | None:
    """Extract Bearer token from request headers.

    Args:
        headers: Request headers dictionary

    Returns:
        Token string if found, None otherwise
    """
    if not headers:
        return None

    auth_header = headers.get("Authorization") or headers.get("authorization")

    if not auth_header:
        return None

    # Check for Bearer token format
    if not auth_header.startswith("Bearer "):
        logger.warning("Authorization header missing 'Bearer ' prefix")
        return None

    # Extract token
    token = auth_header[7:].strip()  # Remove "Bearer " prefix

    if not token:
        logger.warning("Empty token after 'Bearer ' prefix")
        return None

    return token


def _extract_direct_token(kwargs: dict[str, Any]) -> str | None:
    """Extract authentication token from direct parameters.

    Args:
        kwargs: Function keyword arguments

    Returns:
        Token string if found, None otherwise
    """
    return kwargs.get("auth_token") or kwargs.get("token")


def _clear_token_from_kwargs(kwargs: dict[str, Any]) -> None:
    """Remove token parameters from keyword arguments.

    Args:
        kwargs: Function keyword arguments to modify in-place
    """
    kwargs.pop("auth_token", None)
    kwargs.pop("token", None)


def _extract_context_from_kwargs(kwargs: dict[str, Any]) -> Any:
    """Extract request context from keyword arguments.

    Args:
        kwargs: Function keyword arguments

    Returns:
        Context object if found, None otherwise
    """
    return kwargs.get("_context") or kwargs.get("context")


def _extract_headers_from_context(context: Any) -> dict[str, str] | None:
    """Extract headers from request context.

    Args:
        context: Request context object

    Returns:
        Headers dictionary if found, None otherwise
    """
    return getattr(context, "headers", None) if context else None


def _validate_direct_token(token: str, func_name: str) -> None:
    """Validate a directly provided token.

    Args:
        token: Token to validate
        func_name: Name of the protected function (for error reporting)

    Raises:
        InvalidTokenError: If token is invalid
    """
    if not validate_token(token):
        logger.warning(f"Access denied to {func_name}: invalid token")
        raise InvalidTokenError(
            {
                "tool": func_name,
                "reason": "invalid_token",
            }
        )


def _validate_context_token(token: str, func_name: str) -> None:
    """Validate a token extracted from request context.

    Args:
        token: Token to validate
        func_name: Name of the protected function (for error reporting)

    Raises:
        InvalidTokenError: If token is invalid
    """
    if not validate_token(token):
        logger.warning(f"Access denied to {func_name}: invalid token")
        raise InvalidTokenError(
            {
                "tool": func_name,
                "reason": "token_validation_failed",
            }
        )


def _authenticate_via_context(
    context: Any,
    func_name: str,
) -> None:
    """Authenticate using token from request context.

    Args:
        context: Request context object
        func_name: Name of the protected function (for error reporting)

    Raises:
        MissingTokenError: If token cannot be extracted
        InvalidTokenError: If token is invalid
    """
    if not context:
        logger.warning(f"Access denied to {func_name}: no authentication context")
        raise MissingTokenError(
            {
                "tool": func_name,
                "reason": "no_context_or_token",
            }
        )

    headers = _extract_headers_from_context(context)

    if not headers:
        logger.warning(f"Access denied to {func_name}: no headers in context")
        raise MissingTokenError(
            {
                "tool": func_name,
                "reason": "no_headers",
            }
        )

    token = extract_token_from_headers(headers)

    if not token:
        logger.warning(f"Access denied to {func_name}: missing token")
        raise MissingTokenError(
            {
                "tool": func_name,
                "reason": "missing_bearer_token",
            }
        )

    _validate_context_token(token, func_name)


def require_auth(func: Callable) -> Callable:
    """Decorator to require authentication for MCP tools.

    This decorator checks for a valid Bearer token in the request context
    before allowing access to the protected tool.

    Usage:
        @require_auth
        async def protected_tool(param: str) -> dict:
            return {"result": "protected data"}

    Args:
        func: Function to protect

    Returns:
        Wrapped function with authentication check
    """

    @wraps(func)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        # Check if authentication is enabled
        if not is_auth_enabled():
            logger.debug("Authentication disabled, allowing request")
            return await func(*args, **kwargs)

        # Check for direct token parameter first (for testing)
        token = _extract_direct_token(kwargs)

        if token:
            _validate_direct_token(token, func.__name__)
            _clear_token_from_kwargs(kwargs)
            logger.debug(f"Access granted to {func.__name__} (direct token)")
            return await func(*args, **kwargs)

        # Try to extract token from context
        # FastMCP passes context as part of kwargs
        context = _extract_context_from_kwargs(kwargs)
        _authenticate_via_context(context, func.__name__)

        # Token is valid, proceed with function
        logger.debug(f"Access granted to {func.__name__}")
        return await func(*args, **kwargs)

    return wrapped


class AuthenticationMiddleware:
    """Authentication middleware for FastMCP server.

    This middleware can be used with FastMCP's middleware system to
    automatically protect all tools or specific categories of tools.
    """

    def __init__(
        self,
        protected_categories: set[str] | None = None,
        protected_tools: set[str] | None = None,
    ):
        """Initialize authentication middleware.

        Args:
            protected_categories: Tool categories to protect (default: all aggregation tools)
            protected_tools: Specific tool names to protect (default: all aggregation tools)
        """
        # Default to protecting aggregation and analytics tools
        self.protected_categories = protected_categories or {
            "search",
            "analytics",
            "graph",
        }
        self.protected_tools = protected_tools or {
            "search_all_systems",
            "get_system_metrics",
            "analyze_trends",
            "detect_anomalies",
            "correlate_systems",
            "query_knowledge_graph",
            "find_path",
            "get_graph_statistics",
        }

        logger.info(
            f"Authentication middleware initialized: "
            f"{len(self.protected_categories)} categories, "
            f"{len(self.protected_tools)} tools protected"
        )

    def is_tool_protected(
        self,
        tool_name: str,
        tool_category: str | None = None,
    ) -> bool:
        """Check if a tool requires authentication.

        Args:
            tool_name: Name of the tool
            tool_category: Optional tool category

        Returns:
            True if tool requires authentication, False otherwise
        """
        # Check specific tool name
        if tool_name in self.protected_tools:
            return True

        # Check tool category
        return bool(tool_category and tool_category in self.protected_categories)

    async def authenticate_request(
        self,
        tool_name: str,
        tool_category: str | None = None,
        context: Any = None,
    ) -> bool:
        """Authenticate a request to a protected tool.

        Args:
            tool_name: Name of the tool being accessed
            tool_category: Optional tool category
            context: Request context (may contain headers)

        Returns:
            True if authentication successful, False otherwise

        Raises:
            MissingTokenError: If token is missing
            InvalidTokenError: If token is invalid
        """
        # Check if authentication is enabled
        if not is_auth_enabled():
            return True

        # Check if tool is protected
        if not self.is_tool_protected(tool_name, tool_category):
            return True

        # Extract token from context
        headers = None
        if context and hasattr(context, "headers"):
            headers = context.headers

        token = extract_token_from_headers(headers)

        if not token:
            raise MissingTokenError(
                {
                    "tool": tool_name,
                    "category": tool_category,
                    "reason": "missing_bearer_token",
                }
            )

        if not validate_token(token):
            raise InvalidTokenError(
                {
                    "tool": tool_name,
                    "category": tool_category,
                    "reason": "token_validation_failed",
                }
            )

        return True


def generate_token() -> str:
    """Generate a secure random API token.

    This function generates a cryptographically secure random token
    suitable for use as an API authentication token.

    Returns:
        Secure random token (32 bytes, URL-safe base64 encoded)

    Example:
        >>> token = generate_token()
        >>> print(f"AKOSHA_API_TOKEN={token}")
    """
    return secrets.token_urlsafe(32)


def setup_authentication_instructions() -> str:
    """Generate setup instructions for authentication.

    Returns:
        Setup instructions as a string
    """
    token = generate_token()

    return f"""
# Akosha Authentication Setup

## 1. Generate API Token

A secure token has been generated for you:

```bash
export AKOSHA_API_TOKEN="{token}"
```

## 2. Enable Authentication (Optional)

Authentication is enabled by default when AKOSHA_API_TOKEN is set.
To explicitly enable/disable:

```bash
# Enable authentication
export AKOSHA_AUTH_ENABLED="true"

# Disable authentication (NOT recommended for production)
export AKOSHA_AUTH_ENABLED="false"
```

## 3. Using Authentication

When making MCP tool calls, include the Authorization header:

```python
headers = {{
    "Authorization": f"Bearer {token}"
}}

result = await mcp.call_tool(
    "search_all_systems",
    arguments={{"query": "test"}},
    headers=headers
)
```

## Security Best Practices

1. **Keep tokens secret**: Never commit tokens to version control
2. **Use environment variables**: Store tokens in .env files (gitignored)
3. **Rotate tokens regularly**: Change tokens periodically (e.g., quarterly)
4. **Use strong tokens**: Always use `generate_token()` or similar secure method
5. **Monitor access**: Log authentication attempts and failures

## Protected Tools

The following aggregation endpoints require authentication:

- `search_all_systems` - Cross-system semantic search
- `get_system_metrics` - System-wide metrics and statistics
- `analyze_trends` - Time-series trend analysis
- `detect_anomalies` - Anomaly detection in metrics
- `correlate_systems` - Cross-system correlation analysis
- `query_knowledge_graph` - Knowledge graph queries
- `find_path` - Graph path finding
- `get_graph_statistics` - Graph statistics

For more information, see: akosha/security.py
"""
