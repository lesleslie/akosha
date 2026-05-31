"""Fitness analysis tools for Akosha MCP server.

Provides the run_fitness_analysis MCP tool that triggers the FitnessAnalyzer
on demand, computing and persisting routing fitness signals for the Bodai
feedback loop.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Global FitnessAnalyzer instance, initialized during server startup
_fitness_analyzer: Any = None


def init_fitness_analyzer(analyzer: Any) -> None:
    """Initialize the global FitnessAnalyzer instance.

    Called during server startup after component endpoints are registered.

    Args:
        analyzer: FitnessAnalyzer instance with registered component endpoints
    """
    global _fitness_analyzer
    _fitness_analyzer = analyzer
    logger.info(
        "FitnessAnalyzer initialized with %d component endpoints",
        len(analyzer._component_endpoints),
    )


def register_fitness_tools(app: Any) -> None:
    """Register fitness analysis tools with the MCP server.

    Args:
        app: FastMCP application
    """

    @app.tool()
    async def run_fitness_analysis() -> dict[str, Any]:
        """Manually trigger one fitness analysis cycle.

        Polls all registered Bodai component endpoints for OTel traces,
        computes rolling fitness signals (failure_rate, p99 latency) per
        (task_class, selector) pair, and writes results to Dhara.

        This tool is used for on-demand analysis and testing of the Bodai
        feedback loop. Normally the FitnessAnalyzer runs as a background
        task on a 60-second interval.

        Returns:
            Dict with analysis results:
                {
                    "status": "completed" | "no_data",
                    "task_classes": list[str],
                    "selectors_per_class": dict[str, int],
                    "total_signals": int
                }
        """
        if _fitness_analyzer is None:
            return {
                "status": "error",
                "error": "FitnessAnalyzer not initialized. Server may still be starting up.",
            }

        try:
            signals = await _fitness_analyzer.run_fitness_analysis()

            if not signals:
                return {
                    "status": "no_data",
                    "message": "No traces collected in this cycle.",
                }

            task_classes = list(signals.keys())
            selectors_per_class = {tc: len(sels) for tc, sels in signals.items()}
            total_signals = sum(selectors_per_class.values())

            return {
                "status": "completed",
                "task_classes": task_classes,
                "selectors_per_class": selectors_per_class,
                "total_signals": total_signals,
            }

        except Exception as exc:
            logger.exception("run_fitness_analysis failed: %s", exc)
            return {
                "status": "error",
                "error": str(exc),
            }

    @app.tool()
    async def get_fitness_analyzer_status() -> dict[str, Any]:
        """Get the current status of the fitness analyzer.

        Returns:
            Dict with analyzer status:
                {
                    "running": bool,
                    "component_endpoints": list[tuple[str, str]],
                    "poll_interval_seconds": int
                }
        """
        if _fitness_analyzer is None:
            return {
                "running": False,
                "component_endpoints": [],
                "poll_interval_seconds": 0,
            }

        return {
            "running": _fitness_analyzer._running,
            "component_endpoints": list(_fitness_analyzer._component_endpoints),
            "poll_interval_seconds": _fitness_analyzer._poll_interval,
        }

    logger.info("Fitness analysis tools registered")
