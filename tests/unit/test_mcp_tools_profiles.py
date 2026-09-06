"""Tests for Akosha MCP tool profile registration and discovery."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import MagicMock

import pytest
from mcp_common.tools import ToolProfile

import akosha.mcp.tools as tools_module
from akosha.mcp.tools.profiles import get_active_profile


class DummyFastMCP:
    """Minimal FastMCP stand-in that records registered tools."""

    def __init__(self) -> None:
        self.registered: dict[str, Callable[..., object]] = {}

    def tool(self, *_args, **_kwargs):
        def decorator(fn):
            self.registered[fn.__name__] = fn
            return fn

        return decorator


@pytest.mark.asyncio
async def test_register_all_tools_minimal_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Minimal profiles should only load health tooling plus discovery."""
    app = DummyFastMCP()
    health = MagicMock()
    akosha = MagicMock()
    session_buddy = MagicMock()
    pycharm = MagicMock()

    monkeypatch.setattr(tools_module, "get_active_profile", lambda: ToolProfile.MINIMAL)
    monkeypatch.setattr(tools_module, "register_health_tools_akosha", health)
    monkeypatch.setattr(tools_module, "register_akosha_tools", akosha)
    monkeypatch.setattr(tools_module, "register_session_buddy_tools", session_buddy)
    monkeypatch.setattr(tools_module, "register_pycharm_tools", pycharm)

    tools_module.register_all_tools(app, hot_store=object())

    assert health.call_count == 1
    assert akosha.call_count == 0
    assert session_buddy.call_count == 0
    assert pycharm.call_count == 0

    discover_tools = app.registered["discover_tools"]
    default_result = await discover_tools()
    result = await discover_tools("session")

    assert default_result["profile"] == "minimal"
    assert default_result["query"] is None
    assert default_result["loaded_count"] == 6
    assert default_result["not_loaded_count"] == 20
    # Hint must point operators toward loading more tools (minimal
    # profile is the most restrictive; the full default is reachable
    # by unsetting the env var).
    assert "Minimal profile active" in default_result["hint"]
    assert "AKOSHA_TOOL_PROFILE=standard" in default_result["hint"]
    assert result["profile"] == "minimal"
    assert result["query"] == "session"
    assert result["loaded_count"] == 0
    assert result["loaded_tools"] == []
    assert result["not_loaded_count"] == 2
    assert result["not_loaded_tools"] == [
        "get_cross_system_summary",
        "ingest_session_memory",
    ]


@pytest.mark.asyncio
async def test_register_all_tools_full_profile_and_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full profiles should load all registered groups and surface them in discovery."""
    app = DummyFastMCP()
    health = MagicMock()
    akosha = MagicMock()
    session_buddy = MagicMock()
    pycharm = MagicMock()

    monkeypatch.setattr(tools_module, "get_active_profile", lambda: ToolProfile.FULL)
    monkeypatch.setattr(tools_module, "register_health_tools_akosha", health)
    monkeypatch.setattr(tools_module, "register_akosha_tools", akosha)
    monkeypatch.setattr(tools_module, "register_session_buddy_tools", session_buddy)
    monkeypatch.setattr(tools_module, "register_pycharm_tools", pycharm)

    tools_module.register_all_tools(app, hot_store=object())

    assert health.call_count == 1
    assert akosha.call_count == 1
    assert session_buddy.call_count == 1
    assert pycharm.call_count == 1

    discover_tools = app.registered["discover_tools"]
    result = await discover_tools("session")

    assert result["profile"] == "full"
    assert result["query"] == "session"
    assert result["loaded_count"] == 2
    assert result["loaded_tools"] == [
        "get_cross_system_summary",
        "ingest_session_memory",
    ]
    assert result["not_loaded_count"] == 0
    assert result["not_loaded_tools"] == []
    # Full is the default; the hint must not misleadingly suggest
    # upgrading to full (the static string "Set AKOSHA_TOOL_PROFILE=full
    # to enable all tools" was always-wrong regardless of profile).
    assert "All tool groups loaded" in result["hint"]
    assert "AKOSHA_TOOL_PROFILE=full" not in result["hint"]


@pytest.mark.asyncio
async def test_discover_tools_hint_for_standard_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Standard profile hint must point operators to ``full`` (the default)."""
    app = DummyFastMCP()
    monkeypatch.setattr(tools_module, "get_active_profile", lambda: ToolProfile.STANDARD)
    monkeypatch.setattr(tools_module, "register_health_tools_akosha", MagicMock())
    monkeypatch.setattr(tools_module, "register_akosha_tools", MagicMock())
    monkeypatch.setattr(tools_module, "register_session_buddy_tools", MagicMock())
    monkeypatch.setattr(tools_module, "register_pycharm_tools", MagicMock())

    tools_module.register_all_tools(app, hot_store=object())

    discover_tools = app.registered["discover_tools"]
    result = await discover_tools()

    assert result["profile"] == "standard"
    # Standard hint must point toward full (the default) so an operator
    # who explicitly chose standard can quickly see how to recover the
    # missing groups without reading the docs.
    assert "Standard profile active" in result["hint"]
    assert "AKOSHA_TOOL_PROFILE=full" in result["hint"]


def test_get_active_profile_defaults_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The profile helper should read the environment variable contract."""
    monkeypatch.setenv("AKOSHA_TOOL_PROFILE", "minimal")

    assert get_active_profile() == ToolProfile.MINIMAL


def test_register_health_tools_akosha_delegates_to_shared_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The health-registration wrapper should call the shared contract with Akosha metadata."""
    health_register = MagicMock()
    monkeypatch.setattr(tools_module, "register_health_tools", health_register)

    tools_module.register_health_tools_akosha(object())

    from akosha import __version__
    assert health_register.call_count == 1
    kwargs = health_register.call_args.kwargs
    assert kwargs["service_name"] == "akosha"
    assert kwargs["version"] == __version__, (
        f"register_health_tools_akosha version {kwargs['version']!r} "
        f"drifted from akosha.__version__ {__version__!r}"
    )
    assert kwargs["dependencies"] == tools_module.DEFAULT_DEPENDENCIES
