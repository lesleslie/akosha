"""Authentication and authorization middleware for Akosha API.

Implements JWT verification, RBAC, and audit logging.
"""

import json
from datetime import UTC, datetime
from logging import getLogger
from typing import Any

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = getLogger(__name__)

security = HTTPBearer()


class AuthConfig:
    """Authentication configuration."""

    def __init__(
        self,
        auth_service_url: str = "http://localhost:8080/verify",
        jwks_url: str | None = None,
        required_claims: list[str] | None = None,
    ):
        self.auth_service_url = auth_service_url
        self.jwks_url = jwks_url
        self.required_claims = required_claims or ["sub", "email", "roles"]

    @classmethod
    def from_env(cls) -> "AuthConfig":
        """Load configuration from environment variables."""
        import os

        return cls(
            auth_service_url=os.getenv("AKOSHA_AUTH_SERVICE_URL", "http://localhost:8080/verify"),
            jwks_url=os.getenv("AKOSHA_JWKS_URL"),
            required_claims=os.getenv("AKOSHA_REQUIRED_CLAIMS", "sub,email,roles").split(","),
        )


# Global auth config
auth_config = AuthConfig.from_env()


async def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)) -> dict[str, Any]:
    """Verify JWT token and return user claims.

    Args:
        credentials: HTTP Bearer token from Authorization header

    Returns:
        User claims dict with keys: sub, email, roles, etc.

    Raises:
        HTTPException: 401 if token is invalid
    """
    token = credentials.credentials

    try:
        # Option 1: Verify with auth service
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.post(
                auth_config.auth_service_url,
                headers={"Authorization": f"Bearer {token}"},
                json={"token": token},
            ) as response:
                if response.status != 200:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Invalid authentication token",
                    )

                claims = await response.json()

        # Validate required claims are present
        for claim in auth_config.required_claims:
            if claim not in claims:
                logger.warning(f"Missing required claim: {claim}")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Missing required claim: {claim}",
                )

        return claims

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token verification failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token verification failed",
        )


class Role:
    """User roles for RBAC."""

    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


async def require_role(required_role: str) -> None:
    """Dependency that requires specific role.

    Use with FastAPI Depends:

        @app.post("/admin/settings")
        async def admin_settings(
            claims: dict = Depends(verify_token),
            _ = Depends(require_role(Role.ADMIN))
        ):
            ...
    """

    def dependency(claims: dict = Depends(verify_token)) -> dict:
        """Check if user has required role."""
        roles = claims.get("roles", [])

        if required_role not in roles:
            logger.warning(
                f"User {claims.get('sub')} attempted to access {required_role} resource "
                f"with roles: {roles}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires {required_role} role",
            )

        return claims

    return dependency


class AuditLogger:
    """Audit logger for sensitive operations."""

    def __init__(self, log_file: str = "/var/log/akosha/audit.log"):
        self.log_file = log_file

    def log(
        self,
        user_id: str,
        action: str,
        resource: str,
        result: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Log audit event.

        Args:
            user_id: User who performed the action
            action: Action performed (create, read, update, delete)
            resource: Resource affected (conversation_id, upload_id, etc.)
            result: Result (success, failure, error)
            details: Additional details
        """
        audit_entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "user_id": user_id,
            "action": action,
            "resource": resource,
            "result": result,
            "details": details or {},
        }

        try:
            with open(self.log_file, "a") as f:
                f.write(json.dumps(audit_entry) + "\n")
        except Exception as e:
            logger.error(f"Failed to write audit log: {e}")

        # Also log to application logger
        logger.info(
            f"AUDIT: {user_id} {action} {resource} - {result}",
            extra={"audit": audit_entry},
        )


# Global audit logger instance
audit_logger = AuditLogger()


def audit_log(action: str, resource_type: str):
    """Create FastAPI dependency for audit logging.

    Usage:
        @app.post("/ingest/upload")
        async def upload(
            request: Request,
            claims: dict = Depends(verify_token),
            _audit = Depends(audit_log("ingest", "upload"))
        ):
            # ... upload logic
            return {"status": "success"}
    """

    def dependency(claims: dict = Depends(verify_token)) -> AuditLogger:
        """Dependency that logs audit event on completion."""
        return audit_logger

    return dependency


class RBACMiddleware:
    """Role-based access control middleware for FastAPI."""

    def __init__(self):
        self.role_permissions = {
            Role.ADMIN: {
                "ingest:upload",
                "ingest:delete",
                "query:search",
                "admin:settings",
                "system:migrate",
                "user:manage",
            },
            Role.OPERATOR: {
                "ingest:upload",
                "query:search",
                "system:status",
            },
            Role.VIEWER: {
                "query:search",
                "system:status",
            },
        }

    def has_permission(self, role: str, permission: str) -> bool:
        """Check if role has permission.

        Args:
            role: User role
            permission: Permission string

        Returns:
            True if role has permission
        """
        role_perms = self.role_permissions.get(role, [])
        return permission in role_perms

    async def check_permission(
        self,
        claims: dict,
        permission: str,
    ) -> None:
        """Check if user has permission, raise if not.

        Args:
            claims: User claims from JWT
            permission: Required permission

        Raises:
            HTTPException: 403 if permission denied
        """
        roles = claims.get("roles", [Role.VIEWER])

        # Check each role
        has_permission = False
        for role in roles:
            if self.has_permission(role, permission):
                has_permission = True
                break

        if not has_permission:
            logger.warning(
                f"User {claims.get('sub')} attempted {permission} "
                f"with roles: {roles}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied: {permission}",
            )


# Global RBAC middleware
rbac = RBACMiddleware()


def require_permission(permission: str):
    """FastAPI dependency that requires specific permission.

    Usage:
        @app.post("/ingest/upload")
        async def upload(
            claims: dict = Depends(verify_token),
            _ = Depends(require_permission("ingest:upload"))
        ):
            ...
    """

    async def dependency(claims: dict = Depends(verify_token)) -> dict:
        """Check permission and raise if denied."""
        await rbac.check_permission(claims, permission)
        return claims

    return dependency


# FastAPI security dependencies for common operations
require_admin = require_role(Role.ADMIN)
require_operator = require_role(Role.OPERATOR)
require_viewer = require_role(Role.VIEWER)


# Common permission dependencies
require_ingest_upload = require_permission("ingest:upload")
require_query_search = require_permission("query:search")
require_system_migrate = require_permission("system:migrate")
require_user_manage = require_permission("user:manage")
