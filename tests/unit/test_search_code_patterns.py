"""Regression test for the `'list' object has no attribute 'values'` crash.

REQ-002, REQ-009: the ``search_code_patterns`` and
``find_similar_repositories`` handlers must return a structured response
(populated results OR a typed ``degraded`` envelope) for every indexer shape.
The session-buddy code-graph indexer can return ``graph_data["nodes"]`` as a
dict, a list, or absent; the previous code unconditionally called ``.values()``
on whatever was there, so a list crashed the handler at runtime and the tool
returned a traceback to the caller.

These tests pin the fix by:
- Stubbing the hot_store to return one of three shapes (dict, list, missing).
- Driving the tool through a FastMCP ``Client`` + ``FastMCPTransport``
  (the in-process equivalent of ``InMemoryTransport``) so we exercise the
  real FastMCP dispatch path, not the in-process closure.
- Asserting on the wire-shape envelope: ``assert not result.is_error`` AND
  ``json.loads(result.content[0].text)`` is a dict.

Fails on the unfixed code: the legacy list-shape branch raises
``AttributeError: 'list' object has no attribute 'values'`` inside the handler,
which surfaces through FastMCP as ``is_error=True``.

The ``_extract_payload`` helper lives in ``tests/_support/extract_tool_payload.py``
so future FastMCP tool regression tests can reuse the same envelope parser.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports.memory import FastMCPTransport

from akosha.mcp.tools.pycharm_tools import (
    PyCharmMCPAdapter,
    register_pycharm_tools,
)
from akosha.mcp.tools.tool_registry import FastMCPToolRegistry

from tests._support.extract_tool_payload import _extract_payload


DICT_SHAPE_GRAPH = {
    "repo_path": "/repos/example",
    "commit_hash": "deadbeef",
    "nodes_count": 2,
    "graph_data": {
        "nodes": {
            "n1": {
                "id": "n1",
                "name": "alpha",
                "type": "function",
                "file_path": "src/alpha.py",
                "start_line": 10,
                "source": "def alpha():\n    return 1\n",
            },
            "n2": {
                "id": "n2",
                "name": "beta",
                "type": "function",
                "file_path": "src/beta.py",
                "start_line": 20,
                "source": "def beta():\n    return 2\n",
            },
        },
    },
    "metadata": {},
    "ingested_at": "2026-10-09T00:00:00Z",
}

LIST_SHAPE_GRAPH = {
    "repo_path": "/repos/example",
    "commit_hash": "deadbeef",
    "nodes_count": 2,
    "graph_data": {
        "nodes": [
            {
                "id": "n1",
                "name": "alpha",
                "type": "function",
                "file_path": "src/alpha.py",
                "start_line": 10,
                "source": "def alpha():\n    return 1\n",
            },
            {
                "id": "n2",
                "name": "beta",
                "type": "function",
                "file_path": "src/beta.py",
                "start_line": 20,
                "source": "def beta():\n    return 2\n",
            },
        ],
    },
    "metadata": {},
    "ingested_at": "2026-10-09T00:00:00Z",
}

MISSING_SHAPE_GRAPH = {
    "repo_path": "/repos/example",
    "commit_hash": "deadbeef",
    "nodes_count": 0,
    "graph_data": {},
    "metadata": {},
    "ingested_at": "2026-10-09T00:00:00Z",
}


class _StubHotStore:
    """HotStore stub returning a single canned graph for every code-graph call."""

    def __init__(self, graph: dict[str, Any] | None) -> None:
        self._graph = graph

    async def list_code_graphs(
        self,
        repo_path: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if self._graph is None:
            return []
        return [
            {
                "repo_path": self._graph["repo_path"],
                "commit_hash": self._graph["commit_hash"],
                "nodes_count": self._graph["nodes_count"],
                "ingested_at": self._graph["ingested_at"],
            },
        ]

    async def get_code_graph(
        self,
        repo_path: str,
        commit_hash: str,
    ) -> dict[str, Any] | None:
        return self._graph


def _build_client(graph: dict[str, Any] | None) -> Client:
    """Wire a FastMCP app, register ``search_code_patterns`` with a stubbed
    hot_store, and return a ``Client`` over the in-memory transport.

    The PyCharm adapter is left at its default (no real MCP client); the
    ``search_regex`` call will short-circuit on its regex-sanitization gate
    and return ``[]``, so the test result envelope is driven entirely by the
    code-graph branch under test.
    """
    app = FastMCP("akosha-test")
    registry = FastMCPToolRegistry(app)

    # Reset the global PyCharm adapter cache so the test sees a clean
    # default adapter (mcp_client=None ⇒ search_regex returns []).
    import akosha.mcp.tools.pycharm_tools as _pycharm_mod

    _pycharm_mod._pycharm_adapter = None

    register_pycharm_tools(registry, hot_store=_StubHotStore(graph))
    return Client(FastMCPTransport(app))


def _build_code_graph_client(graph: dict[str, Any] | None) -> Client:
    """Wire a FastMCP app, register ``akosha_find_similar_repositories`` with
    a stubbed hot_store, and return a ``Client`` over the in-memory transport.

    Mirrors ``_build_client`` but for the code-graph analysis tools.
    """
    app = FastMCP("akosha-test-cg")
    registry = FastMCPToolRegistry(app)
    register_code_graph_analysis_tools(registry, hot_store=_StubHotStore(graph))
    return Client(FastMCPTransport(app))


@pytest.mark.asyncio
async def test_dict_shape_returns_structured_response() -> None:
    """graph_data['nodes'] is a dict (preferred shape) — handler iterates
    ``.values()`` and returns populated results.
    """
    async with _build_client(DICT_SHAPE_GRAPH) as client:
        result = await client.call_tool(
            "search_code_patterns",
            {"pattern": r"def \w+"},
        )
    payload = _extract_payload(result)
    assert payload.get("status") == "success", payload
    assert payload.get("pattern") == r"def \w+", payload
    assert "results" in payload, payload
    # Both nodes contain 'def <name>' — expect 2 matches.
    assert len(payload["results"]) == 2, payload


@pytest.mark.asyncio
async def test_list_shape_returns_structured_response() -> None:
    """graph_data['nodes'] is a list (legacy shape, the crashing case).
    The unfixed code raised ``AttributeError: 'list' object has no
    attribute 'values'``; the fix iterates the list directly and the
    handler returns a populated structured response.
    """
    async with _build_client(LIST_SHAPE_GRAPH) as client:
        result = await client.call_tool(
            "search_code_patterns",
            {"pattern": r"def \w+"},
        )
    payload = _extract_payload(result)
    assert payload.get("status") == "success", payload
    assert "results" in payload, payload
    assert len(payload["results"]) == 2, payload


@pytest.mark.asyncio
async def test_missing_shape_returns_structured_response() -> None:
    """graph_data has no 'nodes' key — handler treats the missing case
    as an empty graph and returns a structured response with no matches.
    """
    async with _build_client(MISSING_SHAPE_GRAPH) as client:
        result = await client.call_tool(
            "search_code_patterns",
            {"pattern": r"def \w+"},
        )
    payload = _extract_payload(result)
    assert payload.get("status") == "success", payload
    assert payload.get("results") == [], payload


@pytest.mark.asyncio
async def test_no_hot_store_graphs_returns_structured_response() -> None:
    """HotStore returns no code graphs at all (no graphs indexed yet) —
    handler returns a structured response with no code-graph matches.
    """
    async with _build_client(None) as client:
        result = await client.call_tool(
            "search_code_patterns",
            {"pattern": r"def \w+"},
        )
    payload = _extract_payload(result)
    assert payload.get("status") == "success", payload
    assert payload.get("results") == [], payload


# ---------------------------------------------------------------------------
# Regression tests for ``_compute_graph_similarity`` (the 7th bug site).
#
# The refuter review caught that ``_compute_graph_similarity`` called
# ``graph1.get("nodes", {}).values()`` unconditionally, so a list-shape graph
# crashed the handler with ``AttributeError: 'list' object has no attribute
# 'values'``. The fix applies the same shape-guard pattern used at the other
# 6 fixed sites.
#
# Testing the function directly (rather than through the MCP registration)
# avoids the broken ``registry.app`` access in ``register_code_graph_analysis_tools``
# and gives us a tighter, faster unit test focused on the actual bug.
# ---------------------------------------------------------------------------


def _graph_data(graph: dict[str, Any]) -> dict[str, Any]:
    """Return the inner ``graph_data`` for a graph fixture."""
    return graph["graph_data"]


@pytest.mark.asyncio
async def test_compute_graph_similarity_dict_shape_returns_score() -> None:
    """graph_data['nodes'] is a dict (preferred shape) — the similarity
    function iterates ``.values()`` and returns a finite cosine-similarity
    score.
    """
    from akosha.mcp.tools.code_graph_tools import _compute_graph_similarity

    score = await _compute_graph_similarity(
        _graph_data(DICT_SHAPE_GRAPH),
        _graph_data(DICT_SHAPE_GRAPH),
    )
    # Same graph on both sides → identical type distribution → cosine
    # similarity of 1.0.
    assert score == pytest.approx(1.0), score


@pytest.mark.asyncio
async def test_compute_graph_similarity_list_shape_does_not_crash() -> None:
    """graph_data['nodes'] is a list (the crashing case for the 7th site).

    The unfixed code raised ``AttributeError: 'list' object has no
    attribute 'values'`` inside ``_compute_graph_similarity``; the fix
    iterates the list directly and returns a finite similarity score.

    This is the BLOCKER regression: the unfixed handler returned a
    traceback to the caller instead of a structured response.
    """
    from akosha.mcp.tools.code_graph_tools import _compute_graph_similarity

    score = await _compute_graph_similarity(
        _graph_data(LIST_SHAPE_GRAPH),
        _graph_data(LIST_SHAPE_GRAPH),
    )
    # Identical list-shape graphs → cosine similarity of 1.0.
    assert score == pytest.approx(1.0), score


@pytest.mark.asyncio
async def test_compute_graph_similarity_missing_shape_returns_zero() -> None:
    """graph_data has no 'nodes' key — similarity is 0.0 (legitimate
    "no overlap", per the function's contract; the audit H1 fix removed
    the bare ``except Exception: return 0.0`` so this returns 0.0 only
    because both ``types1`` and ``types2`` are empty).
    """
    from akosha.mcp.tools.code_graph_tools import _compute_graph_similarity

    score = await _compute_graph_similarity(
        _graph_data(MISSING_SHAPE_GRAPH),
        _graph_data(MISSING_SHAPE_GRAPH),
    )
    assert score == 0.0, score


@pytest.mark.asyncio
async def test_compute_graph_similarity_mixed_shapes_does_not_crash() -> None:
    """Mixed shapes (dict vs list) — the 7th-site fix must handle the
    asymmetric case where one graph returns dict-shape and the other
    list-shape. Both must produce a finite score.
    """
    from akosha.mcp.tools.code_graph_tools import _compute_graph_similarity

    score_dict_vs_list = await _compute_graph_similarity(
        _graph_data(DICT_SHAPE_GRAPH),
        _graph_data(LIST_SHAPE_GRAPH),
    )
    score_list_vs_dict = await _compute_graph_similarity(
        _graph_data(LIST_SHAPE_GRAPH),
        _graph_data(DICT_SHAPE_GRAPH),
    )
    # Identical node-type distribution on both sides → cosine similarity of
    # 1.0 in both directions (symmetry is a property of cosine similarity).
    assert score_dict_vs_list == pytest.approx(1.0), score_dict_vs_list
    assert score_list_vs_dict == pytest.approx(1.0), score_list_vs_dict
