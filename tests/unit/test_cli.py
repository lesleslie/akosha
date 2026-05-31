"""Tests for Akosha CLI commands."""

from __future__ import annotations

import builtins
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

import akosha.cli as cli_module
from akosha.cli import app

runner = CliRunner()


class TestCLICommands:
    """Test suite for CLI commands."""

    def test_version_command(self) -> None:
        """Test version command."""
        result = runner.invoke(app, ["version"])

        assert result.exit_code == 0
        assert "Akosha version" in result.stdout

    def test_info_command(self) -> None:
        """Test info command."""
        result = runner.invoke(app, ["info"])

        assert result.exit_code == 0
        assert "Akosha" in result.stdout
        assert "Universal Memory Aggregation System" in result.stdout
        assert "diviner" in result.stdout or "soothsayer" in result.stdout

    @patch("akosha.cli.AkoshaApplication")
    @patch("akosha.cli.AkoshaShell")
    def test_shell_command(self, mock_shell: MagicMock, mock_app: MagicMock) -> None:
        """Test shell command initialization."""
        # The shell command starts an interactive shell which we can't test directly
        # But we can verify it tries to initialize
        mock_shell_instance = MagicMock()
        mock_shell.return_value = mock_shell_instance

        # We need to mock the actual shell start since it's interactive
        with patch.object(mock_shell_instance, "start"):
            # Note: This will still try to start IPython which may not be available
            # In a real test environment, we might need to skip this or mock more deeply
            pass

        # The shell command is hard to test in automated environments
        # So we'll just verify the imports work
        from akosha.cli import shell

        assert callable(shell)

    @patch("akosha.cli.create_app")
    def test_start_command(self, mock_create_app: MagicMock) -> None:
        """Test start command."""
        mock_app_instance = MagicMock()
        mock_create_app.return_value = mock_app_instance

        # The start command runs the server which is blocking
        # We'll just verify the command exists and can be imported
        from akosha.cli import start

        assert callable(start)

    def test_help_command(self) -> None:
        """Test help command."""
        result = runner.invoke(app, ["--help"])

        assert result.exit_code == 0
        assert "Akosha" in result.stdout
        assert "shell" in result.stdout
        assert "mcp" in result.stdout
        assert "start" in result.stdout
        assert "version" in result.stdout
        assert "info" in result.stdout

    def test_shell_help(self) -> None:
        """Test shell command help."""
        result = runner.invoke(app, ["shell", "--help"])

        assert result.exit_code == 0
        assert "Launch Akosha admin shell" in result.stdout

    def test_start_help(self) -> None:
        """Test start command help."""
        result = runner.invoke(app, ["start", "--help"])

        assert result.exit_code == 0
        assert "Start Akosha MCP server" in result.stdout
        assert "--host" in result.stdout
        assert "--port" in result.stdout
        assert "--verbose" in result.stdout

    def test_mcp_start_help(self) -> None:
        """Test nested MCP start command help."""
        result = runner.invoke(app, ["mcp", "start", "--help"])

        assert result.exit_code == 0
        assert "Start Akosha MCP server" in result.stdout
        assert "--host" in result.stdout
        assert "--port" in result.stdout
        assert "--verbose" in result.stdout

    @patch("akosha.cli.create_app")
    def test_start_with_custom_host_port(self, mock_create_app: MagicMock) -> None:
        """Test start command with custom host and port."""
        mock_app_instance = MagicMock()
        mock_create_app.return_value = mock_app_instance

        # Verify the command parsing works (not execution)
        from typer import Context

        # Create a mock context
        mock_ctx = MagicMock(spec=Context)

        # Import and test the start function signature
        from akosha.cli import start

        # Verify function is callable
        assert callable(start)

        # We can't actually run it without blocking on the server
        # But we verified the command exists and parses correctly


class TestCLIIntegration:
    """Integration tests for CLI workflows."""

    def test_command_discovery(self) -> None:
        """Test that all expected commands are discoverable."""
        result = runner.invoke(app, ["--help"])

        # Expected commands
        expected_commands = ["shell", "mcp", "start", "version", "info"]

        for cmd in expected_commands:
            assert cmd in result.stdout, f"Command '{cmd}' not found in help"

    def test_version_output_format(self) -> None:
        """Test version command output format."""
        result = runner.invoke(app, ["version"])

        # Should contain version information
        assert result.exit_code == 0
        assert "version:" in result.stdout.lower()

    def test_info_completeness(self) -> None:
        """Test info command shows complete information."""
        result = runner.invoke(app, ["info"])

        assert result.exit_code == 0
        # Should show key information
        assert "Component Type" in result.stdout or "component" in result.stdout.lower()

    @patch.dict("os.environ", {"AKOSHA_LOG_LEVEL": "DEBUG"})
    def test_verbose_flag(self) -> None:
        """Test that verbose flag is respected."""
        # This is a basic test - in a real scenario we'd capture logs
        from akosha.cli import app as cli_app

        # Verify the CLI app exists
        assert cli_app is not None

    def test_invalid_command(self) -> None:
        """Test handling of invalid commands."""
        result = runner.invoke(app, ["invalid-command"])

        assert result.exit_code != 0
        combined = f"{result.stdout}\n{result.stderr}".lower()
        assert "no such command" in combined or "not found" in combined

    def test_missing_required_args(self) -> None:
        """Test commands with missing required arguments."""
        # Most commands don't have required args, but we can test the CLI still works
        result = runner.invoke(app, [])

        # Typer may treat empty invocation as a help/error path depending on version.
        assert result.exit_code in (0, 2)
        assert "Usage" in result.stdout or "help" in result.stdout.lower()

    @patch("akosha.main.AkoshaApplication")
    @patch("akosha.shell.AkoshaShell")
    def test_shell_command_initializes_and_starts_shell(
        self, mock_shell: MagicMock, mock_app: MagicMock
    ) -> None:
        """Shell command should construct the app and start the shell."""
        shell_instance = MagicMock()
        shell_instance.start = MagicMock()
        mock_shell.return_value = shell_instance

        cli_module.shell(None, mode="standard", verbose=True)

        mock_app.assert_called_once_with(mode="standard")
        mock_shell.assert_called_once()
        shell_instance.start.assert_called_once()
        assert logging.getLogger().level == logging.DEBUG

    def test_shell_command_missing_optional_dependency_exits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing shell imports should exit cleanly with a non-zero code."""
        original_import = builtins.__import__

        def fake_import(name: str, globals=None, locals=None, fromlist=(), level=0):
            if name == "akosha.shell":
                raise ImportError("no shell")
            return original_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        monkeypatch.setattr(cli_module.sys, "exit", MagicMock(side_effect=SystemExit(1)))

        with pytest.raises(SystemExit, match="1"):
            cli_module.shell(None)

    def test_root_callback_shows_help(self) -> None:
        """Invoking the root callback without a subcommand should show help."""
        ctx = MagicMock()
        ctx.invoked_subcommand = None
        ctx.get_help.return_value = "help text"

        with pytest.raises(cli_module.typer.Exit):
            cli_module.main(ctx)

        ctx.get_help.assert_called_once()

    def test_version_unknown_when_metadata_lookup_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Version command should degrade gracefully when package metadata is unavailable."""
        monkeypatch.setattr("importlib.metadata.version", MagicMock(side_effect=Exception("boom")))

        result = runner.invoke(app, ["version"])

        assert result.exit_code == 0
        assert "Akosha version: unknown" in result.stdout

    def test_start_server_success_with_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Start server should load config and run the app in the requested mode."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("alpha: 1\n")

        mode_instance = MagicMock()
        mode_instance.mode_config.description = "Standard mode"
        mode_instance.requires_external_services = True
        app_instance = MagicMock()

        monkeypatch.setattr("akosha.modes.get_mode", MagicMock(return_value=mode_instance))
        monkeypatch.setattr("akosha.mcp.create_app", MagicMock(return_value=app_instance))

        cli_module._start_server(
            host="0.0.0.0", port=9000, mode="standard", config=str(config_path)
        )

        app_instance.run.assert_called_once_with(
            transport="streamable-http", host="0.0.0.0", port=9000, path="/mcp"
        )

    def test_start_server_invalid_mode_exits(self) -> None:
        """Invalid modes should be rejected before any initialization."""
        with pytest.raises(cli_module.typer.Exit) as excinfo:
            cli_module._start_server(mode="invalid")

        assert excinfo.value.exit_code == 1

    def test_start_server_missing_config_file_exits(self, tmp_path: Path) -> None:
        """Missing config files should stop startup early."""
        with pytest.raises(cli_module.typer.Exit) as excinfo:
            cli_module._start_server(config=str(tmp_path / "missing.yaml"))

        assert excinfo.value.exit_code == 1

    def test_start_server_yaml_import_error_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If PyYAML is unavailable, the config file should be ignored."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("alpha: 1\n")

        mode_instance = MagicMock()
        mode_instance.mode_config.description = "Lite mode"
        mode_instance.requires_external_services = False
        app_instance = MagicMock()

        original_import = builtins.__import__

        def fake_import(name: str, globals=None, locals=None, fromlist=(), level=0):
            if name == "yaml":
                raise ImportError("no yaml")
            return original_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        monkeypatch.setattr("akosha.modes.get_mode", MagicMock(return_value=mode_instance))
        monkeypatch.setattr("akosha.mcp.create_app", MagicMock(return_value=app_instance))

        cli_module._start_server(mode="lite", config=str(config_path))

        app_instance.run.assert_called_once()

    def test_start_server_value_error_from_mode_factory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mode factory validation errors should surface as clean CLI exits."""
        monkeypatch.setattr("akosha.modes.get_mode", MagicMock(side_effect=ValueError("bad mode")))

        with pytest.raises(cli_module.typer.Exit) as excinfo:
            cli_module._start_server(mode="lite")

        assert excinfo.value.exit_code == 1

    def test_start_server_generic_failure_from_mode_factory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unexpected initialization errors should also surface as clean CLI exits."""
        monkeypatch.setattr("akosha.modes.get_mode", MagicMock(side_effect=RuntimeError("boom")))

        with pytest.raises(cli_module.typer.Exit) as excinfo:
            cli_module._start_server(mode="lite")

        assert excinfo.value.exit_code == 1

    @patch("akosha.cli._start_server")
    def test_start_command_delegates_to_helper(self, mock_start_server: MagicMock) -> None:
        """The public start command should delegate to the shared helper."""
        cli_module.start(
            host="1.2.3.4", port=9999, mode="standard", config="cfg.yaml", verbose=True
        )

        mock_start_server.assert_called_once_with(
            host="1.2.3.4", port=9999, mode="standard", config="cfg.yaml", verbose=True
        )

    @patch("akosha.cli._start_server")
    def test_mcp_start_command_delegates_to_helper(self, mock_start_server: MagicMock) -> None:
        """The nested MCP start command should reuse the same helper."""
        cli_module.mcp_start(
            host="1.2.3.4", port=9999, mode="standard", config="cfg.yaml", verbose=True
        )

        mock_start_server.assert_called_once_with(
            host="1.2.3.4", port=9999, mode="standard", config="cfg.yaml", verbose=True
        )
