"""Shared FastMCP test double.

``akosha.mcp.server.create_app()`` registers routes via ``@app.custom_route``
and tools via ``@app.tool``. Any stand-in for ``FastMCP`` therefore has to
implement both or ``create_app()`` raises ``AttributeError`` partway through
construction.

This module is the single definition for that stand-in. It previously existed
as two byte-identical copies in ``tests/unit/test_mcp_server_lifespan.py`` and
``tests/unit/test_wave5_lifespan_wiring.py``. Both predate the ``@app.tool()``
registration at ``akosha/mcp/server.py:1471`` (the ``get_health`` MCP tool), so
neither had a ``tool()`` method and 15 tests failed at
``AttributeError: 'DummyFastMCP' object has no attribute 'tool'`` the moment
``create_app()`` reached that decorator.

Two copies of a test double is the same failure mode as two copies of a
production model: they drift, and only the one that is exercised tells you.
Hence one module here rather than a third inline definition.

Used by:

- ``tests/unit/test_mcp_server_lifespan.py``
- ``tests/unit/test_wave5_lifespan_wiring.py``
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["DummyFastMCP", "make_fastmcp_factory"]


class DummyFastMCP:
    """Minimal FastMCP stand-in that records registered tools and routes."""

    def __init__(self, name: str = "", version: str = "", lifespan: Any = None) -> None:
        self.name = name
        self.version = version
        # The lifespan is stored on both the public path (constructor kwarg)
        # and the legacy `_mcp_server` path that existing tests still read.
        self.lifespan = lifespan
        self._mcp_server = SimpleNamespace(lifespan=lifespan)
        self.routes: dict[str, object] = {}
        self.registered_tools: dict[str, Callable[..., object]] = {}

    def tool(self, *_args: Any, name: str | None = None, **_kwargs: Any):
        """Mirror FastMCP's decorator-factory tool registration.

        Supports both ``@app.tool()`` and ``@app.tool(name="...")``. Returns
        the undecorated function so the decorated name stays usable in the
        module that registered it.
        """

        def decorator(fn: Callable[..., object]) -> Callable[..., object]:
            self.registered_tools[name if name else fn.__name__] = fn
            return fn

        return decorator

    def custom_route(self, path: str, methods: list[str]):
        """Mirror FastMCP's ``custom_route`` decorator-factory."""

        def decorator(func: Callable[..., object]) -> Callable[..., object]:
            self.routes[path] = {"methods": methods, "handler": func}
            return func

        return decorator

    def http_app(self) -> str:
        """Stand in for FastMCP's ASGI app factory."""
        return "http-app"


def make_fastmcp_factory(
    monkeypatch: Any,
    app_name: str,
    app_version: str,
) -> DummyFastMCP:
    """Patch ``akosha.mcp.server.FastMCP`` to hand back one stable dummy.

    ``create_app()`` calls ``FastMCP(name, version, lifespan=lifespan)``. The
    factory returns the SAME instance every call and back-propagates the
    lifespan onto it, so a test holding the returned object sees the lifespan
    ``create_app()`` passed in — including tests that reach it through the
    legacy ``app._mcp_server.lifespan`` access path.
    """
    app = DummyFastMCP(app_name, app_version)

    def factory(name: str, version: str, lifespan: Any = None) -> DummyFastMCP:
        app.lifespan = lifespan
        app._mcp_server.lifespan = lifespan
        return app

    monkeypatch.setattr("akosha.mcp.server.FastMCP", factory)
    return app
