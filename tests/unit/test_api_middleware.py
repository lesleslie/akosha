"""Tests for Akosha API authentication and authorization middleware."""

import json
import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from akosha.api.middleware import (
    AuditLogger,
    AuthConfig,
    RBACMiddleware,
    Role,
    require_admin,
    require_operator,
    require_permission,
    require_role,
)

try:
    from akosha.api.middleware import require_viewer as _require_viewer

    _has_viewer = True
except ImportError:
    _has_viewer = False


# ============================================================================
# AuthConfig
# ============================================================================


class TestAuthConfig:
    """Tests for AuthConfig configuration class."""

    def test_default_config(self):
        config = AuthConfig()
        assert config.auth_service_url == "http://localhost:8080/verify"
        assert config.jwks_url is None
        assert config.required_claims == ["sub", "email", "roles"]

    def test_custom_config(self):
        config = AuthConfig(
            auth_service_url="https://auth.example.com/verify",
            jwks_url="https://auth.example.com/.well-known/jwks.json",
            required_claims=["sub"],
        )
        assert "auth.example.com" in config.auth_service_url
        assert config.jwks_url is not None
        assert config.required_claims == ["sub"]

    def test_from_env_overrides(self, monkeypatch):
        monkeypatch.setenv("AKOSHA_AUTH_SERVICE_URL", "https://env.example.com/verify")
        monkeypatch.setenv("AKOSHA_JWKS_URL", "https://env.example.com/jwks")
        monkeypatch.setenv("AKOSHA_REQUIRED_CLAIMS", "sub,org")
        config = AuthConfig.from_env()
        assert "env.example.com" in config.auth_service_url
        assert config.jwks_url == "https://env.example.com/jwks"
        assert config.required_claims == ["sub", "org"]

    def test_from_env_missing_vars_use_defaults(self, monkeypatch):
        monkeypatch.delenv("AKOSHA_AUTH_SERVICE_URL", raising=False)
        monkeypatch.delenv("AKOSHA_JWKS_URL", raising=False)
        monkeypatch.delenv("AKOSHA_REQUIRED_CLAIMS", raising=False)
        config = AuthConfig.from_env()
        assert config.auth_service_url == "http://localhost:8080/verify"
        assert config.jwks_url is None
        assert config.required_claims == ["sub", "email", "roles"]

    def test_empty_required_claims_uses_default(self):
        config = AuthConfig(required_claims=None)
        assert config.required_claims == ["sub", "email", "roles"]


# ============================================================================
# verify_token
# ============================================================================


class TestVerifyToken:
    """Tests for JWT token verification."""

    @pytest.fixture
    def mock_credentials(self):
        cred = MagicMock()
        cred.credentials = "test-token-123"
        return cred

    @pytest.fixture
    def valid_claims(self):
        return {
            "sub": "user-123",
            "email": "test@example.com",
            "roles": ["admin"],
        }

    class _FakeResponse:
        def __init__(self, status: int, payload: object | None = None) -> None:
            self.status = status
            self._payload = payload

        async def __aenter__(self) -> object:
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def json(self) -> object:
            assert self._payload is not None
            return self._payload

    @staticmethod
    def _install_fake_aiohttp(
        monkeypatch: pytest.MonkeyPatch,
        response: object | None = None,
        post_exc: Exception | None = None,
    ) -> object:
        class _FakeSession:
            def __init__(self, response_obj: object | None, post_exception: Exception | None) -> None:
                self._response = response_obj
                self._post_exception = post_exception
                self.post_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

            async def __aenter__(self) -> object:
                return self

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                return False

            def post(self, *args: object, **kwargs: object) -> object:
                self.post_calls.append((args, kwargs))
                if self._post_exception is not None:
                    raise self._post_exception
                assert self._response is not None
                return self._response

        fake_module = ModuleType("aiohttp")
        session = _FakeSession(response, post_exc)
        fake_module.ClientSession = lambda: session  # type: ignore[assignment]
        monkeypatch.setitem(sys.modules, "aiohttp", fake_module)
        return session

    @pytest.mark.asyncio
    async def test_valid_token_returns_claims(
        self,
        mock_credentials,
        valid_claims,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import akosha.api.middleware as middleware_module

        response = self._FakeResponse(200, valid_claims)
        session = self._install_fake_aiohttp(monkeypatch, response=response)
        monkeypatch.setattr(
            middleware_module,
            "auth_config",
            AuthConfig(
                auth_service_url="https://auth.example.com/verify",
                required_claims=["sub", "email", "roles"],
            ),
        )

        claims = await middleware_module.verify_token(mock_credentials)

        assert claims == valid_claims
        assert session.post_calls == [
            (
                ("https://auth.example.com/verify",),
                {
                    "headers": {"Authorization": "Bearer test-token-123"},
                    "json": {"token": "test-token-123"},
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_invalid_token_returns_401(
        self,
        mock_credentials,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import akosha.api.middleware as middleware_module
        from fastapi import HTTPException

        response = self._FakeResponse(401, {"detail": "nope"})
        self._install_fake_aiohttp(monkeypatch, response=response)
        monkeypatch.setattr(
            middleware_module,
            "auth_config",
            AuthConfig(auth_service_url="https://auth.example.com/verify"),
        )

        with pytest.raises(HTTPException) as exc_info:
            await middleware_module.verify_token(mock_credentials)

        assert exc_info.value.status_code == 401
        assert "Invalid authentication token" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_auth_service_unreachable_returns_401(
        self,
        mock_credentials,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import akosha.api.middleware as middleware_module
        from fastapi import HTTPException

        self._install_fake_aiohttp(
            monkeypatch,
            post_exc=RuntimeError("service unavailable"),
        )
        monkeypatch.setattr(
            middleware_module,
            "auth_config",
            AuthConfig(auth_service_url="https://auth.example.com/verify"),
        )

        with pytest.raises(HTTPException) as exc_info:
            await middleware_module.verify_token(mock_credentials)

        assert exc_info.value.status_code == 401
        assert "Token verification failed" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_missing_required_claim_returns_403(
        self,
        mock_credentials,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import akosha.api.middleware as middleware_module
        from fastapi import HTTPException

        response = self._FakeResponse(200, {"sub": "user-123", "email": "test@example.com"})
        self._install_fake_aiohttp(monkeypatch, response=response)
        monkeypatch.setattr(
            middleware_module,
            "auth_config",
            AuthConfig(
                auth_service_url="https://auth.example.com/verify",
                required_claims=["sub", "email", "roles"],
            ),
        )

        with pytest.raises(HTTPException) as exc_info:
            await middleware_module.verify_token(mock_credentials)

        assert exc_info.value.status_code == 403
        assert "Missing required claim: roles" in exc_info.value.detail


# ============================================================================
# Role
# ============================================================================


class TestRole:
    """Tests for Role constants."""

    def test_role_constants(self):
        assert Role.ADMIN == "admin"
        assert Role.OPERATOR == "operator"
        assert Role.VIEWER == "viewer"

    def test_role_values_are_strings(self):
        assert isinstance(Role.ADMIN, str)
        assert isinstance(Role.OPERATOR, str)
        assert isinstance(Role.VIEWER, str)


# ============================================================================
# require_role
# ============================================================================


class TestRequireRole:
    """Tests for require_role dependency factory."""

    @pytest.mark.asyncio
    async def test_returns_dependency_function(self):
        dep = await require_role(Role.ADMIN)
        assert callable(dep)

    @pytest.mark.asyncio
    async def test_admin_role_granted(self):
        dep = await require_role(Role.ADMIN)
        claims = {"sub": "admin-user", "roles": ["admin"]}
        result = dep(claims=claims)
        assert result["sub"] == "admin-user"

    @pytest.mark.asyncio
    async def test_operator_role_granted(self):
        dep = await require_role(Role.OPERATOR)
        claims = {"sub": "ops-user", "roles": ["operator"]}
        result = dep(claims=claims)
        assert result["sub"] == "ops-user"

    @pytest.mark.asyncio
    async def test_viewer_role_denied_admin_access(self):
        from fastapi import HTTPException

        dep = await require_role(Role.ADMIN)
        claims = {"sub": "viewer-user", "roles": ["viewer"]}
        with pytest.raises(HTTPException) as exc_info:
            dep(claims=claims)
        assert exc_info.value.status_code == 403
        assert "admin" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_empty_roles_denied(self):
        from fastapi import HTTPException

        dep = await require_role(Role.ADMIN)
        claims = {"sub": "no-role-user", "roles": []}
        with pytest.raises(HTTPException) as exc_info:
            dep(claims=claims)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_multiple_roles_one_matches(self):
        dep = await require_role(Role.OPERATOR)
        claims = {"sub": "multi-user", "roles": ["viewer", "operator"]}
        result = dep(claims=claims)
        assert result["sub"] == "multi-user"

    @pytest.mark.asyncio
    async def test_missing_roles_key_denied(self):
        from fastapi import HTTPException

        dep = await require_role(Role.ADMIN)
        claims = {"sub": "no-roles-key"}
        with pytest.raises(HTTPException) as exc_info:
            dep(claims=claims)
        assert exc_info.value.status_code == 403


# ============================================================================
# AuditLogger
# ============================================================================


class TestAuditLogger:
    """Tests for AuditLogger."""

    def test_log_creates_json_entry(self, tmp_path):
        logger = AuditLogger(log_file=str(tmp_path / "audit.log"))
        logger.log("user-1", "create", "resource-1", "success")
        content = (tmp_path / "audit.log").read_text()
        entry = json.loads(content.strip())
        assert entry["user_id"] == "user-1"
        assert entry["action"] == "create"
        assert entry["resource"] == "resource-1"
        assert entry["result"] == "success"

    def test_log_includes_timestamp(self, tmp_path):
        logger = AuditLogger(log_file=str(tmp_path / "audit.log"))
        logger.log("user-1", "read", "resource-1", "success")
        content = (tmp_path / "audit.log").read_text()
        entry = json.loads(content.strip())
        assert "timestamp" in entry
        assert "T" in entry["timestamp"]

    def test_log_handles_file_write_error(self, tmp_path):
        logger = AuditLogger(log_file=str(tmp_path / "nonexistent" / "audit.log"))
        logger.log("user-1", "create", "resource-1", "success")
        # Should not raise, just log error

    def test_log_details_default_to_empty_dict(self, tmp_path):
        logger = AuditLogger(log_file=str(tmp_path / "audit.log"))
        logger.log("user-1", "delete", "resource-1", "success")
        content = (tmp_path / "audit.log").read_text()
        entry = json.loads(content.strip())
        assert entry["details"] == {}

    def test_log_with_details(self, tmp_path):
        logger = AuditLogger(log_file=str(tmp_path / "audit.log"))
        details = {"reason": "cleanup", "count": 5}
        logger.log("user-1", "delete", "resource-1", "success", details=details)
        content = (tmp_path / "audit.log").read_text()
        entry = json.loads(content.strip())
        assert entry["details"]["reason"] == "cleanup"
        assert entry["details"]["count"] == 5

    def test_log_multiple_entries(self, tmp_path):
        logger = AuditLogger(log_file=str(tmp_path / "audit.log"))
        logger.log("user-1", "create", "r1", "success")
        logger.log("user-2", "delete", "r2", "failure")
        content = (tmp_path / "audit.log").read_text()
        lines = content.strip().split("\n")
        assert len(lines) == 2


# ============================================================================
# RBACMiddleware
# ============================================================================


class TestRBACMiddleware:
    """Tests for RBACMiddleware."""

    @pytest.fixture
    def middleware(self):
        return RBACMiddleware()

    def test_admin_has_all_permissions(self, middleware):
        admin_perms = middleware.role_permissions[Role.ADMIN]
        assert "ingest:upload" in admin_perms
        assert "admin:settings" in admin_perms
        assert "system:migrate" in admin_perms
        assert "user:manage" in admin_perms

    def test_operator_has_limited_permissions(self, middleware):
        ops_perms = middleware.role_permissions[Role.OPERATOR]
        assert "ingest:upload" in ops_perms
        assert "query:search" in ops_perms
        assert "admin:settings" not in ops_perms
        assert "user:manage" not in ops_perms

    def test_viewer_has_minimal_permissions(self, middleware):
        viewer_perms = middleware.role_permissions[Role.VIEWER]
        assert "query:search" in viewer_perms
        assert "ingest:upload" not in viewer_perms
        assert "admin:settings" not in viewer_perms

    def test_has_permission_unknown_role_returns_false(self, middleware):
        assert middleware.has_permission("unknown_role", "query:search") is False

    def test_has_permission_unknown_permission_returns_false(self, middleware):
        assert middleware.has_permission(Role.ADMIN, "nonexistent:perm") is False

    @pytest.mark.asyncio
    async def test_check_permission_granted(self, middleware):
        claims = {"sub": "admin-user", "roles": ["admin"]}
        await middleware.check_permission(claims, "admin:settings")

    @pytest.mark.asyncio
    async def test_check_permission_denied(self, middleware):
        from fastapi import HTTPException

        claims = {"sub": "viewer-user", "roles": ["viewer"]}
        with pytest.raises(HTTPException) as exc_info:
            await middleware.check_permission(claims, "admin:settings")
        assert exc_info.value.status_code == 403
        assert "Permission denied" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_check_permission_multiple_roles(self, middleware):
        claims = {"sub": "multi-user", "roles": ["viewer", "operator"]}
        await middleware.check_permission(claims, "ingest:upload")

    @pytest.mark.asyncio
    async def test_check_permission_viewer_cannot_ingest(self, middleware):
        from fastapi import HTTPException

        claims = {"sub": "viewer-user", "roles": ["viewer"]}
        with pytest.raises(HTTPException):
            await middleware.check_permission(claims, "ingest:upload")


# ============================================================================
# require_permission
# ============================================================================


class TestRequirePermission:
    """Tests for require_permission dependency factory."""

    def test_returns_callable(self):
        dep = require_permission("ingest:upload")
        assert callable(dep)

    @pytest.mark.asyncio
    async def test_permission_granted_for_admin(self):
        dep = require_permission("ingest:upload")
        claims = {"sub": "admin", "roles": ["admin"]}
        result = await dep(claims=claims)
        assert result["sub"] == "admin"

    @pytest.mark.asyncio
    async def test_permission_denied_for_viewer(self):
        from fastapi import HTTPException

        dep = require_permission("admin:settings")
        claims = {"sub": "viewer", "roles": ["viewer"]}
        with pytest.raises(HTTPException):
            await dep(claims=claims)


# ============================================================================
# Prebuilt dependencies
# ============================================================================


class TestPrebuiltDependencies:
    """Tests for prebuilt role/permission dependencies."""

    @pytest.mark.asyncio
    async def test_require_admin_is_callable(self):
        dep = await require_admin
        assert callable(dep)

    @pytest.mark.asyncio
    async def test_require_operator_is_callable(self):
        dep = await require_operator
        assert callable(dep)

    @pytest.mark.asyncio
    async def test_require_viewer_is_callable(self):
        if _has_viewer:
            dep = await _require_viewer
            assert callable(dep)
        else:
            pytest.skip("require_viewer not exported")

    def test_audit_log_dependency_returns_global_logger(self):
        import akosha.api.middleware as middleware_module

        dep = middleware_module.audit_log("ingest", "upload")
        assert callable(dep)
        assert dep() is middleware_module.audit_logger
