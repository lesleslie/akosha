"""Bodai-specific MCP client helpers for Akosha.

``query_local_traces`` — Bodai-specific MCP helper for FitnessAnalyzer.
Stays in akosha per plan §4.4 (response-shape coercion is
Bodai-specific, not part of the cross-repo mcp-common SDK surface).

``extract_mcp_payload`` — shared helper for unwrapping the JSON-RPC
``{"content": [{"type": "text", "text": "<json>"}]}`` envelope that
:meth:`mcp_common.clients.CommonMCPClient.call_tool` returns.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp_common.clients.common_mcp_client import CommonMCPClient

logger = logging.getLogger(__name__)


def extract_mcp_payload(result: Any) -> Any:
    """Unwrap the JSON payload from a ``CallToolResult`` envelope.

    ``CommonMCPClient.call_tool`` returns a ``CallToolResult`` whose
    ``content[0].text`` holds the JSON-stringified tool result. This
    helper extracts and JSON-decodes that payload so call sites can
    work with the unwrapped shape they expect.

    Tolerant of dict-shaped test mocks (subscript access) AND real
    pydantic ``CallToolResult`` objects (attribute access) AND
    ``TextContent`` items (attribute access).

    Args:
        result: Either a real ``CallToolResult`` object or a dict
            standing in for one (test mocks).

    Returns:
        The parsed JSON payload (any JSON-decodable type), or
        ``None`` if the envelope is empty/malformed.
    """
    content = result["content"] if isinstance(result, dict) else getattr(result, "content", None)
    if not content:
        return None
    first = content[0]
    text = first["text"] if isinstance(first, dict) else getattr(first, "text", None)
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.debug("extract_mcp_payload: failed to decode text: %s", exc)
        return None


async def query_local_traces(
    client: "CommonMCPClient",
    task_class: str,
    time_range_minutes: int = 60,
) -> list[dict[str, Any]]:
    """Query traces from a Bodai component's local OTel store.

    Args:
        client: ``CommonMCPClient`` connected to the component's MCP endpoint.
        task_class: Task classification to filter traces (e.g. "code_generation").
        time_range_minutes: How far back to query (default 60 minutes).

    Returns:
        List of trace summary dicts from the component's local store.

    Note:
        This helper stays in akosha because the response-shape coercion
        (list OR dict with ``traces``/``items``/``result`` keys) is
        Bodai-specific. See plan §4.4 of
        ``docs/plans/2026-09-14-common-mcp-client-transport-unification.md``.
    """
    result = await client.call_tool(
        "akosha_query_local_traces",
        {
            "task_class": task_class,
            "time_range_minutes": time_range_minutes,
        },
    )
    payload = extract_mcp_payload(result)
    if isinstance(payload, list):
        return payload  # type: ignore[return-value]
    if isinstance(payload, dict):
        items = payload.get("traces") or payload.get("items") or payload.get("result")
        if isinstance(items, list):
            return items  # type: ignore[return-value]
    logger.debug("Unexpected query_local_traces payload shape: %r", payload)
    return []
