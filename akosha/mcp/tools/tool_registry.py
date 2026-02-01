"""Lightweight registry to organise Akosha MCP tools."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastmcp import FastMCP


class ToolCategory(str, Enum):
    """High-level groupings for Akosha MCP tools."""

    SEARCH = "search"
    ANALYTICS = "analytics"
    GRAPH = "graph"
    INGESTION = "ingestion"
    SYSTEM = "system"


@dataclass(slots=True)
class ToolMetadata:
    """Metadata describing a registered tool."""

    name: str
    description: str
    category: ToolCategory
    examples: list[dict[str, Any]] = field(default_factory=list)
    is_async: bool = True


@dataclass(slots=True)
class ToolRegistration:
    metadata: ToolMetadata
    coroutine: Any
    decorated: Any


class FastMCPToolRegistry:
    """Register tools with FastMCP while tracking metadata."""

    def __init__(self, app: FastMCP) -> None:
        self._app = app
        self._tools: dict[str, ToolRegistration] = {}

    def register(self, metadata: ToolMetadata) -> Callable[[Any], Any]:
        def decorator(func: Any) -> Any:
            if not asyncio.iscoroutinefunction(func):
                raise TypeError("Akosha MCP tools must be async functions")

            decorated = self._app.tool(name=metadata.name, description=metadata.description)(func)
            self._tools[metadata.name] = ToolRegistration(
                metadata=metadata,
                coroutine=func,
                decorated=decorated,
            )
            return decorated

        return decorator

    @property
    def tools(self) -> dict[str, ToolRegistration]:
        return self._tools.copy()
