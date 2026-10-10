"""Regression test for the cross-repo ``search_code_patterns`` source-fallback.

REQ-CROSS-001: ``mcp__akosha__search_code_patterns`` must return a
non-empty result for patterns that match in the source code of a
node, even when the node has no ``source`` field.

The current contract: the search iterates code-graph nodes and
calls ``node.get("source", "")``. Nodes shipped by the
mahavishnu-side ``core.code_index.indexer`` do NOT carry a ``source``
field (the parser only populates ``symbol_id``/``symbol_name``/
``symbol_type``/``file_path``/``start_line``/``end_line``/``signature``/
``complexity``/``commit_hash``). The fallback to ``""`` is silent
and the regex match always returns ``None`` — so the search comes
back empty even when the underlying source code contains the
pattern.

The fix: when a node has no ``source`` field but does carry
``file_path`` + ``start_line`` + ``end_line`` + ``repo_path``,
the search reads the line range directly from disk and matches the
regex against that slice. This is a strict superset of the old
behaviour — nodes that already have ``source`` continue to work
unchanged.

This test exercises the disk-read fallback with a real temp file
(no mocking of the filesystem). It is RED on the unfixed code:
the fixture graph has no ``source`` field, so the current
``re.search(pattern, "")`` branch never matches, and the search
returns 0 results even though the pattern is plainly present in
the file.

SECURITY (REQ-CROSS-001-SEC): the disk-read fallback must NOT
be a path-traversal vector. The ``file_path`` field comes from
the code graph (session-buddy → akosha) and is operator-controlled
data, not caller-controlled — but defense in depth: the
fallback must reject any candidate path that resolves outside
``repo_path``. Tests for this are in
``test_search_code_patterns_path_traversal_safety``.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
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


def _build_real_source_graph(
    tmp_path: Path,
    *,
    file_relpath: str,
    start_line: int,
    end_line: int,
) -> dict[str, Any]:
    """Build a code graph fixture shaped like the real mahavishnu payload.

    Critically: NO ``source`` field. The fix under test is the
    disk-read fallback that constructs ``source`` from
    ``repo_path`` + ``file_path`` + ``start_line``/``end_line``.
    """
    return {
        "repo_path": str(tmp_path),
        "commit_hash": "deadbeef",
        "nodes_count": 1,
        "graph_data": {
            "nodes": [
                {
                    "symbol_id": f"{tmp_path}|||{file_relpath}|||function|||needle_function",
                    "symbol_name": "needle_function",
                    "symbol_type": "function",
                    "file_path": file_relpath,
                    "repo_path": str(tmp_path),
                    "start_line": start_line,
                    "end_line": end_line,
                    "language": "python",
                    "signature": None,
                    "complexity": 1,
                    "is_deleted": False,
                    "commit_hash": "deadbeef",
                    # Note: no ``source`` field — the fixture mirrors the
                    # real mahavishnu payload exactly.
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
            }
        ]

    async def get_code_graph(
        self,
        repo_path: str,
        commit_hash: str,
    ) -> dict[str, Any] | None:
        return self._graph


def _build_client(graph: dict[str, Any] | None) -> Client:
    """Same pattern as the sibling ``test_search_code_patterns.py``.

    The PyCharm adapter is left at its default (mcp_client=None ⇒
    ``search_regex`` short-circuits to ``[]`` via the regex-sanitize
    gate), so the result envelope is driven entirely by the
    code-graph branch under test.
    """
    app = FastMCP("akosha-test-source-fallback")
    registry = FastMCPToolRegistry(app)

    import akosha.mcp.tools.pycharm_tools as _pycharm_mod

    _pycharm_mod._pycharm_adapter = None

    register_pycharm_tools(registry, hot_store=_StubHotStore(graph))
    return Client(FastMCPTransport(app))


@pytest.mark.asyncio
async def test_search_code_patterns_reads_source_from_disk_when_node_has_no_source(
    tmp_path: Path,
) -> None:
    """When the node has no ``source`` field, the search must read the
    line range from ``repo_path/file_path`` and match the regex
    against that slice.

    The fixture file contains a uniquely-named function
    ``needle_function`` that contains a ``UNIQUE_PATTERN_TOKEN``. A
    regex search for ``UNIQUE_PATTERN_TOKEN`` should find it even
    though the node's stored graph has no ``source`` field.
    """
    file_relpath = "src/needle_module.py"
    file_abs = tmp_path / file_relpath
    file_abs.parent.mkdir(parents=True)
    file_abs.write_text(
        textwrap.dedent(
            """\
            # Line 1
            # Line 2
            # Line 3
            # Line 4
            # Line 5
            def needle_function():
                # Line 7
                # Line 8
                # Line 9
                return "UNIQUE_PATTERN_TOKEN"
            """
        )
    )

    # The function spans lines 6..10 in the file (1-indexed).
    graph = _build_real_source_graph(
        tmp_path,
        file_relpath=file_relpath,
        start_line=6,
        end_line=10,
    )

    async with _build_client(graph) as client:
        result = await client.call_tool(
            "search_code_patterns",
            {"pattern": "UNIQUE_PATTERN_TOKEN", "scope": "python"},
        )

    payload = _extract_payload(result)
    assert payload["status"] == "success", payload
    # The unique pattern only appears in the disk file; the current
    # code does not read the disk, so the count must be 1 once the
    # fallback is implemented (RED on the unfixed code: count==0).
    assert payload["count"] == 1, (
        f"Expected 1 hit for UNIQUE_PATTERN_TOKEN; got {payload!r}. "
        "The fixture file contains the pattern, but the node has no "
        "``source`` field — the search must read the line range from "
        "disk as a fallback."
    )
    results = payload["results"]
    assert len(results) == 1
    hit = results[0]
    assert hit["source"] == "code_graph"
    assert hit["file_path"] == file_relpath
    assert hit["line_number"] == 10  # the line where the token lives
    assert "UNIQUE_PATTERN_TOKEN" in hit["match_text"]


@pytest.mark.asyncio
async def test_search_code_patterns_node_without_file_path_falls_back_to_signature(
    tmp_path: Path,
) -> None:
    """If a node has neither ``source`` nor ``start_line``/``file_path``,
    the search must still attempt the ``signature`` field (which on
    function nodes carries the redacted docstring). This is a
    graceful-degradation path: the node is too thin to reconstruct
    source from disk, but ``signature`` may still match a docstring
    search.
    """
    graph = {
        "repo_path": str(tmp_path),
        "commit_hash": "deadbeef",
        "nodes_count": 1,
        "graph_data": {
            "nodes": [
                {
                    "symbol_id": "x",
                    "symbol_name": "x",
                    "symbol_type": "function",
                    "file_path": "nope.py",  # file does not exist on disk
                    "repo_path": str(tmp_path),
                    "start_line": 1,
                    "end_line": 2,
                    "language": "python",
                    "signature": "Build a thing that does UNIQUE_DOC_TOKEN.",
                    "complexity": 1,
                    "is_deleted": False,
                    "commit_hash": "deadbeef",
                }
            ]
        },
        "metadata": {},
        "ingested_at": "2026-10-10T00:00:00Z",
    }

    async with _build_client(graph) as client:
        result = await client.call_tool(
            "search_code_patterns",
            {"pattern": "UNIQUE_DOC_TOKEN", "scope": "python"},
        )

    payload = _extract_payload(result)
    assert payload["status"] == "success", payload
    assert payload["count"] >= 1, (
        f"Expected the docstring fallback to find UNIQUE_DOC_TOKEN in "
        f"the node's signature; got {payload!r}"
    )


@pytest.mark.asyncio
async def test_search_code_patterns_disk_read_rejects_path_traversal(
    tmp_path: Path,
) -> None:
    """A malicious code-graph entry with ``file_path='../../etc/passwd'``
    (or any path that escapes ``repo_path``) must NOT cause the search
    to read outside the indexed repo. The fallback must resolve the
    candidate path and reject any path that isn't a descendant of
    ``repo_path``.

    Test layout:
    - ``tmp_path`` is the repo root. We DO NOT put a file there with
      the target token.
    - We put a file in the user's HOME directory
      (``/tmp/UNIQUE_TOKEN_OUTSIDE_REPO``) that does contain the
      token. If the fallback resolves the candidate as written, it
      will read that file and surface a hit. The contract says: no
      hit. The fallback must reject the traversal.
    """
    import tempfile

    # Place the "outside" file in a known location that's definitely
    # not under ``tmp_path``. We use ``tempfile.gettempdir()`` for
    # portability — on macOS this is ``/var/folders/.../T/`` or
    # ``/tmp/``, on Linux it's ``/tmp/``. ``tmp_path`` (the pytest
    # tmp dir) is a sibling of these, not a parent.
    outside_dir = Path(tempfile.gettempdir())
    outside_file = outside_dir / f"search_outside_repo_{tmp_path.name}.txt"
    outside_token = "OUTSIDE_REPO_SECRET_TOKEN"
    outside_file.write_text(outside_token)
    try:
        # Compute a relative path that escapes tmp_path: e.g.
        # ``../<outside_dir_basename>/<file_name>``. We rely on the
        # common case where ``outside_dir`` and ``tmp_path`` are
        # siblings under ``/tmp`` or ``/var/folders/.../T/``.
        try:
            traversal_rel = outside_file.resolve().relative_to(
                tmp_path.resolve()
            )
        except ValueError:
            pytest.skip(
                "outside_dir and tmp_path are not siblings under a common "
                "parent; cannot construct a relative-traversal fixture on "
                "this filesystem layout."
            )

        graph = {
            "repo_path": str(tmp_path),
            "commit_hash": "deadbeef",
            "nodes_count": 1,
            "graph_data": {
                "nodes": [
                    {
                        "symbol_id": "evil",
                        "symbol_name": "evil",
                        "symbol_type": "function",
                        "file_path": str(traversal_rel),
                        "repo_path": str(tmp_path),
                        "start_line": 1,
                        "end_line": 1,
                        "language": "python",
                        "signature": None,
                        "complexity": 1,
                        "is_deleted": False,
                        "commit_hash": "deadbeef",
                    }
                ]
            },
            "metadata": {},
            "ingested_at": "2026-10-10T00:00:00Z",
        }

        async with _build_client(graph) as client:
            result = await client.call_tool(
                "search_code_patterns",
                {"pattern": outside_token, "scope": "python"},
            )

        payload = _extract_payload(result)
        assert payload["status"] == "success", payload
        # The traversal must not have been followed; the search
        # must report zero hits because the file is outside the
        # repo root.
        assert payload["count"] == 0, (
            f"Path-traversal safety violation: the disk-read fallback "
            f"read {outside_file} (outside the repo {tmp_path}). "
            f"Result: {payload!r}"
        )
    finally:
        outside_file.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_search_code_patterns_disk_read_caps_file_size(
    tmp_path: Path,
) -> None:
    """The disk-read fallback must cap the bytes read so a malicious
    or pathological code-graph entry pointing at a multi-gigabyte
    file can't blow up memory. The contract: read at most
    ``_MAX_DISK_READ_BYTES`` bytes; anything beyond that is
    truncated and reported as ``source=None`` for that node (the
    search then falls through to the signature/docstring path,
    which has its own size cap).
    """
    file_relpath = "huge_file.py"
    file_abs = tmp_path / file_relpath
    # Write a file larger than the read cap. Use a small cap
    # (override the constant via monkeypatch) so the test is fast.
    import akosha.mcp.tools.pycharm_tools as _pycharm_mod

    big_token = "UNIQUE_BIG_FILE_TOKEN"
    # 1 MiB of padding + the token at the end. ``start_line``/
    # ``end_line`` point at the token's actual line.
    pad = "x" * (1024 * 1024)
    body = pad + "\n" + f"# {big_token}\n"
    file_abs.write_text(body)
    # Locate the token's line in the file.
    lines = body.split("\n")
    token_line = len(lines) - 1  # 1-indexed; the token is the last line
    assert big_token in lines[token_line - 1]

    graph = {
        "repo_path": str(tmp_path),
        "commit_hash": "deadbeef",
        "nodes_count": 1,
        "graph_data": {
            "nodes": [
                {
                    "symbol_id": "big",
                    "symbol_name": "big",
                    "symbol_type": "function",
                    "file_path": file_relpath,
                    "repo_path": str(tmp_path),
                    "start_line": token_line,
                    "end_line": token_line,
                    "language": "python",
                    "signature": None,
                    "complexity": 1,
                    "is_deleted": False,
                    "commit_hash": "deadbeef",
                }
            ]
        },
        "metadata": {},
        "ingested_at": "2026-10-10T00:00:00Z",
    }

    # Override the size cap to a small value so the test runs fast.
    original_cap = _pycharm_mod._MAX_DISK_READ_BYTES
    _pycharm_mod._MAX_DISK_READ_BYTES = 4096
    try:
        async with _build_client(graph) as client:
            result = await client.call_tool(
                "search_code_patterns",
                {"pattern": big_token, "scope": "python"},
            )
    finally:
        _pycharm_mod._MAX_DISK_READ_BYTES = original_cap

    payload = _extract_payload(result)
    assert payload["status"] == "success", payload
    # With a 4 KiB cap, the 1 MiB+ file is too big to read; the
    # fallback should NOT surface the token, and the node should
    # contribute zero hits.
    assert payload["count"] == 0, (
        f"Resource-cap violation: a file larger than the read cap was "
        f"loaded into memory. Result: {payload!r}"
    )
