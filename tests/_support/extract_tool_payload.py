"""Shared helper to extract FastMCP tool payloads from the wire-shape envelope.

When a tool is invoked through a FastMCP ``Client`` (over ``FastMCPTransport``
or any other transport), the return value is a ``CallToolResult``-shaped object
whose ``.content[0].text`` holds the tool's structured return value encoded as
JSON. This helper centralizes the ``is_error`` + ``content[0].text`` → ``dict``
parsing so every regression test in the suite pins the same wire-shape contract.

Used by:

- ``tests/unit/test_search_code_patterns.py`` (the
  ``'list' object has no attribute 'values'`` regression).
- Future FastMCP tool tests that need the same envelope parsing.
"""

from __future__ import annotations

import json
from typing import Any


def _extract_payload(result: Any) -> dict[str, Any]:
    """Parse the FastMCP wire-shape envelope into a dict.

    The FastMCP client returns a ``CallToolResult`` whose ``.content`` is a
    list of content blocks; for plain tool returns the first block is a
    ``TextContent`` whose ``.text`` is the JSON-encoded return value.

    Raises:
        AssertionError: ``result.is_error`` is True (handler raised; structured
            response expected).
        AssertionError: ``result.content`` is empty.
        AssertionError: Parsed JSON is not a dict.

    Returns:
        The parsed ``dict[str, Any]`` payload.
    """
    assert not result.is_error, (
        f"Tool returned is_error=True; structured response expected. "
        f"content={result.content!r}"
    )
    assert result.content, "Tool returned empty content"
    text = result.content[0].text
    payload = json.loads(text)
    assert isinstance(payload, dict), (
        f"Tool returned a non-dict payload: {payload!r}"
    )
    return payload


__all__ = ["_extract_payload"]
