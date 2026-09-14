"""HTTP client for Dhara's MCP server.

Plan: docs/plans/2026-08-29-akosha-websocket-search.md (Followup 4).
Provides the ``await list_prefix(prefix)`` async method that
:mod:`akosha.ingestion.websocket_invocations_subscriber` consumes.

MCP transport: uses ``mcp_common.clients.CommonMCPClient.call_tool``
(streamable-HTTP) instead of legacy ``POST /tools/call`` POSTs.

Graceful failure: every method catches MCP errors and returns empty
results / logs at WARNING. The subscriber is a best-effort consumer;
missing or unreachable Dhara must never crash Akosha startup.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from mcp_common.clients.common_mcp_client import CommonMCPClient

from akosha.mcp.client import extract_mcp_payload

logger = logging.getLogger(__name__)


DHARA_DEFAULT_URL = "http://localhost:8683/mcp"


class DharaHttpClient:
    """Async MCP client for Dhara's ``list_prefix`` and ``put`` tools.

    Methods:
        list_prefix(prefix) -> list[tuple[str, dict]]: list keys+values
            matching prefix. Empty list on any error.
        put(key, value) -> bool: write a single row. False on any error.
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        """Initialize the client.

        Args:
            base_url: Dhara MCP endpoint. Defaults to ``$DHARA_MCP_URL`` or
                ``DHARA_DEFAULT_URL``.
            timeout_seconds: MCP per-call timeout. Matches the 10-second
                precedent at ``akosha/mcp/server.py:227``.
        """
        self._base_url = (base_url or os.getenv("DHARA_MCP_URL", DHARA_DEFAULT_URL)).rstrip("/")
        self._timeout = timeout_seconds
        # Lazy client -- created on first call so import-time doesn't
        # require event-loop initialization.
        self._client: CommonMCPClient | None = None

    async def _ensure_client(self) -> CommonMCPClient:
        if self._client is None:
            self._client = CommonMCPClient(
                base_url=self._base_url,
                timeout=self._timeout,
            )
        return self._client

    async def aclose(self) -> None:
        """Close the underlying MCP client. Safe to call repeatedly."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def list_prefix(self, prefix: str) -> list[tuple[str, dict[str, Any]]]:
        """List ``(key, value)`` tuples whose key starts with prefix.

        Returns an empty list on any transport / parse error so the
        subscriber's polling loop never raises into the caller.
        """
        client = await self._ensure_client()
        try:
            call_result = await client.call_tool(
                "list_prefix",
                {"prefix": prefix},
                timeout=self._timeout,
            )
            data = extract_mcp_payload(call_result)
            return self._parse_list_payload(data, prefix)
        except Exception as exc:
            logger.debug("DharaHttpClient.list_prefix(%r) failed: %s", prefix, exc)
            return []

    async def put(self, key: str, value: dict[str, Any]) -> bool:
        """Write a single key/value row via Dhara's MCP ``put`` tool.

        Returns True on success, False on any error. Best-effort by
        design -- the caller decides what to do with a failed write.
        """
        client = await self._ensure_client()
        try:
            await client.call_tool(
                "put",
                {"key": key, "value": value},
                timeout=self._timeout,
            )
            return True
        except Exception as exc:
            logger.debug("DharaHttpClient.put(%r) failed: %s", key, exc)
            return False

    @staticmethod
    def _parse_list_payload(data: Any, prefix: str) -> list[tuple[str, dict[str, Any]]]:
        """Parse the unwrapped ``list_prefix`` payload.

        Dhara's ``list_prefix`` tool returns either a list of
        ``{"key": ..., "value": ...}`` records OR a list of
        ``[key, value]`` tuples. Be tolerant of both shapes.
        """
        if not isinstance(data, list):
            logger.debug(
                "DharaHttpClient._parse_list_payload: unexpected shape for prefix=%r: %r",
                prefix,
                data,
            )
            return []
        rows: list[tuple[str, dict[str, Any]]] = []
        for item in data:
            if isinstance(item, dict) and "key" in item and "value" in item:
                rows.append((item["key"], item["value"]))
            elif (
                isinstance(item, (list, tuple))
                and len(item) == 2
                and isinstance(item[0], str)
            ):
                rows.append((item[0], item[1]))  # type: ignore[arg-type]
        return rows
