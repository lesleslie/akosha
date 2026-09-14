"""Agent metadata schema (Phase 3 of bodai-skill-agent-distribution plan).

Defines :class:`AgentMetadata`, the canonical Pydantic v2 model for
specialist-agent metadata advertised by every Bodai MCP server. The model
is the wire shape returned by ``mcp__<server>__list_agents`` and embedded
in ``mcp__<server>__get_agent`` responses (alongside ``body``, which
carries the agent's full system prompt — see §11 B-6).

Phase 3 schema ownership
------------------------

Per plan §5 Phase 3, the agent schema is canonical across all 5 Bodai
servers. The ``id`` is built as ``f"{server_key}:{name}:{version}"`` —
mirroring Phase 1's SkillMetadata ``id`` shape so the federation layer
(Phase 4) can aggregate both with the same code path.

Path-traversal allowlist (B-4)
-----------------------------

Per plan §5 task #4 and §11 B-4, ``name`` and ``server_key`` fields are
constrained to the same strict allowlist that SkillMetadata uses:

- ``/`` (anywhere)
- leading ``.`` (cannot start with a dot)
- uppercase characters
- any character outside ``[a-z0-9._-]``
- total length > 63 (the regex ``{0,62}`` after the first char)
- literal substring ``..`` (defense-in-depth)

This module reuses the same regex constant and ``..`` check that
``skill_schema`` defines. The validator runs at construction time and
on attribute assignment (``validate_assignment=True``).

B-6: ``system_prompt`` body integrity
-------------------------------------

Per plan §11 B-6, the agent body IS the system prompt — Claude Code
reads ``system_prompt`` directly, NOT the surrounding metadata. The
field is therefore required to be non-empty by the agent tools layer
(``agents_tools.py`` enforces ``len(system_prompt) >= 1`` before signing).

``content_hash`` is the lowercase hex SHA-256 of the
``system_prompt`` bytes. A :func:`model_validator` asserts the
invariant ``content_hash == sha256(system_prompt)`` so a forged hash
is rejected at the model boundary.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Strict allowlist regex per B-4 / plan §5 task #4. Mirrors
# ``akosha.mcp.skill_schema._NAME_OR_SERVER_RE`` byte-for-byte so the
# same negative tests apply across schemas.
_NAME_OR_SERVER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


class AgentMetadata(BaseModel):
    """Canonical agent metadata advertised by every Bodai MCP server.

    Mirrors :class:`akosha.mcp.skill_schema.SkillMetadata`'s structure
    (identity / routing / signing payload) but carries the agent-specific
    fields that Claude Code reads at load time:

    - ``model`` — ``"sonnet"`` / ``"opus"`` (Claude Code frontmatter)
    - ``tools`` — EXACT tool names (frontmatter, NOT regex)
    - ``system_prompt`` — FULL body (B-6: installer writes verbatim)
    - ``status`` / ``scope`` / ``category`` — picker display + federation

    The ``signature`` and ``server_pubkey_id`` fields are populated by
    :mod:`akosha.mcp.tools.agents_tools` AFTER signing. The canonical
    signing payload is the ``model_dump`` of this model with those two
    fields stripped (see ``canonical_payload_for_signing`` in
    :mod:`akosha.skills_signer`).
    """

    model_config = ConfigDict(
        extra="forbid",
        # ``str_strip_whitespace`` is intentionally NOT set: per Phase 3
        # §11 B-6, the ``system_prompt`` field carries the agent body
        # verbatim and ``content_hash`` covers those exact bytes.
        # Stripping whitespace would invalidate the hash-pin contract
        # when consumers round-trip the metadata through Pydantic.
        # Short text fields (``description``, ``title``, ``category``,
        # ``owner``, ``last_reviewed``) have explicit validators or
        # are taxonomy labels where trimming is not load-bearing.
        # ``validate_assignment`` lets us re-validate when the tool
        # code sets ``metadata.signature`` after construction.
        validate_assignment=True,
    )

    schema_version: Literal[1] = 1

    # ``id`` is the globally unique agent identifier —
    # ``{server_key}:{name}:{version}``. The validator below enforces
    # the allowlist on the constituent fields; ``id`` itself is built
    # from them and is therefore constrained transitively.
    id: str

    # ``server_key`` is the registry key for the originating server
    # (e.g. ``"akosha"``, ``"mahavishnu"``, ``"session-buddy"``,
    # ``"dhara"``, ``"crackerjack"``). Plan §5 Phase 3 explicitly
    # documents this as an *open string* (NOT a ``Literal``) because
    # new Bodai components may join and the federation layer must
    # accept any well-formed key.
    server_key: str

    # ``name`` is the agent's short identifier (e.g. ``"akosha-specialist"``).
    # Constrained to the B-4 allowlist. Note the plan calls out the
    # install path uses ``<server>-<name>.md`` to avoid collisions —
    # ``server_key`` is enforced separately so the *combination* is
    # unique even if two servers happen to share a ``name``.
    name: str

    # ``title`` is the picker display name (e.g. ``"Akosha Specialist"``).
    # Optional — the client can fall back to ``name``.
    title: str | None = None

    # ``description`` follows Claude Code's frontmatter convention:
    # the picker shows the first 1-2 sentences in the tool description.
    # Bounded at 1024 chars (same as SkillMetadata).
    description: str = Field(max_length=1024)

    # Semantic version, required for federation tie-break (Phase 4 sorts
    # by relevance_score DESC, server_key ASC, name ASC — version is the
    # ``data.version`` the client uses to detect changes).
    version: str = "0.0.0"

    # ``model`` is the Claude Code frontmatter ``model`` field —
    # typically ``"sonnet"`` or ``"opus"``. Open string rather than
    # Literal so future models don't require a schema bump.
    model: str

    # ``tools`` is the EXACT list of tool names the agent may invoke
    # (Claude Code frontmatter). Per plan §5 Phase 3 task #1, this is
    # NOT a regex — it's the verbatim list the frontmatter uses.
    tools: list[str] = Field(default_factory=list)

    # ``system_prompt`` is the FULL body. Plan §11 B-6: Claude Code
    # reads this directly, not the surrounding metadata. The default
    # is ``""`` so the schema is constructible in tests, but the
    # agents_tools layer rejects empty values before signing (B-6
    # install path). The :meth:`_validate_body_integrity` model
    # validator additionally asserts that ``content_hash`` matches
    # ``sha256(system_prompt)``.
    system_prompt: str = ""

    # ``dependencies`` are other agent/skill names this agent needs.
    # Mirrors SkillMetadata's field for federation graph traversal.
    dependencies: list[str] = Field(default_factory=list)

    # ``tool_refs`` are MCP tool names referenced by the agent (used
    # by the picker to surface related tools, and by Phase 4 to
    # build relevance scores).
    tool_refs: list[str] = Field(default_factory=list)

    # Audit / governance metadata. All optional — the picker shows
    # them when present.
    category: str | None = None
    owner: str | None = None
    status: Literal["active", "archived", "draft"] | None = None
    last_reviewed: str | None = None
    scope: Literal["user-global", "project-local"] = "user-global"

    # Body integrity. ``content_hash`` is the lowercase hex SHA-256 of
    # ``system_prompt`` bytes. Asserted by :meth:`_validate_body_integrity`
    # so a forged hash is rejected at the boundary.
    content_hash: str

    # Signing payload. Both fields are populated by the tool handler
    # AFTER signing; ``signature`` carries the base64 ed25519 signature
    # and ``server_pubkey_id`` is the 16-char hex ``key_id`` from the
    # server's pubkey manifest.
    signature: str | None = None
    server_pubkey_id: str | None = None

    @field_validator("server_key", "name")
    @classmethod
    def _validate_allowlist(cls, value: str) -> str:
        """Enforce B-4 path-traversal allowlist on ``name`` and ``server_key``.

        Forbids ``/``, leading ``.``, uppercase characters, any character
        outside ``[a-z0-9._-]``, total length > 63, and the literal
        substring ``..`` (defense-in-depth, since the regex already
        forbids leading ``.`` but does not forbid ``..`` in the middle).
        """
        if not _NAME_OR_SERVER_RE.fullmatch(value):
            raise ValueError(
                f"value {value!r} does not match allowlist regex "
                r"'^[a-z0-9][a-z0-9._-]{0,62}$' "
                "(forbidden: '/', uppercase, leading '.', length > 63)"
            )
        if ".." in value:
            raise ValueError(f"value {value!r} contains forbidden substring '..'")
        return value

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str) -> str:
        """Description must be non-empty after stripping whitespace."""
        if not value.strip():
            raise ValueError("description must be non-empty")
        return value

    @field_validator("id")
    @classmethod
    def _validate_id_shape(cls, value: str) -> str:
        """``id`` must be ``{server_key}:{name}:{version}`` with no leading dot or slash.

        The constituent fields are individually validated by their own
        validators; this check ensures the composite matches the
        documented format and disallows extra colons in unexpected places.
        """
        if not value:
            raise ValueError("id must be non-empty")
        parts = value.split(":")
        if len(parts) != 3:
            raise ValueError(
                f"id {value!r} must be 'server_key:name:version' (exactly 3 colon-separated parts)"
            )
        # Reuse the allowlist check on the server_key + name substrings;
        # the version substring uses the same character class but allows
        # a leading ``v`` (e.g. ``v1.0.0``) — so we only check for
        # obviously-forbidden characters.
        server_key, name, version = parts
        if not _NAME_OR_SERVER_RE.fullmatch(server_key):
            raise ValueError(f"id {value!r} has invalid server_key segment {server_key!r}")
        if not _NAME_OR_SERVER_RE.fullmatch(name):
            raise ValueError(f"id {value!r} has invalid name segment {name!r}")
        if not version or "/" in version or ".." in version:
            raise ValueError(f"id {value!r} has invalid version segment {version!r}")
        return value

    @model_validator(mode="after")
    def _validate_body_integrity(self) -> AgentMetadata:
        """B-6 body integrity: ``content_hash`` MUST equal ``sha256(system_prompt)``.

        A forged ``content_hash`` is rejected at the model boundary. Empty
        ``system_prompt`` is allowed at the schema level (so tests can
        construct minimal payloads) but the ``agents_tools`` layer
        rejects empty bodies before signing — the installer needs the
        FULL body to write a working agent file (B-6).
        """
        expected = hashlib.sha256(self.system_prompt.encode("utf-8")).hexdigest()
        if self.content_hash != expected:
            raise ValueError(
                f"content_hash mismatch: declared {self.content_hash!r} "
                f"but sha256(system_prompt)={expected!r}"
            )
        return self


__all__ = ["AgentMetadata"]


# Re-exported for the agents_tools layer to validate name allowlist at
# the API boundary without importing the schema (mirrors skill_tools).
_NAME_ALLOWLIST_RE = _NAME_OR_SERVER_RE


def _allowlisted_name(value: str) -> bool:
    """B-4 API-boundary check on the ``name`` parameter.

    Mirrors the field_validator so failures at the API boundary return
    a uniform error envelope rather than raising past the MCP boundary.
    """
    return bool(_NAME_OR_SERVER_RE.fullmatch(value)) and ".." not in value


def build_agent_id(server_key: str, name: str, version: str) -> str:
    """Build the canonical agent ``id`` field.

    Convenience helper used by the agents_tools catalog builder. NOT a
    validator — assumes ``server_key`` / ``name`` already pass the
    B-4 allowlist. Splitting this out keeps the tool handler free of
    f-string duplication.
    """
    return f"{server_key}:{name}:{version}"


def compute_content_hash(system_prompt: str) -> str:
    """Compute the lowercase hex SHA-256 of ``system_prompt`` bytes.

    Used by the agents_tools catalog builder. Mirrors the
    ``_validate_body_integrity`` invariant so callers can build the
    metadata without re-deriving the hash function.
    """
    return hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()


def _coerce_tools_value(value: Any) -> list[str]:
    """Normalize a tools entry from the static catalog to ``list[str]``.

    The catalog row may carry a single string (rare) or a sequence; we
    always coerce to a list to match the schema's ``list[str]`` field.
    """
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]
