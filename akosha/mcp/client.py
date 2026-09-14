"""DharaServiceRegistryClient — minimal async client for Dhara's service registry.

Used by Akosha to read bodai_component services from Dhara's ecosystem state
so FitnessAnalyzer can discover registered component endpoints.

Does NOT use the MCP protocol — talks to Dhara's REST API directly via httpx.
"""

from __future__ import annotations

from typing import Any


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
