from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True, scope="function")
def _reset_auth_config():
    yield
    try:
        from akosha.mcp.auth import _reset_config
        _reset_config()
    except (ImportError, AttributeError):
        pass


def test_validate_auth_config_passes_when_disabled(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.delenv("BODAI_SHARED_SECRET", raising=False)
    assert os.environ.get("JWT_SECRET") is None
    assert os.environ.get("BODAI_SHARED_SECRET") is None

    from akosha.mcp.auth import validate_auth_config

    assert validate_auth_config() is True


def test_validate_auth_config_raises_when_secret_too_short(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "short")
    assert os.environ.get("JWT_SECRET") == "short"

    from akosha.mcp.auth import validate_auth_config

    with pytest.raises((ValueError, Exception)):
        validate_auth_config()


@pytest.mark.asyncio
async def test_require_auth_passes_when_disabled(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.delenv("BODAI_SHARED_SECRET", raising=False)

    from akosha.mcp.auth import require_auth

    @require_auth
    async def my_tool(**kwargs):
        return "ok"

    result = await my_tool()
    assert result == "ok"
