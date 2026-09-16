"""Agent metadata schema (Phase 3 of bodai-skill-agent-distribution plan).

Per docs/superpowers/plans/2026-09-14-dhara-mcp-decomposition-implementation.md
Phase 10 task 4, the canonical agent schema now lives in
``mcp_common.canonical_schemas.agent``. This module is a thin re-export
shim that preserves the legacy ``AgentMetadata`` class name + the
helper functions used by tests and tool handlers. New code should
import directly from ``mcp_common.canonical_schemas``.

The previous version of this module carried a 22-field model with
B-4 path-traversal allowlist validators, B-6 body-integrity
``model_validator`` (``sha256(system_prompt) == content_hash``), and
``extra="forbid"`` / ``validate_assignment=True`` semantics. All of
these now live in :class:`mcp_common.canonical_schemas.agent.AgentCanonicalSchema`.
This shim preserves ``isinstance(x, AgentMetadata)`` for callers that
import the legacy name (the alias resolves to the same class object).

Refs:
- docs/superpowers/specs/2026-09-14-dhara-mcp-decomposition-design.md §4.11
- docs/audits/2026-09-15-decomposition-final-review.md §2.1 W4
"""

from __future__ import annotations

from typing import Any

from mcp_common.canonical_schemas.agent import AgentCanonicalSchema
from mcp_common.canonical_schemas._validators import (
    NAME_OR_SERVER_RE,
    allowlisted_name,
    build_agent_id,
    coerce_tools_value,
    compute_content_hash,
)

# Backward-compat alias. ``isinstance(x, AgentMetadata)`` resolves to
# ``isinstance(x, AgentCanonicalSchema)`` because Python treats the
# alias as the same class object.
AgentMetadata = AgentCanonicalSchema

# Re-exported for the agents_tools layer to validate name allowlist at
# the API boundary without importing the schema (mirrors skill_tools).
# The canonical helper is exposed as ``allowlisted_name`` (no leading
# underscore) — the underscore-prefixed alias here preserves the legacy
# name used by ``akosha.mcp.tools.agents_tools``.
_allowlisted_name = allowlisted_name


__all__ = [
    "AgentMetadata",
    "NAME_OR_SERVER_RE",
    "_allowlisted_name",
    "allowlisted_name",
    "build_agent_id",
    "compute_content_hash",
    "_coerce_tools_value",
]
