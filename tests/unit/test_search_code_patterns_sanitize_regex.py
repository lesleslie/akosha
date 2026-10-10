"""Regression test for the ``search_code_patterns`` ReDoS mitigation gap.

REQ-CROSS-001-SEC-2: The pre-flight length cap (1600 chars) at the
top of ``search_code_patterns`` is a coarse filter; the real
defense-in-depth is the existing ``PyCharmMCPAdapter._sanitize_regex``
helper, which (1) caps at 500 chars, (2) blocklists the six
catastrophic-backtracking nesting patterns, and (3) probes
``re.compile`` for syntax errors. The PyCharm-bridge path
(``adapter.search_regex``) routes through ``_sanitize_regex``
internally — but the code-graph search path applies the raw
pattern to ``re.search(pattern, source)`` with no sanitization.

A caller who passes a 1000-char pattern that triggers catastrophic
backtracking in the code-graph loop will hang the process because
``re`` has no native timeout. The fix: route the pattern through
``adapter._sanitize_regex`` once, before either search runs, and
reject the call with ``rejected: True`` if the sanitization fails.

This test exercises the two ReDoS-relevant rejection paths of
``_sanitize_regex`` (blocklist match, length cap) and asserts that
``search_code_patterns`` propagates the rejection through to the
caller instead of running the raw pattern against node sources.

NOTE: the test fixture source is intentionally a short string of
characters that are NOT in the regex's alphabet. This keeps the
"unfixed" code path fast (so the RED state is observable in
seconds, not minutes) while still exercising the raw ``re.search``
call that the fix is meant to short-circuit. The blocklist
rejection happens BEFORE the regex engine sees the source, so
the rejection is observable regardless of source content.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports.memory import FastMCPTransport

from akosha.mcp.tools.pycharm_tools import register_pycharm_tools
from akosha.mcp.tools.tool_registry import FastMCPToolRegistry
from tests._support.extract_tool_payload import _extract_payload


def _build_code_graph_with_source(
    *,
    body: str,
) -> dict[str, Any]:
    """Build a code-graph fixture that DOES carry a ``source`` field
    so the code-graph path is exercised regardless of disk-read
    fallback status. ``repo_path`` and ``file_path`` are
    placeholders — the inline ``source`` field bypasses them.
    """
    return {
        "repo_path": "/nonexistent/repo",
        "commit_hash": "deadbeef",
        "nodes_count": 1,
        "graph_data": {
            "nodes": [
                {
                    "symbol_id": "test|node|function|victim",
                    "symbol_name": "victim",
                    "symbol_type": "function",
                    "file_path": "x.py",
                    "repo_path": "/nonexistent/repo",
                    "start_line": 1,
                    "end_line": 1,
                    "language": "python",
                    "signature": None,
                    "complexity": 1,
                    "is_deleted": False,
                    "commit_hash": "deadbeef",
                    "source": body,
                }
            ]
        },
        "metadata": {},
        "ingested_at": "2026-10-10T00:00:00Z",
    }


class _StubHotStore:
    def __init__(self, graph: dict[str, Any] | None) -> None:
        self._graph = graph

    async def list_code_graphs(
        self,
        repo_path: str | None = None,  # noqa: ARG002
        limit: int = 100,  # noqa: ARG002
    ) -> list[dict[str, Any]]:
        if self._graph is None:
            return []
        return [
            {
                "repo_path": self._graph["repo_path"],
                "commit_hash": self._graph["commit_hash"],
                "nodes_count": self._graph["nodes_count"],
                "ingested_at": self._graph["ingested_at"],
            }
        ]

    async def get_code_graph(
        self,
        repo_path: str,  # noqa: ARG002
        commit_hash: str,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        return self._graph


def _build_client(graph: dict[str, Any] | None) -> Client:
    """Same wiring as the sibling ``test_search_code_patterns_source_fallback.py``:
    force a fresh ``PyCharmMCPAdapter`` with ``mcp_client=None`` so the
    PyCharm-bridge path short-circuits to ``[]``. The result envelope
    is driven entirely by the code-graph branch under test.
    """
    app = FastMCP("akosha-test-sanitize-regex")
    registry = FastMCPToolRegistry(app)

    import akosha.mcp.tools.pycharm_tools as _pycharm_mod

    _pycharm_mod._pycharm_adapter = None

    register_pycharm_tools(registry, hot_store=_StubHotStore(graph))
    return Client(FastMCPTransport(app))


@pytest.mark.asyncio
async def test_search_code_patterns_rejects_blocklisted_nesting_pattern() -> None:
    """A pattern that matches one of the six ``_sanitize_regex`` blocklist
    entries (e.g. ``(.*)+``) must be rejected before it reaches
    ``re.search(pattern, source)`` in the code-graph loop.

    Without the fix, the code-graph path applies the raw pattern to a
    real source string. On a long enough source the nested quantifier
    ``(.*)+`` is catastrophic — ``re`` has no native timeout so the
    process hangs.

    The contract: the call returns ``rejected: True`` and zero results.

    Pattern choice: ``(.*)+`` alone (6 chars) is in the
    ``_sanitize_regex`` blocklist (``r"\\(\\.\\*\\)\\+"``) and
    matches the entire 10-char source in one go — so the raw
    ``re.search`` is fast even on the unfixed code, and the test
    finishes in <1s in either state (REJECTED by the fix, NOT
    REJECTED on the unfixed code).
    """
    graph = _build_code_graph_with_source(
        # Source with no 'a' or 'b' so the regex is forced into
        # backtracking territory. Wait — (.*)+ matches anything
        # greedily, so it matches the whole source in one step.
        # The engine doesn't backtrack because the search succeeds
        # at the first position.
        body="xxxxxxxxxx",
    )

    async with _build_client(graph) as client:
        result = await client.call_tool(
            "search_code_patterns",
            {"pattern": r"(.*)+", "scope": "python"},
        )

    payload = _extract_payload(result)
    assert payload["status"] == "success", payload
    assert payload.get("rejected") is True, (
        f"Blocklisted pattern must be rejected: got {payload!r}. "
        "Without the fix, the raw pattern is applied to the code-graph "
        "source via re.search and can hang the process via catastrophic "
        "backtracking (re has no native timeout)."
    )
    assert payload["count"] == 0, payload
    assert payload["results"] == [], payload


@pytest.mark.asyncio
async def test_search_code_patterns_rejects_pattern_exceeding_sanitize_cap() -> None:
    """A pattern that exceeds the inner ``_sanitize_regex`` 500-char cap
    (but stays under the outer 1600-char cap) must also be rejected.

    This is the second layer of defense — even a non-blocklisted
    pattern over 500 chars is suspicious enough to refuse, because
    backtracking cost scales with pattern length and a 1000-char
    pattern applied to a multi-MB source slice is a DoS vector.

    The contract: ``rejected: True``, zero results, no exception.

    Pattern choice: ``(c|d) * 200`` is 1200 chars. It uses ``c``
    and ``d`` (not ``a``/``b``) to avoid the blocklist entirely —
    we're testing the length gate, not the blocklist. The source
    uses ``x`` characters (not ``c`` or ``d``) so the
    ``re.search`` on the unfixed path fails fast without
    catastrophic backtracking.
    """
    graph = _build_code_graph_with_source(
        body="x" * 20,
    )

    long_pattern = "(c|d)" * 200  # 1200 chars, safe non-blocklisted

    async with _build_client(graph) as client:
        result = await client.call_tool(
            "search_code_patterns",
            {"pattern": long_pattern, "scope": "python"},
        )

    payload = _extract_payload(result)
    assert payload["status"] == "success", payload
    assert payload.get("rejected") is True, (
        f"Pattern over 500 chars must be rejected by the inner "
        f"_sanitize_regex cap: got {payload!r}. Length was "
        f"{len(long_pattern)} chars."
    )
    assert payload["count"] == 0, payload
