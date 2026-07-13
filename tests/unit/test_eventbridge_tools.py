"""Unit tests for ``akosha.mcp.tools.eventbridge_tools``.

Verifies the registration-time wiring and per-call re-read semantics of
the ``enabled`` toggle. The MCP tool is opt-in: callers (operators via
the MCP server) can flip the toggle without restarting the server.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from akosha.mcp.tools.eventbridge_tools import register_eventbridge_tools


pytestmark = pytest.mark.unit


def _capture_tool() -> Any:
    """Build a fake FastMCP app whose ``@app.tool()`` decorator captures the
    wrapped async function. Returns the captured callable.
    """
    captured: list[Any] = []

    def tool_decorator(*_args: Any, **_kwargs: Any) -> Any:
        def deco(func: Any) -> Any:
            captured.append(func)
            return func

        return deco

    app = MagicMock()
    app.tool = tool_decorator
    register_eventbridge_tools(app)
    assert len(captured) == 1, f"expected 1 tool, registered {len(captured)}"
    return captured[0]


def _capture_tool_with(
    *,
    enabled: bool | None = None,
    enabled_fn: Any = None,
) -> Any:
    """Build a fake FastMCP app and register with custom enabled source.

    Exactly one of ``enabled`` or ``enabled_fn`` should be provided to
    exercise the legacy and per-call paths respectively.
    """
    captured: list[Any] = []

    def tool_decorator(*_args: Any, **_kwargs: Any) -> Any:
        def deco(func: Any) -> Any:
            captured.append(func)
            return func

        return deco

    app = MagicMock()
    app.tool = tool_decorator
    kwargs: dict[str, Any] = {}
    if enabled is not None:
        kwargs["enabled"] = enabled
    if enabled_fn is not None:
        kwargs["enabled_fn"] = enabled_fn
    register_eventbridge_tools(app, **kwargs)
    assert len(captured) == 1
    return captured[0]


@pytest.mark.asyncio
async def test_enabled_re_reads_each_call_when_enabled_fn_provided() -> None:
    """When ``enabled_fn`` is passed, the closure invokes it on EVERY call.

    This is the per-call re-read contract: operators can flip the toggle
    without restarting the MCP server. The legacy ``enabled`` boolean is
    captured once at registration -- this test verifies the new behavior.
    """
    state = {"enabled": False}

    def my_enabled_fn() -> bool:
        return state["enabled"]

    publish = _capture_tool_with(enabled_fn=my_enabled_fn)

    result_1 = await publish(topic="pattern.detected", payload={"x": 1})
    assert result_1 == {"status": "disabled"}

    state["enabled"] = True

    result_2 = await publish(topic="pattern.detected", payload={"x": 2})
    assert result_2.get("status") == "no_publisher", (
        "After flipping enabled=True mid-flight the next call must "
        "observe the new state. Got: " + repr(result_2)
    )


@pytest.mark.asyncio
async def test_enabled_callable_invoked_per_call_not_once() -> None:
    """The enabled callable is invoked EVERY call, not cached at registration."""
    call_count = 0

    def my_enabled_fn() -> bool:
        nonlocal call_count
        call_count += 1
        return False

    publish = _capture_tool_with(enabled_fn=my_enabled_fn)

    await publish(topic="pattern.detected", payload={"x": 1})
    await publish(topic="pattern.detected", payload={"x": 2})
    await publish(topic="pattern.detected", payload={"x": 3})

    assert call_count == 3, (
        f"enabled_fn should be invoked once per call; got {call_count} for 3 calls"
    )


@pytest.mark.asyncio
async def test_legacy_enabled_flag_still_supported() -> None:
    """Backward compat: ``enabled=False`` (legacy) keeps the tool disabled.

    When ``enabled_fn`` is not provided, the legacy ``enabled`` bool is
    used and the tool returns ``{"status": "disabled"}`` regardless of
    call count.
    """
    publish = _capture_tool_with(enabled=False)

    result = await publish(topic="pattern.detected", payload={"x": 1})
    assert result == {"status": "disabled"}


@pytest.mark.asyncio
async def test_legacy_enabled_true_returns_no_publisher() -> None:
    """When legacy ``enabled=True`` is passed without a publisher wired,
    the tool returns ``{"status": "no_publisher", ...}``.
    """
    publish = _capture_tool_with(enabled=True)

    result = await publish(topic="pattern.detected", payload={"x": 1})
    assert result.get("status") == "no_publisher"
    assert "warning" in result
