from __future__ import annotations

import logging
from functools import wraps
from typing import TYPE_CHECKING, Any

from mcp_common.auth.config import AuthConfig
from mcp_common.auth.core import create_service_token
from mcp_common.auth.core import verify_token as _verify_token
from mcp_common.auth.exceptions import AuthError
from mcp_common.auth.permissions import Permission

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


class MCPAuthError(Exception):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


_config: AuthConfig | None = None


def _reset_config() -> None:
    global _config
    _config = None


def _get_config() -> AuthConfig:
    global _config
    if _config is None:
        _config = AuthConfig(service_name="akosha", secret_env_var="JWT_SECRET")
    return _config


def require_auth(
    func_or_permission: Callable[..., Any] | Permission = Permission.READ,
) -> Callable[..., Any]:
    if callable(func_or_permission):
        return _make_wrapper(func_or_permission, Permission.READ)
    if not isinstance(func_or_permission, Permission):
        raise TypeError(
            f"require_auth expects a Permission or callable, got {type(func_or_permission).__name__!r}"
        )
    permission = func_or_permission

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        return _make_wrapper(func, permission)

    return decorator


def _make_wrapper(func: Callable[..., Any], permission: Permission) -> Callable[..., Any]:
    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        cfg = _get_config()
        if not cfg.enabled:
            kwargs["authenticated_user_id"] = "anonymous"
            return await func(*args, **kwargs)

        token_str = kwargs.pop("__auth_token__", None) or kwargs.pop("__request_context__", {}).get(
            "auth_token"
        )
        if not token_str:
            raise MCPAuthError("Authentication required. Please provide a valid JWT token.")

        if isinstance(token_str, str) and token_str.startswith("Bearer "):
            token_str = token_str.removeprefix("Bearer ")

        try:
            payload = _verify_token(token_str, secret=cfg.secret, expected_audience="akosha")
        except AuthError as exc:
            raise MCPAuthError(str(exc)) from exc

        if permission not in payload.permissions:
            raise MCPAuthError(f"Insufficient permissions: {permission.value!r} required")

        kwargs["authenticated_user_id"] = payload.subject
        return await func(*args, **kwargs)

    return wrapper


def validate_auth_config() -> bool:
    cfg = _get_config()
    if cfg.enabled:
        logger.info("Authentication configuration validated")
    else:
        logger.warning("Authentication disabled (no secret configured)")
    return True


def generate_jwt_token(
    user_id: str,  # noqa: ARG001
    expiration_minutes: int | None = None,
    additional_claims: dict[str, Any] | None = None,  # noqa: ARG001
) -> str:
    cfg = _get_config()
    ttl = (expiration_minutes or 60) * 60
    return create_service_token(
        secret=cfg.secret,
        issuer="akosha",
        audience="akosha",
        permissions=[Permission.READ],
        ttl_seconds=ttl,
    )


__all__ = [
    "MCPAuthError",
    "generate_jwt_token",
    "require_auth",
    "validate_auth_config",
]
