"""Akosha MCP Server - Universal Memory Aggregation via Model Context Protocol."""

import typing as t

# Re-export the package's runtime-resolved version so MCP-protocol
# handshakes report the same value as ``akosha.__version__``. The
# single source of truth is :file:`pyproject.toml` via
# :func:`importlib.metadata.version`.
from akosha import __version__  # noqa: F401  (re-export)

from akosha.mcp.server import APP_NAME, APP_VERSION, create_app

__all__ = [
    "APP_NAME",
    "APP_VERSION",
    "__version__",
    "create_app",
]


# Lazy initialization pattern - expose http_app from server module
def __getattr__(name: str) -> t.Any:
    """Lazy attribute access for http_app."""
    if name == "http_app":
        from akosha.mcp.server import create_app

        return create_app().http_app()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
