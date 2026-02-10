"""Entry point for running Akosha MCP server."""

if __name__ == "__main__":
    import os

    import uvicorn

    from akosha.mcp import create_app

    app = create_app()

    # Get port from environment variable or use default
    port = int(os.getenv("MCP_PORT", "8682"))
    host = os.getenv("MCP_HOST", "127.0.0.1")

    uvicorn.run(
        app.http_app,
        host=host,
        port=port,
        log_level="info",
    )
