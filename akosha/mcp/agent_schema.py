"""Agent metadata schema (Phase 3 of bodai-skill-agent-distribution plan).

Per docs/superpowers/plans/2026-09-14-dhara-mcp-decomposition-implementation.md
Phase 10 task 4, the canonical agent schema now lives in
``mcp_common.canonical_schemas.agent``. This module is a thin re-export
shim that preserves the legacy ``AgentMetadata`` class name + the
helper functions used by tests and tool handlers. New code should
import directly from ``mcp_common.canonical_schemas``.

AkoSHA is the only Bodai component that strictly enforces the B-6
body-integrity invariant (``sha256(system_prompt) == content_hash``)
at the schema layer. Other components enforce the hash at the
agents_tools layer instead. We expose that strict behavior via a
subclass :class:`AgentMetadata` that extends the canonical schema with
the body-integrity model_validator. Callers and tests that import
``akosha.mcp.agent_schema.AgentMetadata`` get the strict version;
callers that want the permissive canonical class can import
``AgentCanonicalSchema`` directly.

Refs:
- docs/superpowers/specs/2026-09-14-dhara-mcp-decomposition-design.md §4.11
- docs/audits/2026-09-15-decomposition-final-review.md §2.1 W4
"""

from __future__ import annotations

from mcp_common.canonical_schemas._validators import (
    NAME_OR_SERVER_RE,
    allowlisted_name,
    build_agent_id,
    coerce_tools_value,
    compute_content_hash,
)
from mcp_common.canonical_schemas.agent import AgentCanonicalSchema
from pydantic import model_validator


class AgentMetadata(AgentCanonicalSchema):
    """AkoSHA's strict variant of the canonical AgentCanonicalSchema.

    Adds the B-6 body-integrity ``model_validator`` that asserts
    ``content_hash == sha256(system_prompt)``. A forged hash is rejected
    at the model boundary; an empty ``system_prompt`` is allowed (the
    agents_tools layer rejects empty bodies before signing).
    """

    @model_validator(mode="after")
    def _validate_body_integrity(self) -> AgentMetadata:
        if not self.system_prompt and not self.content_hash:
            return self
        expected = compute_content_hash(self.system_prompt)
        if self.content_hash and self.content_hash != expected:
            raise ValueError(
                f"content_hash mismatch: declared {self.content_hash!r} "
                f"but sha256(system_prompt)={expected!r}"
            )
        return self


# Re-exported for the agents_tools layer to validate name allowlist at
# the API boundary without importing the schema (mirrors skill_tools).
# The canonical helper is exposed as ``allowlisted_name`` (no leading
# underscore) — the underscore-prefixed alias here preserves the legacy
# name used by ``akosha.mcp.tools.agents_tools``.
_allowlisted_name = allowlisted_name

# The canonical helper is exposed as ``coerce_tools_value`` (no leading
# underscore) — the underscore-prefixed alias here preserves the legacy
# name used by ``akosha.mcp.tools.agents_tools`` to normalize a tools
# entry from the static catalog to ``list[str]``.
_coerce_tools_value = coerce_tools_value


__all__ = [
    "NAME_OR_SERVER_RE",
    "AgentCanonicalSchema",
    "AgentMetadata",
    "_allowlisted_name",
    "_coerce_tools_value",
    "allowlisted_name",
    "build_agent_id",
    "compute_content_hash",
]
