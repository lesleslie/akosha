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
    mode: Annotated[
        str, typer.Option("--mode", "-m", help="Operational mode (lite|standard)")
    ] = "lite",
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

    Args:
        mode: Operational mode (lite or standard)
        verbose: Enable verbose logging
    """
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    logger.info(f"Initializing Akosha application in {mode} mode...")

    # Import only when needed to avoid import errors
    from akosha.main import AkoshaApplication

    # Initialize application with mode
    akosha_app = AkoshaApplication(mode=mode)

    logger.info("Starting Akosha admin shell...")

    # Import shell only when needed
    try:
        from akosha.shell import AkoshaShell

        # Create and start shell
        shell_instance = AkoshaShell(akosha_app)
        shell_instance.start()
    except ImportError as e:
        logger.error(f"Failed to import shell: {e}")
        logger.error("Admin shell requires optional dependencies")
        logger.error("Install with: pip install ipython")
        sys.exit(1)


@app.command()
def start(
    host: Annotated[str, typer.Option("--host", "-h", help="Host to bind to")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p", help="Port to bind to")] = 8682,
    mode: Annotated[
        str, typer.Option("--mode", "-m", help="Operational mode (lite|standard)")
    ] = "lite",
    config: Annotated[
        str, typer.Option("--config", "-c", help="Path to configuration file")
    ] = "",
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Enable verbose output")
    ] = False,
) -> None:
    """Start Akosha MCP server in the specified mode.

    Launches the Akosha MCP server with FastMCP framework using
    streamable-http transport for proper MCP protocol support.

    MODES:
        lite: Zero external dependencies, in-memory only
        standard: Full production config with Redis and cloud storage

    Examples:
        # Start in lite mode (default)
        akosha start

        # Start in standard mode with Redis
        akosha start --mode=standard

        # Start with custom host and port
        akosha start --host 0.0.0.0 --port 9000 --mode=standard

        # Start with custom config file
        akosha start --mode=standard --config /path/to/config.yaml

    Args:
        host: Host to bind to
        port: Port to bind to
        mode: Operational mode (lite or standard)
        config: Path to custom configuration file
        verbose: Enable verbose logging
    """
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Validate mode
    valid_modes = ["lite", "standard"]
    if mode not in valid_modes:
        typer.echo(f"❌ Invalid mode: {mode}", err=True)
        typer.echo(f"   Valid modes: {', '.join(valid_modes)}", err=True)
        raise typer.Exit(code=1)

    logger.info(f"Starting Akosha MCP server in {mode} mode on {host}:{port}")

    # Load custom config if provided
    config_dict: dict[str, Any] = {}
    if config:
        config_path = Path(config)
        if not config_path.exists():
            typer.echo(f"❌ Configuration file not found: {config}", err=True)
            raise typer.Exit(code=1)

        try:
            import yaml

            with config_path.open("r") as f:
                config_dict = yaml.safe_load(f)
            logger.info(f"Loaded configuration from {config}")
        except ImportError:
            typer.echo("⚠️  PyYAML not installed, ignoring config file", err=True)
        except Exception as e:
            typer.echo(f"⚠️  Failed to load config: {e}", err=True)

    # Initialize mode
    try:
        from akosha.modes import get_mode

        mode_instance = get_mode(mode, config=config_dict)
        logger.info(f"Initialized {mode} mode: {mode_instance}")
    except ValueError as e:
        typer.echo(f"❌ {e}", err=True)
        raise typer.Exit(code=1)
    except Exception as e:
        typer.echo(f"❌ Failed to initialize mode: {e}", err=True)
        logger.exception("Mode initialization failed")
        raise typer.Exit(code=1)

    # Import the MCP app
    from akosha.mcp import create_app

    # Create and run the MCP server with streamable-http transport
    app_instance = create_app(mode=mode_instance)

    logger.info(f"✅ Akosha ready in {mode} mode")
    logger.info(f"   Mode: {mode_instance.mode_config.description}")
    logger.info(f"   External services required: {mode_instance.requires_external_services}")

    app_instance.run(transport="streamable-http", host=host, port=port, path="/mcp")


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
    typer.echo("Component Type: diviner (reveals hidden patterns)")
    typer.echo("Adapters: vector_db, graph_db, analytics, alerting")
    typer.echo("")
    typer.echo("Operational Modes:")
    typer.echo("  - lite: Zero dependencies, in-memory only")
    typer.echo("  - standard: Full production with Redis and cloud storage")
    typer.echo("")
    typer.echo("For more information, see: https://github.com/yourusername/akosha")


@app.command()
def modes() -> None:
    """List available operational modes."""
    from akosha.modes import list_modes, get_mode

    typer.echo("Available operational modes:")
    typer.echo("")

    for mode_name in list_modes():
        mode_instance = get_mode(mode_name, config={})
        typer.echo(f"  {mode_name}:")
        typer.echo(f"    Description: {mode_instance.mode_config.description}")
        typer.echo(f"    Redis: {'Enabled' if mode_instance.mode_config.redis_enabled else 'Disabled'}")
        typer.echo(f"    Cold Storage: {'Enabled' if mode_instance.mode_config.cold_storage_enabled else 'Disabled'}")
        typer.echo(f"    Cache Backend: {mode_instance.mode_config.cache_backend}")
        typer.echo(f"    External Services: {'Required' if mode_instance.requires_external_services else 'None'}")
        typer.echo("")


def main() -> None:
    """Main CLI entry point."""
    app()


if __name__ == "__main__":
    main()
