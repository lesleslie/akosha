"""DharaServiceRegistryClient + Bodai-specific CommonMCPClient helpers.

DharaServiceRegistryClient — minimal async client for Dhara's service registry.
Used by Akosha to read bodai_component services from Dhara's ecosystem state
so FitnessAnalyzer can discover registered component endpoints.
Does NOT use the MCP protocol — talks to Dhara's REST API directly via httpx.

``query_local_traces`` — Bodai-specific MCP helper for FitnessAnalyzer. Stays
in akosha per plan §4.4 (response-shape coercion is Bodai-specific, not part
of the cross-repo mcp-common SDK surface).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp_common.clients.common_mcp_client import CommonMCPClient

logger = logging.getLogger(__name__)


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
    if isinstance(result, list):
        return result  # type: ignore[return-value]
    if isinstance(result, dict):
        items = result.get("traces") or result.get("items") or result.get("result")
        if isinstance(items, list):
            return items  # type: ignore[return-value]
    logger.debug("Unexpected query_local_traces response shape: %r", result)
    return []


class DharaServiceRegistryClient:
    """Minimal async client for Dhara's service registry operations.

    Used by Akosha to read bodai_component services from Dhara's ecosystem state
    so FitnessAnalyzer can discover registered component endpoints.

    Does NOT use the MCP protocol — talks to Dhara's REST API directly via httpx.
    """

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def list_services(
        self,
        service_type: str | None = None,
        capability: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Call Dhara's list_services tool via HTTP POST."""
        import httpx2 as httpx

        payload: dict[str, Any] = {"name": "list_services", "arguments": {}}
        args = payload["arguments"]  # type: ignore[assignment]
        if service_type is not None:
            args["service_type"] = service_type
        if capability is not None:
            args["capability"] = capability
        if status is not None:
            args["status"] = status

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{self.base_url}/tools/call", json=payload)
            response.raise_for_status()
            result = response.json()
            # MCP tool response format: {"content": [{"type": "text", "text": "..."}]}
            # The text is a JSON string of the actual result
            content = result.get("content", [])
            if content and content[0].get("type") == "text":
                import json

                result_data: Any = json.loads(content[0]["text"])
                if isinstance(result_data, list):
                    return result_data  # type: ignore[return-value]
            return []

    async def aclose(self) -> None:
        """No-op placeholder kept for API compatibility."""
        pass

    async def get(self, key: str) -> dict[str, Any] | None:
        """Get a single key/value record from Dhara KV store."""
        import httpx2 as httpx

        payload: dict[str, Any] = {"name": "get", "arguments": {"key": key}}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{self.base_url}/tools/call", json=payload)
            response.raise_for_status()
            result = response.json()
            # MCP tool response format: {"content": [{"type": "text", "text": "..."}]}
            # The text is a JSON string of the value, or null
            content = result.get("content", [])
            if content and content[0].get("type") == "text":
                import json

                raw = json.loads(content[0]["text"])
                # raw may be None (key not found) or a dict / string
                if raw is None:
                    return None
                if isinstance(raw, dict):
                    return raw  # type: ignore[return-value]
                if isinstance(raw, str):
                    # some servers return the URL string directly
                    return {"url": raw}
            return None

    async def list_prefix(self, prefix: str) -> list[dict[str, Any]]:
        """List all key/value records under a key prefix via Dhara KV store.

        Phase 0 spec: component endpoints are stored as KV records under
        the 'component_endpoint/' prefix, keyed as 'component_endpoint/{name}'.
        """
        import httpx2 as httpx

        payload: dict[str, Any] = {"name": "list_prefix", "arguments": {"prefix": prefix}}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{self.base_url}/tools/call", json=payload)
            response.raise_for_status()
            result = response.json()
            # MCP tool response format: {"content": [{"type": "text", "text": "..."}]}
            # The text is a JSON string of the raw list
            content = result.get("content", [])
            if content and content[0].get("type") == "text":
                import json

                result_data: Any = json.loads(content[0]["text"])
                if isinstance(result_data, list):
                    return result_data  # type: ignore[return-value]
            return []
