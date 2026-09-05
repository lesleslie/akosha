"""Tests for ``akosha.mcp.tools.tool_registry`` — FastMCPToolRegistry + ToolCategory.

Audit found this module at 0% coverage. The registry sits between
``@app.tool()`` (FastMCP's decorator) and the metadata each tool
declares — it must reject sync tools (FastMCP expects async
coroutines), record the metadata, and return a working decorated
function.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from akosha.mcp.tools.tool_registry import (
    FastMCPToolRegistry,
    ToolCategory,
    ToolMetadata,
    ToolRegistration,
)


class FakeApp:
    """Captures tool registrations so we can assert what got decorated."""

    def __init__(self) -> None:
        self.registrations: dict[str, dict[str, Any]] = {}

    def tool(self, *, name: str, description: str) -> Any:
        def decorator(func: Any) -> Any:
            self.registrations[name] = {
                "description": description,
                "function": func,
            }
            return func

        return decorator


# ---------------------------------------------------------------------------
# ToolCategory
# ---------------------------------------------------------------------------


def test_tool_category_has_six_members() -> None:
    """Pin the public surface — adding a category is a deliberate change."""
    expected = {"search", "analytics", "events", "graph", "ingestion", "system"}
    actual = {c.value for c in ToolCategory}
    assert actual == expected


def test_tool_category_members_are_str_enum() -> None:
    """StrEnum members compare equal to their string value."""
    assert ToolCategory.SEARCH == "search"
    assert ToolCategory.GRAPH == "graph"


# ---------------------------------------------------------------------------
# FastMCPToolRegistry — registration + sync-tool rejection
# ---------------------------------------------------------------------------


@pytest.fixture
def app() -> FakeApp:
    return FakeApp()


@pytest.fixture
def registry(app: FakeApp) -> FastMCPToolRegistry:
    return FastMCPToolRegistry(app)  # type: ignore[arg-type]


def test_registry_starts_empty(registry: FastMCPToolRegistry) -> None:
    assert registry.tools == {}


def test_register_decorates_tool_with_name_and_description(
    registry: FastMCPToolRegistry, app: FakeApp
) -> None:
    async def my_tool(x: int) -> int:
        return x * 2

    metadata = ToolMetadata(
        name="my_tool",
        description="doubles an int",
        category=ToolCategory.SYSTEM,
    )
    decorated = registry.register(metadata)(my_tool)
    assert app.registrations["my_tool"]["description"] == "doubles an int"
    assert app.registrations["my_tool"]["function"] is decorated


def test_register_records_tool_metadata_in_tools_dict(
    registry: FastMCPToolRegistry,
) -> None:
    async def my_tool() -> str:
        return "hi"

    metadata = ToolMetadata(
        name="my_tool",
        description="...",
        category=ToolCategory.ANALYTICS,
        examples=[{"input": {}, "output": "hi"}],
        is_async=True,
    )
    registry.register(metadata)(my_tool)
    record = registry.tools["my_tool"]
    assert isinstance(record, ToolRegistration)
    assert record.metadata is metadata
    assert record.coroutine is my_tool


def test_register_rejects_sync_function(
    registry: FastMCPToolRegistry,
) -> None:
    """Tools must be async — FastMCP dispatches via await on the result."""
    metadata = ToolMetadata(
        name="sync_tool",
        description="...",
        category=ToolCategory.SYSTEM,
    )

    def sync_tool() -> str:  # NOT async
        return "hi"

    with pytest.raises(TypeError, match="must be async"):
        registry.register(metadata)(sync_tool)


def test_register_returns_the_decorated_function(
    registry: FastMCPToolRegistry,
) -> None:
    """The decorator must return a callable so callers can hold the reference."""

    async def my_tool() -> None:
        return None

    metadata = ToolMetadata(
        name="my_tool", description="...", category=ToolCategory.SEARCH
    )
    decorated = registry.register(metadata)(my_tool)
    assert callable(decorated)


def test_tools_property_returns_a_copy(
    registry: FastMCPToolRegistry,
) -> None:
    """Mutating the returned dict must not affect the registry's internal state."""

    async def my_tool() -> None:
        return None

    metadata = ToolMetadata(
        name="my_tool", description="...", category=ToolCategory.SEARCH
    )
    registry.register(metadata)(my_tool)
    snapshot = registry.tools
    snapshot.pop("my_tool")  # mutate the snapshot
    assert "my_tool" in registry.tools  # original is intact


def test_register_overwrites_existing_name(
    registry: FastMCPToolRegistry, app: FakeApp
) -> None:
    """Re-registering under the same name replaces the prior registration."""
    metadata = ToolMetadata(
        name="my_tool", description="v1", category=ToolCategory.SYSTEM
    )

    async def v1() -> None:
        return None

    async def v2() -> None:
        return None

    registry.register(metadata)(v1)
    assert registry.tools["my_tool"].coroutine is v1

    metadata_v2 = ToolMetadata(
        name="my_tool", description="v2", category=ToolCategory.SYSTEM
    )
    registry.register(metadata_v2)(v2)
    assert registry.tools["my_tool"].coroutine is v2
    assert registry.tools["my_tool"].metadata.description == "v2"


# ---------------------------------------------------------------------------
# ToolMetadata defaults
# ---------------------------------------------------------------------------


def test_tool_metadata_default_examples_is_empty_list() -> None:
    """Mutable defaults must be factory-isolated — no shared list across instances."""
    m1 = ToolMetadata(name="a", description="d", category=ToolCategory.SYSTEM)
    m2 = ToolMetadata(name="b", description="d", category=ToolCategory.SYSTEM)
    assert m1.examples == []
    assert m2.examples == []
    # Mutating one must not affect the other.
    m1.examples.append({"input": 1})
    assert m2.examples == []


def test_tool_metadata_default_is_async_is_true() -> None:
    """Async-first is the default — most Akosha tools are coroutines."""
    m = ToolMetadata(name="a", description="d", category=ToolCategory.SYSTEM)
    assert m.is_async is True


# ---------------------------------------------------------------------------
# ToolRegistration
# ---------------------------------------------------------------------------


def test_tool_registration_dataclass_carries_all_three_fields() -> None:
    """The slots-dataclass exposes metadata + coroutine + decorated."""
    metadata = ToolMetadata(
        name="t", description="d", category=ToolCategory.GRAPH
    )
    coroutine = lambda: None  # placeholder; not used in this test
    decorated = MagicMock()
    reg = ToolRegistration(metadata=metadata, coroutine=coroutine, decorated=decorated)
    assert reg.metadata is metadata
    assert reg.coroutine is coroutine
    assert reg.decorated is decorated
