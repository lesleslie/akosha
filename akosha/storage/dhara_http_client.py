"""HTTP client for Dhara's MCP server.

Plan: docs/plans/2026-08-29-akosha-websocket-search.md (Followup 4).
Provides the ``await list_prefix(prefix)`` async method that
:mod:`akosha.ingestion.websocket_invocations_subscriber` consumes.

Akosha's existing Dhara integration is HTTP-only -- every call goes
through ``POST /tools/call`` (see ``akosha/mcp/server.py:70-100`` and
``akosha/processing/fitness_analyzer.py:185-190``). This module
extracts that pattern into a small reusable client so the subscriber
can poll ``websocket_tool_invocation/v1/*`` without re-implementing
httpx plumbing.

Graceful failure: every method catches httpx errors and returns empty
results / logs at WARNING. The subscriber is a best-effort consumer;
missing or unreachable Dhara must never crash Akosha startup.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)


DHARA_DEFAULT_URL = "http://localhost:8683/mcp"


class DharaHttpClient:
    """Async HTTP client for Dhara's MCP-style ``POST /tools/call`` API.

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
            timeout_seconds: HTTP request timeout. Matches the 10-second
                precedent at ``akosha/mcp/server.py:83``.
        """
        self._base_url = (base_url or os.getenv("DHARA_MCP_URL", DHARA_DEFAULT_URL)).rstrip("/")
        self._timeout = timeout_seconds
        # Lazy client -- created on first call so import-time doesn't
        # require httpx event-loop initialization.
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        """Close the underlying httpx.AsyncClient. Safe to call repeatedly."""
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
            response = await client.post(
                f"{self._base_url}/tools/call",
                json={"name": "list_prefix", "arguments": {"prefix": prefix}},
            )
            response.raise_for_status()
            data = response.json()
            # Dhara returns MCP-format:
            # ``{"content": [{"type": "text", "text": "<json string>"}]}``.
            return self._parse_mcp_content(data, prefix)
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
            response = await client.post(
                f"{self._base_url}/tools/call",
                json={"name": "put", "arguments": {"key": key, "value": value}},
            )
            response.raise_for_status()
            return True
        except Exception as exc:
            logger.debug("DharaHttpClient.put(%r) failed: %s", key, exc)
            return False

    @staticmethod
    def _parse_mcp_content(data: Any, prefix: str) -> list[tuple[str, dict[str, Any]]]:
        """Parse MCP-format tool-call response into ``[(key, value), ...]``.

        Dhara wraps payloads in ``{"content": [{"type": "text", "text":
        "<json>"}]}`` per the MCP spec. ``text`` is a JSON-encoded
        string of the actual list. Be tolerant of multiple shapes --
        Dhara's response envelope may evolve.
        """
        try:
            content = data.get("content") if isinstance(data, dict) else None
            if not content:
                return []
            text = content[0].get("text") if isinstance(content, list) and content else None
            if not text:
                return []
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [
                    (item["key"], item["value"])
                    for item in parsed
                    if isinstance(item, dict) and "key" in item and "value" in item
                ]
            return []
        except (json.JSONDecodeError, KeyError, TypeError, IndexError) as exc:
            logger.debug(
                "DharaHttpClient._parse_mcp_content: parse failed for prefix=%r: %s",
                prefix,
                exc,
            )
            return []
