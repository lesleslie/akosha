"""BodaiComponentMCPClient — MCP client for polling Bodai component endpoints.

Calls `query_local_traces` on each component's MCP HTTP endpoint using the official
MCP Python client library (streamable_http transport).

Session Management (FastMCP streamable-http):
    The streamable-http transport uses a session-based flow:
    1. POST with initialize request (no session ID) → server creates session
    2. Server returns session ID via mcp-session-id header in response
    3. Client sends 'initialized' notification to complete handshake
    4. Client opens GET SSE stream for server-to-client messages
    5. Subsequent POST requests include the session ID header

This client uses the official mcp.client.streamable_http_client() which handles
all session lifecycle correctly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp.client.session import ClientSession

logger = logging.getLogger(__name__)


class BodaiComponentMCPClient:
    """Async MCP client for calling tools on Bodai components.

    Uses the official MCP Python client's streamable_http transport which properly
    handles session establishment, initialized notification, and GET SSE stream
    management for server-to-client messaging.

    Parameters:
        base_url: Full MCP HTTP server URL (e.g. "http://localhost:8680/mcp")
        timeout: Request timeout in seconds
        token: Optional Bearer token for auth
    """

    # Allowed URL schemes — block SSRF via file://, ftp://, gopher://, etc.
    _ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

    def __init__(
        self,
        base_url: str,
        timeout: float = 30.0,
        token: str | None = None,
    ) -> None:
        from urllib.parse import urlparse

        parsed = urlparse(base_url)
        if parsed.scheme not in self._ALLOWED_SCHEMES:
            raise ValueError(
                f"BodaiComponentMCPClient: scheme '{parsed.scheme}' is not allowed. "
                f"Only {sorted(self._ALLOWED_SCHEMES)} are permitted (SSRF protection)."
            )
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._token = token
        self._session: ClientSession | None = None
        self._transport_context: Any = None
        self._get_session_id: Any = None

    @property
    def tools_url(self) -> str:
        """Return the tool invocation endpoint."""
        return self.base_url

    @property
    def session_id(self) -> Any:
        """Return the current MCP session ID, or None if not established."""
        if self._get_session_id is not None:
            return self._get_session_id()
        return None

    async def _ensure_session(self) -> None:
        """Establish MCP session using official client transport."""
        if self._session is not None:
            return

        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        http_client: Any = None
        if self._token:
            import httpx

            http_client = httpx.AsyncClient(
                timeout=self.timeout,
                headers={"Authorization": f"Bearer {self._token}"},
            )

        self._transport_context = streamable_http_client(
            self.base_url,
            http_client=http_client,
            terminate_on_close=True,
        )

        rs, ws, self._get_session_id = await self._transport_context.__aenter__()
        self._session = ClientSession(rs, ws)
        await self._session.__aenter__()
        await self._session.initialize()

        logger.debug("MCP session established: %s", self.session_id)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call an MCP tool over HTTP.

        Args:
            name: Tool name (e.g. "query_local_traces")
            arguments: Tool arguments dict

        Returns:
            Tool result from the MCP response
        """
        await self._ensure_session()

        # ``_ensure_session`` always sets ``_session`` to a ``ClientSession``,
        # but ty can't see across the await boundary. Use a runtime guard so
        # the type narrows without a blanket ``type: ignore``.
        session = self._session
        if session is None:
            raise RuntimeError("MCP session is not initialized")
        return await session.call_tool(name, arguments)

    async def query_local_traces(
        self,
        task_class: str,
        time_range_minutes: int = 60,
    ) -> list[dict[str, Any]]:
        """Query traces from a Bodai component's local OTel store.

        Args:
            task_class: Task classification to filter traces (e.g. "code_generation")
            time_range_minutes: How far back to query (default 60 minutes)

        Returns:
            List of trace summary dicts from the component's local store.
        """
        result = await self.call_tool(
            "query_local_traces",
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

    async def aclose(self) -> None:
        """Close the MCP session and transport."""
        if self._session is not None:
            await self._session.__aexit__(None, None, None)
            self._session = None
        if self._transport_context is not None:
            await self._transport_context.__aexit__(None, None, None)
            self._transport_context = None
        self._get_session_id = None


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
        import httpx

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
        """No-op for API compatibility with BodaiComponentMCPClient."""
        pass

    async def get(self, key: str) -> dict[str, Any] | None:
        """Get a single key/value record from Dhara KV store."""
        import httpx

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
        import httpx

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
