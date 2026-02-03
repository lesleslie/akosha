"""Akosha MCP Server - Universal Memory Aggregation via Model Context Protocol."""

__version__ = "0.1.0"

from akosha.mcp.server import APP_NAME, APP_VERSION, create_app

__all__ = [
    "APP_NAME",
    "APP_VERSION",
    "__version__",
    "create_app",
]


# Lazy initialization pattern - expose http_app from server module
def __getattr__(name: str):
    """Lazy attribute access for http_app."""
    if name == "http_app":
        from akosha.mcp.server import create_app

        return create_app().http_app()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
