"""Helpers that normalize code-graph data for the akosha MCP handlers.

REQ-IDX-001 (per ``docs/plans/2026-10-09-akosha-indexing-wiring.md``): the
session-buddy code-graph indexer can return ``graph_data["nodes"]`` as
a dict (upstream default), a list (what session-buddy's
``get_code_graph`` actually serializes — a JSON array of node dicts),
or absent. Hand-rolled inline ``isinstance(nodes, dict)`` checks
across ``pycharm_tools.py`` and ``code_graph_tools.py`` all rebuild
the same shape-guard logic; this module extracts the contract once.

Contract:
- ``iter_graph_nodes(graph_data)`` yields every dict-shaped node, regardless of
  whether ``graph_data["nodes"]`` is a dict-of-dicts (legacy / upstream) or a
  list-of-dicts (session-buddy serialization).
- Items that are not dicts are silently dropped — never raised on.
- ``graph_data`` itself can be ``None`` or any non-dict; yields nothing.
- A new iterator is returned per call; consuming one does not poison another.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator


def iter_graph_nodes(graph_data: Any) -> Iterator[dict[str, Any]]:
    """Yield code-graph node dicts from ``graph_data``.

    Args:
        graph_data: Either a dict containing a ``"nodes"`` key, or anything
            else (in which case no nodes are yielded). The ``"nodes"`` value
            may be a dict-of-dicts, a list-of-dicts, an empty container,
            ``None``, or any non-iterable type — only dict-shaped items are
            emitted, everything else is silently skipped.

    Yields:
        Each ``dict[str, Any]`` that qualifies as a node. A fresh iterator is
        returned on every call so callers can iterate the result multiple
        times.
    """
    if not isinstance(graph_data, dict):
        return
    nodes = graph_data.get("nodes")
    if isinstance(nodes, dict):
        for node in nodes.values():
            if isinstance(node, dict):
                yield node
    elif isinstance(nodes, list):
        for node in nodes:
            if isinstance(node, dict):
                yield node
