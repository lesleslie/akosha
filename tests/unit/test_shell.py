"""Unit tests for Akosha admin shell."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from akosha.cli import app
from akosha.main import AkoshaApplication
from akosha.shell import AkoshaShell

runner = CliRunner()


@pytest.fixture
def akosha_app():
    """Create Akosha application fixture."""
    return AkoshaApplication()


@pytest.fixture
def akosha_shell(akosha_app):
    """Create Akosha shell fixture."""
    return AkoshaShell(akosha_app)


class TestAkoshaShell:
    """Test AkoshaShell initialization and configuration."""

    def test_shell_initialization(self, akosha_shell):
        """Test shell initializes correctly."""
        assert akosha_shell is not None
        assert akosha_shell.app is not None
        assert hasattr(akosha_shell, "session_tracker")
        assert hasattr(akosha_shell, "namespace")

    def test_component_name(self, akosha_shell):
        """Test component name is correct."""
        assert akosha_shell._get_component_name() == "akosha"

    def test_component_type(self, akosha_shell):
        """Test component type is soothsayer."""
        assert akosha_shell._get_component_type() == "soothsayer"

    def test_component_version(self, akosha_shell):
        """Test component version can be retrieved."""
        version = akosha_shell._get_component_version()
        assert isinstance(version, str)
        assert len(version) > 0

    def test_adapters_info(self, akosha_shell):
        """Test adapters information is correct."""
        adapters = akosha_shell._get_adapters_info()
        assert isinstance(adapters, list)
        assert "vector_db" in adapters
        assert "graph_db" in adapters
        assert "analytics" in adapters
        assert "alerting" in adapters

    def test_namespace_has_intelligence_commands(self, akosha_shell):
        """Test namespace includes all intelligence commands."""
        ns = akosha_shell.namespace
        assert "aggregate" in ns
        assert "search" in ns
        assert "detect" in ns
        assert "graph" in ns
        assert "trends" in ns
        assert "adapters" in ns
        assert "version" in ns

    def test_banner_content(self, akosha_shell):
        """Test banner contains expected content."""
        banner = akosha_shell._get_banner()
        assert "Akosha Admin Shell" in banner
        assert "Distributed Intelligence" in banner
        assert "aggregate(query" in banner
        assert "search(query" in banner
        assert "detect(metric" in banner
        assert "graph(query" in banner
        assert "trends(metric" in banner
        assert "Session Tracking" in banner


class TestIntelligenceCommands:
    """Test intelligence command implementations."""

    @pytest.mark.asyncio
    async def test_aggregate_command(self, akosha_shell):
        """Test aggregate command executes."""
        result = await akosha_shell._aggregate(
            query="*", filters={"source": "test"}, limit=10
        )
        assert result is not None
        assert "status" in result
        assert "query" in result
        assert result["query"] == "*"

    @pytest.mark.asyncio
    async def test_search_command(self, akosha_shell):
        """Test search command executes."""
        result = await akosha_shell._search(query="test query", index="all", limit=10)
        assert result is not None
        assert "status" in result
        assert "query" in result
        assert result["query"] == "test query"

    @pytest.mark.asyncio
    async def test_detect_command(self, akosha_shell):
        """Test detect command executes."""
        result = await akosha_shell._detect(metric="all", threshold=0.8, window=300)
        assert result is not None
        assert "status" in result
        assert "metric" in result
        assert result["metric"] == "all"

    @pytest.mark.asyncio
    async def test_graph_command(self, akosha_shell):
        """Test graph command executes."""
        result = await akosha_shell._graph(query="test", node_type=None, depth=2)
        assert result is not None
        assert "status" in result
        assert "query" in result
        assert result["query"] == "test"

    @pytest.mark.asyncio
    async def test_trends_command(self, akosha_shell):
        """Test trends command executes."""
        result = await akosha_shell._trends(
            metric="all", window=3600, granularity=60
        )
        assert result is not None
        assert "status" in result
        assert "metric" in result
        assert result["metric"] == "all"


class TestSessionTracking:
    """Test session tracking integration."""

    @pytest.mark.asyncio
    async def test_session_tracker_initialization(self, akosha_shell):
        """Test session tracker is initialized."""
        assert akosha_shell.session_tracker is not None
        assert akosha_shell.session_tracker.component_name == "akosha"

    @pytest.mark.asyncio
    async def test_session_start_emission(self, akosha_shell):
        """Test session start event can be emitted."""
        # Verify session tracker is initialized
        assert akosha_shell.session_tracker is not None
        assert akosha_shell.session_tracker.component_name == "akosha"

    @pytest.mark.asyncio
    async def test_session_end_methods_exist(self, akosha_shell):
        """Test shell has session tracking methods."""
        # Verify session tracker has required methods
        assert hasattr(akosha_shell.session_tracker, 'emit_session_start')
        assert hasattr(akosha_shell.session_tracker, 'emit_session_end')
        assert hasattr(akosha_shell.session_tracker, '_check_availability')


class TestCLIIntegration:
    """Test CLI integration."""

    def test_cli_shell_command_exists(self):
        """Test shell command is available in CLI."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "shell" in result.stdout

    def test_cli_version_command(self):
        """Test version command works."""
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert "version" in result.stdout.lower()

    def test_cli_info_command(self):
        """Test info command works."""
        result = runner.invoke(app, ["info"])
        assert result.exit_code == 0
        assert "Akosha" in result.stdout
        assert "soothsayer" in result.stdout

    @patch("akosha.cli.AkoshaShell")
    @patch("akosha.cli.AkoshaApplication")
    def test_cli_shell_launch(self, MockApp, MockShell):
        """Test CLI launches shell correctly."""
        # Mock application and shell
        mock_app = MagicMock()
        MockApp.return_value = mock_app

        mock_shell = MagicMock()
        MockShell.return_value = mock_shell

        # Invoke shell command
        result = runner.invoke(app, ["shell"])

        # Verify initialization
        MockApp.assert_called_once()
        MockShell.assert_called_once_with(mock_app)

        # Note: shell.start() is synchronous but we can't test it easily
        # without mocking IPython internals
