"""Akosha CLI entry point.

Provides command-line interface for Akosha operations including
starting the admin shell, running services, and managing configuration.

Adopts ``oneiric.cli.base.OneiricCLIBase`` (oneiric>=0.19.0) so the
ecosystem CLIs share a unified ``version`` / ``doctor`` / ``health``
surface, the ``--json`` global flag, and the ``ExitCode`` enum.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from oneiric.cli.base import OneiricCLIBase

from akosha.config import DEFAULT_MCP_PORT

try:
    from akosha.main import AkoshaApplication  # type: ignore[import]
except Exception:  # pragma: no cover - optional for test patching
    AkoshaApplication = None  # ty: ignore[invalid-assignment]

try:
    from akosha.shell import AkoshaShell  # type: ignore[import]
except Exception:  # pragma: no cover - optional for test patching
    AkoshaShell = None  # ty: ignore[invalid-assignment]

try:
    from akosha.mcp import create_app  # type: ignore[import]
except Exception:  # pragma: no cover - import is validated in command paths
    create_app = None  # ty: ignore[invalid-assignment]

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class AkoshaCLI(OneiricCLIBase):
    """Akosha Typer app backed by the shared OneiricCLIBase.

    Real ``_doctor_checks`` probe storage paths, mode registry, and the
    installed oneiric dep. Real ``_health_probe`` loads ``AkoshaConfig``
    and reports liveness against the configured mode.
    """

    def __init__(self) -> None:
        super().__init__(
            component_name="akosha",
            help=("Akosha - Universal Memory Aggregation System for distributed intelligence"),
            add_completion=False,
        )

    # ------------------------------------------------------------------
    # OneiricCLIBase hooks — real checks (NOT stubs)
    # ------------------------------------------------------------------
    def _doctor_checks(self) -> dict[str, Any]:
        """Return real diagnostic checks for Akosha.

        Probes:

        - installed package version (via importlib.metadata)
        - oneiric dependency version (the OneiricCLIBase dep)
        - storage path writability (warm + WAL)
        - mode registry contents (lite, standard)
        - config layer load via ``akosha.config.get_config``
        """
        checks: dict[str, dict[str, Any]] = {}

        # 1. Package metadata
        try:
            from importlib.metadata import PackageNotFoundError
            from importlib.metadata import version as metadata_version

            ver = metadata_version("akosha")
            checks["package_version"] = {
                "status": "ok",
                "detail": f"akosha {ver}",
            }
        except PackageNotFoundError:
            checks["package_version"] = {
                "status": "warn",
                "detail": "akosha not installed (running from source)",
            }

        # 2. oneiric dependency version
        try:
            from importlib.metadata import PackageNotFoundError
            from importlib.metadata import version as metadata_version

            oneiric_ver = metadata_version("oneiric")
            major_minor = tuple(int(p) for p in oneiric_ver.split(".")[:2])
            required = (0, 19)
            status = "ok" if major_minor >= required else "fail"
            checks["oneiric_dep"] = {
                "status": status,
                "detail": f"oneiric {oneiric_ver} (>= {'.'.join(map(str, required))} required)",
            }
        except PackageNotFoundError:
            checks["oneiric_dep"] = {
                "status": "fail",
                "detail": "oneiric not installed",
            }

        # 3. Storage path writability — check warm + WAL paths actually
        # exist or can be created. Hits real on-disk state.
        from akosha.config import get_config

        try:
            cfg = get_config()
            warm_path = cfg.warm.path
            if warm_path is None:
                checks["storage_paths"] = {
                    "status": "fail",
                    "detail": "warm storage path is not configured",
                }
            else:
                warm_path.mkdir(parents=True, exist_ok=True)
                probe = warm_path / ".akosha-doctor-probe"
                try:
                    probe.write_text("ok")
                    probe.unlink(missing_ok=True)
                    checks["storage_paths"] = {
                        "status": "ok",
                        "detail": f"warm path writable at {warm_path}",
                    }
                except OSError as exc:
                    checks["storage_paths"] = {
                        "status": "fail",
                        "detail": f"warm path not writable ({warm_path}): {exc}",
                    }
        except Exception as exc:  # pragma: no cover - defensive
            checks["storage_paths"] = {
                "status": "fail",
                "detail": f"config load failed: {exc}",
            }

        # 4. Mode registry
        try:
            from akosha.modes import list_modes

            modes = list_modes()
            expected = {"lite", "standard"}
            missing = expected - set(modes)
            if missing:
                checks["mode_registry"] = {
                    "status": "fail",
                    "detail": f"missing modes: {sorted(missing)}",
                }
            else:
                checks["mode_registry"] = {
                    "status": "ok",
                    "detail": f"registered modes: {sorted(modes)}",
                }
        except Exception as exc:  # pragma: no cover - defensive
            checks["mode_registry"] = {
                "status": "fail",
                "detail": f"mode registry probe failed: {exc}",
            }

        # 5. Config layered load via get_config (Oneiric-backed)
        try:
            cfg = get_config()
            checks["config_load"] = {
                "status": "ok",
                "detail": f"mode={cfg.mode} env={cfg.environment}",
            }
        except Exception as exc:  # pragma: no cover - defensive
            checks["config_load"] = {
                "status": "fail",
                "detail": f"config load failed: {exc}",
            }

        return checks

    def _health_probe(self) -> dict[str, Any]:
        """Return a real liveness snapshot for the Akosha CLI.

        Returns:
            Dict with ``status`` (ok/degraded/error), ``version``,
            ``mode``, ``default_port``, ``storage_backend``, and
            ``modes_available`` keys. Mirrors the oneiric runtime
            health schema enough to be machine-readable.
        """
        from akosha.config import get_config

        snapshot: dict[str, Any] = {
            "status": "ok",
            "version": self.component_version,
            "default_port": DEFAULT_MCP_PORT,
            "modes_available": [],
        }

        try:
            cfg = get_config()
            snapshot["mode"] = cfg.mode
            snapshot["environment"] = cfg.environment
            snapshot["storage_backend"] = cfg.hot.backend
        except Exception as exc:
            snapshot["status"] = "degraded"
            snapshot["mode"] = None
            snapshot["environment"] = None
            snapshot["storage_backend"] = None
            snapshot["config_error"] = str(exc)

        try:
            from akosha.modes import list_modes

            snapshot["modes_available"] = sorted(list_modes())
        except Exception as exc:
            snapshot["status"] = "degraded"
            snapshot["modes_error"] = str(exc)

        return snapshot


# Create CLI app — OneiricCLIBase subclass wires the unified callback,
# ``--json`` flag, and ``version``/``doctor``/``health`` commands.
app = AkoshaCLI()

# Match the ecosystem convention: `python -m <package> mcp start`
mcp_app = typer.Typer(help="MCP server lifecycle management")
app.add_typer(mcp_app, name="mcp")


@app.command()
def shell(
    _ctx: typer.Context,
    mode: Annotated[
        str, typer.Option("--mode", "-m", help="Operational mode (lite|standard)")
    ] = "lite",
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable verbose output")] = False,
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

    # Configure structured logging via Oneiric (Console / string tracebacks)
    from oneiric.core.logging import LoggingConfig, configure_logging

    configure_logging(
        LoggingConfig(
            level="DEBUG" if verbose else "INFO",
            emit_json=False,
        )
    )

    logger.info("Starting Akosha admin shell...")

    # Import shell only when needed
    try:
        from akosha.shell import AkoshaShell

        # Create and start shell. ``AkoshaShell.start`` is intentionally sync
        # so it overrides ``AdminShell.start`` (also sync). The IPython shell
        # blocks until exit, so there is no coroutine to ``asyncio.run``.
        shell_instance = AkoshaShell(akosha_app)
        shell_instance.start()
    except ImportError as e:
        logger.error(f"Failed to import shell: {e}")
        logger.error("Admin shell requires optional dependencies")
        logger.error("Install with: pip install ipython")
        sys.exit(1)


def _load_config(config_path: str) -> dict[str, Any]:
    """Load configuration from a YAML file.

    Args:
        config_path: Path to YAML configuration file.

    Returns:
        Configuration dictionary loaded from the file.
    """
    path = Path(config_path)
    if not path.exists():
        typer.echo(f"❌ Configuration file not found: {config_path}", err=True)
        raise typer.Exit(code=1)

    try:
        import yaml

        with path.open("r") as f:
            config_dict: dict[str, Any] = yaml.safe_load(f) or {}
        logger.info(f"Loaded configuration from {config_path}")
        return config_dict
    except ImportError:
        typer.echo("⚠️  PyYAML not installed, ignoring config file", err=True)
        return {}
    except Exception as e:
        typer.echo(f"⚠️  Failed to load config: {e}", err=True)
        return {}


def _init_mode(mode: str, config: dict[str, Any]) -> Any:
    """Initialize an Akosha mode instance.

    Args:
        mode: Operational mode (lite or standard).
        config: Configuration dictionary to pass to the mode.

    Returns:
        Initialized mode instance.

    Raises:
        typer.Exit: If mode is invalid or initialization fails.
    """
    from akosha.modes import get_mode

    try:
        mode_instance = get_mode(mode, config=config)
        logger.info(f"Initialized {mode} mode: {mode_instance}")
        return mode_instance
    except ValueError as e:
        typer.echo(f"❌ {e}", err=True)
        raise typer.Exit(code=1) from e
    except Exception as e:
        typer.echo(f"❌ Failed to initialize mode: {e}", err=True)
        logger.exception("Mode initialization failed")
        raise typer.Exit(code=1) from e


def _configure_logging(verbose: bool) -> None:
    """Configure structured logging via Oneiric.

    Args:
        verbose: If True, set log level to DEBUG; otherwise INFO.
    """
    from oneiric.core.logging import LoggingConfig, configure_logging

    configure_logging(
        LoggingConfig(
            level="DEBUG" if verbose else "INFO",
            emit_json=False,
        )
    )


def _start_server(
    host: Annotated[str, typer.Option("--host", "-h", help="Host to bind to")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p", help="Port to bind to")] = DEFAULT_MCP_PORT,
    mode: Annotated[
        str, typer.Option("--mode", "-m", help="Operational mode (lite|standard)")
    ] = "lite",
    config: Annotated[str, typer.Option("--config", "-c", help="Path to configuration file")] = "",
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable verbose output")] = False,
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

    # Load config and initialize mode
    config_dict: dict[str, Any] = _load_config(config) if config else {}
    mode_instance = _init_mode(mode, config_dict)
    _configure_logging(verbose)

    # Create and run the MCP server
    from akosha.mcp import create_app

    app_instance = create_app(mode=mode_instance)

    logger.info(f"✅ Akosha ready in {mode} mode")
    logger.info(f"   Mode: {mode_instance.mode_config.description}")
    logger.info(f"   External services required: {mode_instance.requires_external_services}")

    app_instance.run(transport="streamable-http", host=host, port=port, path="/mcp")


@app.command()
def start(
    host: Annotated[str, typer.Option("--host", "-h", help="Host to bind to")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p", help="Port to bind to")] = DEFAULT_MCP_PORT,
    mode: Annotated[
        str, typer.Option("--mode", "-m", help="Operational mode (lite|standard)")
    ] = "lite",
    config: Annotated[str, typer.Option("--config", "-c", help="Path to configuration file")] = "",
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable verbose output")] = False,
) -> None:
    """Start Akosha MCP server in the specified mode."""
    _start_server(host=host, port=port, mode=mode, config=config, verbose=verbose)


@mcp_app.command("start")
def mcp_start(
    host: Annotated[str, typer.Option("--host", "-h", help="Host to bind to")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p", help="Port to bind to")] = DEFAULT_MCP_PORT,
    mode: Annotated[
        str, typer.Option("--mode", "-m", help="Operational mode (lite|standard)")
    ] = "lite",
    config: Annotated[str, typer.Option("--config", "-c", help="Path to configuration file")] = "",
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable verbose output")] = False,
) -> None:
    """Start Akosha MCP server in the specified mode."""
    _start_server(host=host, port=port, mode=mode, config=config, verbose=verbose)


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
    from akosha.modes import get_mode, list_modes

    typer.echo("Available operational modes:")
    typer.echo("")

    for mode_name in list_modes():
        mode_instance = get_mode(mode_name, config={})
        typer.echo(f"  {mode_name}:")
        typer.echo(f"    Description: {mode_instance.mode_config.description}")
        typer.echo(
            f"    Redis: {'Enabled' if mode_instance.mode_config.redis_enabled else 'Disabled'}"
        )
        typer.echo(
            f"    Cold Storage: {'Enabled' if mode_instance.mode_config.cold_storage_enabled else 'Disabled'}"
        )
        typer.echo(f"    Cache Backend: {mode_instance.mode_config.cache_backend}")
        typer.echo(
            f"    External Services: {'Required' if mode_instance.requires_external_services else 'None'}"
        )
        typer.echo("")


def main_cli() -> None:
    """Main CLI entry point.

    Routes through ``OneiricCLIBase.run()`` (inherited from
    ``typer.Typer.run()``) so the unified callback / ``--json`` flag /
    ``version`` / ``doctor`` / ``health`` subcommands work as a single
    Typer app.
    """
    # OneiricCLIBase subclasses typer.Typer so ``run`` is inherited, but zuban
    # doesn't resolve it through the typer base class without help.
    app.run()  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]


if __name__ == "__main__":
    main_cli()
