"""Runtime facts builder for the ``akosha_list_agents`` MCP tool (Phase 3 Task 3.1).

Mirrors the legacy inline ``akosha_list_agents`` wire shape so the FastMCP
handler can delegate with ``RuntimeFactsBuilder(self.app).build_all()``.

Wire contract (v1, plan 2026-09-26 §5 task 3.1):

* ``build_all`` returns the legacy LITERAL wire shape:
  ``list[dict[str, Any]]`` where each dict is
  :meth:`AgentMetadata.model_dump` invoked with ``mode="json"`` — i.e.
  the exact same payload served by the pre-Phase-3 inline
  ``akosha_list_agents`` body. The list is ordered to match the
  ``_STATIC_AGENTS`` declaration order so the installer sees a stable
  ordering across boots.

Design rules (mirroring ``mahavishnu/core/runtime_facts.py``):

* No oneiric / mahavishnu / session_buddy imports (stdlib ``logging`` +
  pydantic ``ValidationError`` only). The boot push to Oneiric lives in
  a sibling module :mod:`akosha.mcp.oneiric_runtime`.
* Sync-only introspection; ``build_all`` never ``await``s. Body
  loading is delegated to ``_build_unsigned_metadata`` which reads
  the catalog body file synchronously — by design, the same L-6
  request-time-load semantics the inline tool has today.
* Reads only the module-level static catalog. App-level overrides
  are NOT supported: the catalog is the single source of truth so
  ``akosha_list_agents`` and the Phase 3 boot push can never drift.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

if TYPE_CHECKING:
    from fastmcp import FastMCP


logger = logging.getLogger(__name__)


class RuntimeFactsBuilder:
    """Enumerate the static agent catalog into the legacy ``list_agents`` wire shape.

    The returned ``list[dict[str, Any]]`` matches the canonical wire shape
    served by the pre-Phase-3 inline ``akosha_list_agents`` MCP tool
    body. The Oneiric envelope is NOT constructed in this builder; it
    lives in :mod:`akosha.mcp.oneiric_runtime` so the two surfaces
    (MCP ``list_agents`` vs Oneiric push) can evolve independently.

    The builder is intentionally pure-introspection: ``build_all`` does
    not ``await`` anything and never makes network I/O. Body loading is
    sync (L-6) by the same token the pre-Phase-3 inline body is sync.
    """

    def __init__(self, app: FastMCP) -> None:
        self._app = app

    def build_all(self) -> list[dict[str, Any]]:
        """Return the canonical ``akosha_list_agents`` payload.

        Iterates the static catalog, builds :class:`AgentMetadata` for
        each entry, and returns the ``list[dict]`` wire shape that
        ``akosha_list_agents`` already serves. Per-entry failures
        (``FileNotFoundError``, ``ValidationError``, ``KeyError``) are
        logged and skipped — matching the pre-Phase-3 inline behavior
        exactly so a single broken body file does not poison the whole
        listing.

        Returns
        -------
        list[dict[str, Any]]
            ``[AgentMetadata.model_dump(mode="json"), ...]`` — one
            dict per static agent, in catalog declaration order.
        """
        # Local imports keep the module import-graph flat: this builder
        # is only loaded when Phase 3 wiring activates the
        # ``akosha_list_agents`` body replacement. Importing
        # ``_build_unsigned_metadata`` also brings in the canonical
        # B-6 content_hash validation path; we deliberately re-raise
        # the same exception types so per-entry failure semantics are
        # preserved bit-for-bit.
        from akosha.mcp.tools.agents_tools import (
            _STATIC_AGENTS,
            _build_unsigned_metadata,
        )

        out: list[dict[str, Any]] = []
        for entry in _STATIC_AGENTS:
            try:
                metadata = _build_unsigned_metadata(entry["name"])
            except (FileNotFoundError, ValidationError, KeyError) as exc:
                logger.exception(
                    "list_agents: failed to build metadata for %s: %s",
                    entry["name"],
                    exc,
                )
                continue
            out.append(metadata.model_dump(mode="json"))
        return out
