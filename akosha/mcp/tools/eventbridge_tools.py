"""MCP tools for the Akosha-side EventBridge publisher.

Exposes a single ``publish_to_eventbridge`` MCP tool that wraps the
underlying ``publish_*`` async functions into a sync-callable interface
for Claude Code and other MCP clients.

Mirrors the dispatch_to_pool pattern from
``mahavishnu/mcp/tools/pool_tools.py``: optional ``async_callback`` flag
returns a workflow_id immediately and runs the publish in the background.

The tool is gated by an explicit ``enabled`` parameter — calling
``register_eventbridge_tools(mcp_app, enabled=True)`` is required to
expose it. The MCP server wiring must pass
``enabled=cfg.eventbridge.enabled`` from the loaded AkoshaConfig.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any

from akosha.observability.eventbridge_publisher import (
    publish_aggregation_completed,
    publish_anomaly_detected,
    publish_insight_generated,
    publish_pattern_detected,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from akosha.mcp.tools.tool_registry import ToolCategory

logger = logging.getLogger(__name__)


async def _dispatch_topic(topic: str, payload: dict[str, Any]) -> None:
    """Route a (topic, payload) pair to the matching ``publish_*`` function."""
    if topic == "pattern.detected":
        await publish_pattern_detected(
            pattern_id=payload["pattern_id"],
            pattern_type=payload["pattern_type"],
            description=payload["description"],
            confidence=payload["confidence"],
            metadata=payload.get("metadata", {}),
        )
    elif topic == "anomaly.detected":
        await publish_anomaly_detected(
            anomaly_id=payload["anomaly_id"],
            anomaly_type=payload["anomaly_type"],
            severity=payload["severity"],
            description=payload["description"],
            metrics=payload.get("metrics", {}),
        )
    elif topic == "insight.generated":
        await publish_insight_generated(
            insight_id=payload["insight_id"],
            insight_type=payload["insight_type"],
            title=payload["title"],
            description=payload["description"],
            data=payload.get("data", {}),
        )
    elif topic == "aggregation.completed":
        await publish_aggregation_completed(
            aggregation_id=payload["aggregation_id"],
            aggregation_type=payload["aggregation_type"],
            record_count=payload["record_count"],
            summary=payload.get("summary", {}),
        )
    else:
        logger.warning(
            "akosha.eventbridge_tools: unknown topic=%s; ignoring", topic
        )


def register_eventbridge_tools(
    mcp_app: FastMCP,
    enabled: bool = False,
    category: ToolCategory | None = None,  # noqa: ARG001 - reserved for future registry grouping
) -> None:
    """Register the EventBridge publisher MCP tool.

    Args:
        mcp_app: FastMCP application instance.
        enabled: Master toggle. When False (default), the tool returns
            ``{"status": "disabled"}`` for every call. Callers must pass
            ``enabled=True`` after validating the operator has set
            ``eventbridge.enabled=true`` in settings.
        category: Optional ToolCategory for registry grouping.
    """
    _enabled = enabled

    @mcp_app.tool()
    async def publish_to_eventbridge(
        topic: str,
        payload: dict[str, Any],
        async_callback: bool = False,
    ) -> dict[str, Any]:
        """Publish an analytics event to the Akosha EventBridge stream."""
        if not _enabled:
            return {"status": "disabled"}

        if async_callback:
            workflow_id = f"pub_{uuid.uuid4().hex[:12]}"
            _dispatch_task = asyncio.create_task(_dispatch_topic(topic, payload))  # noqa: RUF006
            return {"workflow_id": workflow_id, "status": "queued"}

        await _dispatch_topic(topic, payload)
        return {"status": "published"}


__all__ = ["register_eventbridge_tools"]
