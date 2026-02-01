"""Entry point for running Akosha MCP server."""

if __name__ == "__main__":
    import uvicorn

    from akosha.mcp import create_app

    app = create_app()

    uvicorn.run(
        app.http_app,
        host="127.0.0.1",
        port=3002,
        log_level="info",
    )
