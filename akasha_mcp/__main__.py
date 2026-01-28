"""Entry point for running akasha-mcp server."""

from akasha_mcp.main import create_app

if __name__ == "__main__":
    import uvicorn

    app = create_app()

    uvicorn.run(
        app.http_app,
        host="127.0.0.1",
        port=3002,
        log_level="info",
    )
