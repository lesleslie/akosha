from __future__ import annotations

from types import SimpleNamespace

import pytest
from mcp_common.auth.exceptions import AuthError
from mcp_common.auth.permissions import Permission

SECRET = "akosha-test-secret-that-is-at-least-32-chars"


@pytest.fixture(autouse=True)
def _reset_auth_config():
    try:
        from akosha.mcp.auth import _reset_config

        _reset_config()
    except (ImportError, AttributeError):
        pass
    yield
    try:
        from akosha.mcp.auth import _reset_config

        _reset_config()
    except (ImportError, AttributeError):
        pass


def test_validate_auth_config_passes_when_disabled(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.delenv("BODAI_SHARED_SECRET", raising=False)

    from akosha.mcp.auth import validate_auth_config

    assert validate_auth_config() is True


def test_validate_auth_config_raises_when_secret_too_short(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "short")

    from akosha.mcp.auth import validate_auth_config

    with pytest.raises((ValueError, Exception)):
        validate_auth_config()


@pytest.mark.asyncio
async def test_require_auth_passes_when_disabled(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.delenv("BODAI_SHARED_SECRET", raising=False)

    from akosha.mcp.auth import require_auth

    received: dict = {}

    @require_auth
    async def my_tool(**kwargs):
        received.update(kwargs)
        return "ok"

    result = await my_tool()
    assert result == "ok"
    assert received.get("authenticated_user_id") == "anonymous"


@pytest.mark.asyncio
async def test_require_auth_callable_shortcut(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.delenv("BODAI_SHARED_SECRET", raising=False)

    from akosha.mcp.auth import require_auth

    received: dict = {}

    async def my_tool(**kwargs):
        received.update(kwargs)
        return kwargs.get("authenticated_user_id")

    wrapped = require_auth(my_tool)

    assert wrapped is not my_tool
    assert await wrapped() == "anonymous"
    assert received["authenticated_user_id"] == "anonymous"


def test_require_auth_rejects_non_permission_value():
    from akosha.mcp.auth import require_auth

    with pytest.raises(TypeError, match="Permission or callable"):
        require_auth("bad")


@pytest.mark.asyncio
async def test_require_auth_enforces_permission_and_strips_bearer(monkeypatch):
    from akosha.mcp import auth as auth_module
    from akosha.mcp.auth import require_auth

    monkeypatch.setattr(
        auth_module,
        "_get_config",
        lambda: SimpleNamespace(enabled=True, secret=SECRET),
    )

    def fake_verify(token, secret, expected_audience):
        assert token == "jwt-token"
        assert secret == SECRET
        assert expected_audience == "akosha"
        return SimpleNamespace(subject="user-123", permissions=[Permission.WRITE])

    monkeypatch.setattr(auth_module, "_verify_token", fake_verify)

    received: dict = {}

    @require_auth(Permission.WRITE)
    async def my_tool(**kwargs):
        received.update(kwargs)
        return "ok"

    result = await my_tool(__auth_token__="Bearer jwt-token")

    assert result == "ok"
    assert received["authenticated_user_id"] == "user-123"
    assert "__auth_token__" not in received


@pytest.mark.asyncio
async def test_require_auth_raises_on_missing_token(monkeypatch):
    from akosha.mcp import auth as auth_module
    from akosha.mcp.auth import MCPAuthError, require_auth

    monkeypatch.setattr(
        auth_module,
        "_get_config",
        lambda: SimpleNamespace(enabled=True, secret=SECRET),
    )

    @require_auth(Permission.READ)
    async def my_tool(**kwargs):
        return "ok"

    with pytest.raises(MCPAuthError, match="Authentication required"):
        await my_tool()


@pytest.mark.asyncio
async def test_require_auth_converts_auth_error(monkeypatch):
    from akosha.mcp import auth as auth_module
    from akosha.mcp.auth import MCPAuthError, require_auth

    monkeypatch.setattr(
        auth_module,
        "_get_config",
        lambda: SimpleNamespace(enabled=True, secret=SECRET),
    )
    monkeypatch.setattr(
        auth_module,
        "_verify_token",
        lambda *args, **kwargs: (_ for _ in ()).throw(AuthError("bad token")),
    )

    @require_auth(Permission.READ)
    async def my_tool(**kwargs):
        return "ok"

    with pytest.raises(MCPAuthError, match="bad token"):
        await my_tool(__auth_token__="jwt-token")


@pytest.mark.asyncio
async def test_require_auth_rejects_missing_permission(monkeypatch):
    from akosha.mcp import auth as auth_module
    from akosha.mcp.auth import MCPAuthError, require_auth

    monkeypatch.setattr(
        auth_module,
        "_get_config",
        lambda: SimpleNamespace(enabled=True, secret=SECRET),
    )
    monkeypatch.setattr(
        auth_module,
        "_verify_token",
        lambda *args, **kwargs: SimpleNamespace(
            subject="user-123",
            permissions=[Permission.READ],
        ),
    )

    @require_auth(Permission.ADMIN)
    async def my_tool(**kwargs):
        return "ok"

    with pytest.raises(MCPAuthError, match="Insufficient permissions"):
        await my_tool(__auth_token__="jwt-token")


def test_generate_jwt_token_uses_service_token(monkeypatch):
    from akosha.mcp import auth as auth_module

    monkeypatch.setattr(
        auth_module,
        "_get_config",
        lambda: SimpleNamespace(enabled=True, secret=SECRET),
    )
    captured: dict[str, object] = {}

    def fake_create_service_token(**kwargs):
        captured.update(kwargs)
        return "token-value"

    monkeypatch.setattr(auth_module, "create_service_token", fake_create_service_token)

    from akosha.mcp.auth import generate_jwt_token

    token = generate_jwt_token("user-123", expiration_minutes=15)

    assert token == "token-value"
    assert captured["secret"] == SECRET
    assert captured["permissions"] == [Permission.READ]
    assert captured["ttl_seconds"] == 900


def test_get_config_creates_and_caches_instance(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.delenv("BODAI_SHARED_SECRET", raising=False)

    from akosha.mcp import auth as auth_module

    auth_module._reset_config()
    first = auth_module._get_config()
    second = auth_module._get_config()

    assert first is second
    assert first.enabled is False
