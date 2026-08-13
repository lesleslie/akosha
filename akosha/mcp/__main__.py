"""Entry point for running Akosha MCP server."""

if __name__ == "__main__":
    import os

    import uvicorn

    from akosha.config import DEFAULT_MCP_PORT
    from akosha.mcp import create_app

    app = create_app()

    # Get port from environment variable or use default
    port = int(os.getenv("MCP_PORT", str(DEFAULT_MCP_PORT)))
    host = os.getenv("MCP_HOST", "127.0.0.1")

    uvicorn.run(
        app.http_app,
        host=host,
        port=port,
        log_level="info",
    )
