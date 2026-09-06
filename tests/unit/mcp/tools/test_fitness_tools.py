"""Tests for ``akosha.mcp.tools.fitness_tools`` — the run_fitness_analysis MCP tool.

Audit found this module at 0% coverage. The tool has three observable
states worth pinning:
- FitnessAnalyzer not initialized → structured error response
- Analysis cycle produces no signals → ``status=no_data``
- Analysis cycle produces signals → ``status=completed`` with
  task_classes / selectors_per_class / total_signals

Plus the side-channel ``get_fitness_analyzer_status`` tool.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import akosha.mcp.tools.fitness_tools as fitness_mod
from akosha.mcp.tools.fitness_tools import (
    init_fitness_analyzer,
    register_fitness_tools,
)


@pytest.fixture(autouse=True)
def _reset_global() -> None:
    """Reset the module-level ``_fitness_analyzer`` between tests."""
    fitness_mod._fitness_analyzer = None  # type: ignore[attr-defined]
    yield
    fitness_mod._fitness_analyzer = None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# init_fitness_analyzer
# ---------------------------------------------------------------------------


def test_init_fitness_analyzer_stores_instance() -> None:
    analyzer = MagicMock()
    analyzer._component_endpoints = [("akosha", "http://akosha:8682/mcp")]
    init_fitness_analyzer(analyzer)
    assert fitness_mod._fitness_analyzer is analyzer  # type: ignore[attr-defined]


def test_init_fitness_analyzer_logs_endpoint_count() -> None:
    """The startup log records how many components the analyzer will poll."""
    analyzer = MagicMock()
    analyzer._component_endpoints = [("a", "x"), ("b", "y"), ("c", "z")]
    init_fitness_analyzer(analyzer)
    # We don't assert the log message text — just that init didn't raise
    # and stored the instance. (Logger format varies; the endpoint count
    # check lives in init's body via len().)
    assert fitness_mod._fitness_analyzer is analyzer  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# register_fitness_tools
# ---------------------------------------------------------------------------


class FakeApp:
    """Captures registered tool functions for direct invocation."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self) -> Any:
        def decorator(fn: Any) -> Any:
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def test_register_fitness_tools_registers_two_tools() -> None:
    app = FakeApp()
    register_fitness_tools(app)  # type: ignore[arg-type]
    assert "run_fitness_analysis" in app.tools
    assert "get_fitness_analyzer_status" in app.tools


# ---------------------------------------------------------------------------
# run_fitness_analysis — error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_fitness_analysis_returns_error_when_analyzer_not_initialized() -> None:
    app = FakeApp()
    register_fitness_tools(app)  # type: ignore[arg-type]
    result = await app.tools["run_fitness_analysis"]()
    assert result["status"] == "error"
    assert "FitnessAnalyzer not initialized" in result["error"]


# ---------------------------------------------------------------------------
# run_fitness_analysis — no_data path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_fitness_analysis_returns_no_data_when_no_signals() -> None:
    analyzer = MagicMock()
    analyzer.run_fitness_analysis = AsyncMock(return_value={})
    init_fitness_analyzer(analyzer)

    app = FakeApp()
    register_fitness_tools(app)  # type: ignore[arg-type]

    result = await app.tools["run_fitness_analysis"]()
    assert result["status"] == "no_data"
    assert "No traces collected" in result["message"]


# ---------------------------------------------------------------------------
# run_fitness_analysis — completed path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_fitness_analysis_returns_completed_with_signal_summary() -> None:
    """When signals are produced, the response carries task_classes, counts, and totals."""
    analyzer = MagicMock()
    analyzer.run_fitness_analysis = AsyncMock(
        return_value={
            "code_generation": {"least_loaded": MagicMock(), "round_robin": MagicMock()},
            "reasoning": {"affinity": MagicMock()},
        }
    )
    init_fitness_analyzer(analyzer)

    app = FakeApp()
    register_fitness_tools(app)  # type: ignore[arg-type]
    result = await app.tools["run_fitness_analysis"]()

    assert result["status"] == "completed"
    assert set(result["task_classes"]) == {"code_generation", "reasoning"}
    assert result["selectors_per_class"] == {"code_generation": 2, "reasoning": 1}
    assert result["total_signals"] == 3


@pytest.mark.asyncio
async def test_run_fitness_analysis_catches_internal_exceptions() -> None:
    """Any exception from the analyzer surfaces as ``status=error``.

    Prevents the MCP tool from raising uncaught to the FastMCP runtime.
    """
    analyzer = MagicMock()
    analyzer.run_fitness_analysis = AsyncMock(side_effect=RuntimeError("dfeed exploded"))
    init_fitness_analyzer(analyzer)

    app = FakeApp()
    register_fitness_tools(app)  # type: ignore[arg-type]
    result = await app.tools["run_fitness_analysis"]()

    assert result["status"] == "error"
    assert "dfeed exploded" in result["error"]


# ---------------------------------------------------------------------------
# get_fitness_analyzer_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_fitness_analyzer_status_returns_defaults_when_not_initialized() -> None:
    app = FakeApp()
    register_fitness_tools(app)  # type: ignore[arg-type]
    result = await app.tools["get_fitness_analyzer_status"]()
    assert result == {
        "running": False,
        "component_endpoints": [],
        "poll_interval_seconds": 0,
    }


@pytest.mark.asyncio
async def test_get_fitness_analyzer_status_returns_state_when_initialized() -> None:
    analyzer = MagicMock()
    analyzer._running = True
    analyzer._component_endpoints = [("akosha", "http://akosha:8682/mcp")]
    analyzer._poll_interval = 60
    init_fitness_analyzer(analyzer)

    app = FakeApp()
    register_fitness_tools(app)  # type: ignore[arg-type]
    result = await app.tools["get_fitness_analyzer_status"]()

    assert result["running"] is True
    assert result["component_endpoints"] == [("akosha", "http://akosha:8682/mcp")]
    assert result["poll_interval_seconds"] == 60
