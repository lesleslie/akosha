"""Akosha CLI entry point.

Provides command-line interface for Akosha operations including
starting the admin shell, running services, and managing configuration.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import typer
from typing_extensions import Annotated

from akosha.main import AkoshaApplication
from akosha.shell import AkoshaShell

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Create CLI app
app = typer.Typer(
    name="akosha",
    help="Akosha - Universal Memory Aggregation System for distributed intelligence",
    add_completion=False,
)


@app.command()
def shell(
    ctx: typer.Context,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Enable verbose output")
    ] = False,
) -> None:
    """Launch Akosha admin shell for distributed intelligence operations.

    The admin shell provides an interactive IPython environment with access to:
    - aggregate() - Aggregate across systems
    - search() - Search distributed memory
    - detect() - Detect anomalies
    - graph() - Query knowledge graph
    - trends() - Analyze trends

    Session tracking is automatically enabled via Session-Buddy MCP.
    """
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    logger.info("Initializing Akosha application...")

    # Initialize application
    akosha_app = AkoshaApplication()

    logger.info("Starting Akosha admin shell...")

    # Create and start shell
    shell_instance = AkoshaShell(akosha_app)
    shell_instance.start()


@app.command()
def start(
    host: Annotated[str, typer.Option("--host", "-h", help="Host to bind to")] = "0.0.0.0",
    port: Annotated[int, typer.Option("--port", "-p", help="Port to bind to")] = 8000,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Enable verbose output")
    ] = False,
) -> None:
    """Start Akosha server.

    Launches the Akosha web server for API access and distributed
    memory aggregation.
    """
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    logger.info(f"Starting Akosha server on {host}:{port}")

    # TODO: Implement FastAPI server startup
    logger.info("Server startup not yet implemented")
    sys.exit(1)


@app.command()
def version() -> None:
    """Show Akosha version information."""
    try:
        import importlib.metadata

        ver = importlib.metadata.version("akosha")
        typer.echo(f"Akosha version: {ver}")
    except Exception:
        typer.echo("Akosha version: unknown")


@app.command()
def info() -> None:
    """Show Akosha system information."""
    typer.echo("Akosha - Universal Memory Aggregation System")
    typer.echo("")
    typer.echo("Component Type: soothsayer (reveals hidden patterns)")
    typer.echo("Adapters: vector_db, graph_db, analytics, alerting")
    typer.echo("")
    typer.echo("For more information, see: https://github.com/yourusername/akosha")


def main() -> None:
    """Main CLI entry point."""
    app()


if __name__ == "__main__":
    main()
