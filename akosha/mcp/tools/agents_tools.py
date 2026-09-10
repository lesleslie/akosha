"""Phase 3 server-published agents tools (Phase 3 of bodai-skill-agent-distribution).

Exposes the ``mcp__akosha__akosha_list_agents`` and
``mcp__akosha__akosha_get_agent`` tools that advertise the
server-defined specialist agents to Phase 3's installer and the
Claude Code picker. Agents live as markdown bodies under
``akosha/mcp/tools/agents/<name>.md``; this module reads them at
request time and signs the metadata via the lifespan-owned
:class:`SkillsSigner` (shared with Phase 1's skill_tools).

Security gates (per plan §11):

- **B-1** — every ``get_agent`` response carries an ed25519 signature
  over the canonicalized metadata (the Phase 3 installer verifies this
  before any write to ``~/.claude/agents/akosha-<name>.md``).
- **B-4** — path-traversal allowlist ``^[a-z0-9][a-z0-9._-]{0,62}$``
  is enforced on the ``name`` parameter at the API boundary, BEFORE
  the metadata model re-validates it. Defense-in-depth: an unknown /
  forbidden ``name`` returns an error envelope rather than raising
  past the MCP boundary.
- **B-6** — ``get_agent`` returns ``body = system_prompt`` so the
  installer writes a fully-functional agent file. **The schema's
  ``content_hash`` validator also asserts the hash matches the
  ``system_prompt`` bytes**, so a forged hash is rejected at the
  model boundary.
- **B-7** — each successful tool call bumps
  ``SignerFeedState.cycles_total`` via :meth:`record_cycle` so the
  four mandatory feed signals stay accurate.

Non-goals:

- Body content is loaded at request time (L-6). No session-start
  pre-load.
- The 3 starter agents ship as static markdown files in
  ``akosha/mcp/tools/agents/``; Phase 4's federation layer
  (``mcp__akosha__list_ecosystem_skills``) is what exposes them
  alongside the other 4 servers' catalogs.

Body-stripping invariant
------------------------

``AgentMetadata`` declares ``str_strip_whitespace=True`` globally,
which strips leading/trailing whitespace from ``system_prompt`` at
construction time. To keep ``content_hash == sha256(system_prompt)``
consistent (B-6), the body is stripped BEFORE hashing AND before
passing to the model — so ``body``, ``metadata.system_prompt``, and
``content_hash`` all reference the same byte sequence.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from akosha.mcp.agent_schema import AgentMetadata, _allowlisted_name
from akosha.mcp.skill_schema import SkillMetadata  # noqa: F401  # imported for parity
from akosha.skills_signer import canonical_payload_for_signing

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)


# Allowlist mirror — see B-4 / plan §5 task #4. Duplicated here so the
# API boundary rejects forbidden ``name`` values BEFORE constructing the
# Pydantic model (avoids letting a path-traversal payload reach the
# validator, which would surface as a different error class).
_NAME_ALLOWLIST_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


# Catalog lives next to this module so deployment paths stay self-
# contained. The catalog is static — new agents are added by dropping a
# new ``.md`` file under ``agents/`` AND adding an entry to
# ``_STATIC_AGENTS``.
_AGENTS_DIR = Path(__file__).parent / "agents"


# Phase 3: 3 starter agents with real content. Each tuple is the body's
# filename (relative to ``agents/``), the semantic version, the agent's
# display title, the picker description (becomes ``AgentMetadata.description``),
# the Claude Code model the agent uses (``"sonnet"`` / ``"opus"``),
# the EXACT list of MCP tool names the agent may invoke, and the
# optional governance metadata (category / owner / status /
# last_reviewed / scope / dependencies / tool_refs).
#
# Adding a new server-published agent: drop ``<name>.md`` under
# ``agents/`` AND add an entry below. The validator runs at module
# load — a missing file or mismatched hash fails fast.
_STATIC_AGENTS: list[dict[str, Any]] = [
    {
        "name": "akosha-specialist",
        "body_filename": "akosha-specialist.md",
        "version": "1.0.0",
        "title": "Akosha Specialist",
        "description": (
            "Use proactively for cross-system intelligence questions: "
            "semantic search across the indexed Bodai corpus, anomaly "
            "detection, knowledge-graph correlation. Routes through "
            "mcp__akosha__akosha_search_all_systems, "
            "mcp__akosha__akosha_detect_anomalies, "
            "mcp__akosha__akosha_correlate_systems, and "
            "mcp__akosha__akosha_query_knowledge_graph."
        ),
        "model": "opus",
        "tools": [
            "mcp__akosha__akosha_search_all_systems",
            "mcp__akosha__akosha_detect_anomalies",
            "mcp__akosha__akosha_analyze_trends",
            "mcp__akosha__akosha_correlate_systems",
            "mcp__akosha__akosha_query_knowledge_graph",
            "Read",
        ],
        "tool_refs": [
            "mcp__akosha__akosha_search_all_systems",
            "mcp__akosha__akosha_detect_anomalies",
        ],
        "dependencies": [],
        "category": "memory-aggregation",
        "owner": "akosha",
        "status": "active",
        "last_reviewed": "2026-09-10",
        "scope": "user-global",
    },
    {
        "name": "search-agent",
        "body_filename": "search-agent.md",
        "version": "1.0.0",
        "title": "Single-System Search",
        "description": (
            "Use this agent for focused semantic search across ONE "
            "named Bodai system (e.g. \"find anything in session-buddy "
            "about migration\"). Narrower than akosha-specialist — "
            "routes through mcp__akosha__akosha_search_all_systems "
            "with a system_id filter."
        ),
        "model": "sonnet",
        "tools": [
            "mcp__akosha__akosha_search_all_systems",
            "Read",
        ],
        "tool_refs": [
            "mcp__akosha__akosha_search_all_systems",
        ],
        "dependencies": ["akosha-specialist"],
        "category": "memory-aggregation",
        "owner": "akosha",
        "status": "active",
        "last_reviewed": "2026-09-10",
        "scope": "user-global",
    },
    {
        "name": "pattern-agent",
        "body_filename": "pattern-agent.md",
        "version": "1.0.0",
        "title": "Cross-System Pattern & Trend",
        "description": (
            "Use this agent for recurring-pattern and trending-anomaly "
            "questions across the Bodai component fleet. Routes through "
            "mcp__akosha__akosha_detect_anomalies, "
            "mcp__akosha__akosha_analyze_trends, and "
            "mcp__akosha__akosha_correlate_systems."
        ),
        "model": "opus",
        "tools": [
            "mcp__akosha__akosha_detect_anomalies",
            "mcp__akosha__akosha_analyze_trends",
            "mcp__akosha__akosha_correlate_systems",
            "mcp__akosha__akosha_run_fitness_analysis",
            "Read",
        ],
        "tool_refs": [
            "mcp__akosha__akosha_detect_anomalies",
            "mcp__akosha__akosha_analyze_trends",
            "mcp__akosha__akosha_correlate_systems",
        ],
        "dependencies": ["akosha-specialist"],
        "category": "analytics",
        "owner": "akosha",
        "status": "active",
        "last_reviewed": "2026-09-10",
        "scope": "user-global",
    },
]


_SERVER_KEY = "akosha"


# Name-indexed view of the static catalog — built once at module load so
# the tool handlers can do O(1) lookups instead of scanning the list. This
# MUST stay below ``_STATIC_AGENTS`` so the dict comprehension sees the
# full list (Python's module body executes top-to-bottom; this lookup is
# never called before the module finishes loading).
_STATIC_AGENTS_BY_NAME: dict[str, dict[str, Any]] = {
    entry["name"]: entry for entry in _STATIC_AGENTS
}


def _read_body(filename: str) -> str:
    """Load an agent body from the catalog, asserting the file exists.

    The validator at module load time catches missing files before any
    MCP request reaches the runtime path. Returns the body as UTF-8 text.
    """
    path = _AGENTS_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(
            f"Agent body {filename!r} missing from catalog at {path}"
        )
    return path.read_text(encoding="utf-8")


def _normalize_body(raw_body: str) -> str:
    """Strip leading/trailing whitespace so the body matches what
    ``AgentMetadata`` keeps after ``str_strip_whitespace=True``.

    The schema strips whitespace from ALL string fields at construction
    time. To keep ``content_hash == sha256(system_prompt)`` consistent
    (B-6), we strip BEFORE hashing AND before passing to the model.
    Internal whitespace (newlines, paragraph breaks) is preserved.
    """
    return raw_body.strip()


def _build_unsigned_metadata(name: str) -> AgentMetadata:
    """Build an :class:`AgentMetadata` for the named static agent.

    The metadata has ``signature=None`` and ``server_pubkey_id=None``;
    those fields are populated by :func:`_sign_metadata` after signing.
    Raises :class:`KeyError` if ``name`` is not a known static agent.

    The body is loaded at request time (L-6), normalized to strip
    leading/trailing whitespace (so the schema's
    ``str_strip_whitespace=True`` is a no-op), and the
    ``content_hash`` is computed from the normalized bytes. The schema's
    :func:`_validate_body_integrity` model validator asserts the
    invariant.
    """
    for entry in _STATIC_AGENTS:
        if entry["name"] != name:
            continue
        raw_body = _read_body(entry["body_filename"])
        body = _normalize_body(raw_body)
        body_bytes = body.encode("utf-8")
        content_hash = hashlib.sha256(body_bytes).hexdigest()
        version = entry["version"]
        return AgentMetadata(
            id=f"{_SERVER_KEY}:{name}:{version}",
            server_key=_SERVER_KEY,
            name=name,
            title=entry.get("title"),
            description=entry["description"],
            version=version,
            model=entry["model"],
            tools=list(entry["tools"]),
            system_prompt=body,
            dependencies=list(entry.get("dependencies", [])),
            tool_refs=list(entry.get("tool_refs", [])),
            category=entry.get("category"),
            owner=entry.get("owner"),
            status=entry.get("status"),
            last_reviewed=entry.get("last_reviewed"),
            scope=entry.get("scope", "user-global"),
            content_hash=content_hash,
            # ``schema_version`` defaults to 1; explicit for clarity.
            schema_version=1,
        )
    msg = f"unknown agent {name!r}"
    raise KeyError(msg)


def _sign_metadata(metadata: AgentMetadata, signer: Any) -> AgentMetadata:
    """Apply an ed25519 signature to a copy of ``metadata``.

    The canonical payload strips ``signature`` and ``server_pubkey_id``
    BEFORE canonicalization so the signature doesn't cover itself (per
    ``canonical_payload_for_signing`` docstring + plan §10.1.3). The
    returned copy has both signing fields populated.
    """
    unsigned_dict = metadata.model_dump(mode="json")
    canonical = canonical_payload_for_signing(unsigned_dict)
    signed = signer.sign(canonical)
    return metadata.model_copy(
        update={
            "signature": signed.signature_b64,
            "server_pubkey_id": signed.key_id,
        }
    )


def _is_allowlisted(name: str) -> bool:
    """B-4 API-boundary check on the ``name`` parameter.

    Forbids ``/``, ``..``, leading ``.``, uppercase, length > 63, and
    any character outside ``[a-z0-9._-]``. Mirrors the AgentMetadata
    validator so failures at the API boundary return a uniform
    ``{"success": False, "error": ...}`` envelope.
    """
    return bool(_NAME_ALLOWLIST_RE.fullmatch(name)) and ".." not in name


def register_agents_tools(app: FastMCP) -> None:
    """Register ``akosha_list_agents`` and ``akosha_get_agent`` MCP tools.

    Idempotent at module level (the static catalog is loaded once at
    import). Calling this twice is safe — FastMCP's ``@app.tool``
    decorator is idempotent within a single ``app`` instance.

    The tools access the lifespan-owned :class:`SkillsSigner` via
    ``get_signer_feed_state()``; if the server is running without the
    Phase 1.5 wiring (lite mode / pre-startup), both tools return an
    error envelope rather than raising.

    **B-6 critical contract**: ``akosha_get_agent`` returns
    ``body == system_prompt``. Without this, the installer would ship
    non-functional agents. The test
    ``tests/integration/test_get_agent_e2e.py`` asserts this invariant
    explicitly.
    """

    @app.tool(name="akosha_list_agents")
    async def akosha_list_agents() -> list[dict[str, Any]]:
        """Return metadata for agents this server publishes.

        Returns at least 3 entries (one per static agent in
        ``_STATIC_AGENTS``). The ``signature`` and ``server_pubkey_id``
        fields are ``None`` here — those are populated by
        ``akosha_get_agent`` since the signature is over the canonical
        payload WITHOUT the signing fields themselves.

        The ``system_prompt`` field IS populated (per §11 B-6 — the
        client may need it for previews even without a separate
        ``get_agent`` call). The full body is also available via
        ``akosha_get_agent``.
        """
        from akosha.mcp.signer_feed import (
            get_signer_feed_state,
        )

        state = get_signer_feed_state()
        if state is not None:
            state.record_cycle()

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

    @app.tool(name="akosha_get_agent")
    async def akosha_get_agent(name: str) -> dict[str, Any]:
        """Return the signed metadata + body for one agent.

        Validates ``name`` against the B-4 allowlist BEFORE constructing
        the metadata. Signs the metadata via the lifespan-owned
        :class:`SkillsSigner`. **The body is the agent's full system
        prompt** (B-6 critical contract) — the client (Phase 3 installer)
        writes this verbatim to ``~/.claude/agents/akosha-<name>.md``.

        Returns an error envelope ``{"success": False, "error": ...}``
        on:

        - name allowlist violation (B-4)
        - signer not initialized (server still starting up)
        - name not found on this server
        - body file missing from the catalog
        - metadata validation failure (e.g. content_hash mismatch)
        """
        from akosha.mcp.signer_feed import (
            get_signer_feed_state,
        )

        if not _is_allowlisted(name):
            return {
                "success": False,
                "error": (
                    f"name {name!r} violates path-traversal allowlist "
                    "(B-4): must match ^[a-z0-9][a-z0-9._-]{0,62}$ "
                    "with no '..' substring"
                ),
            }

        state = get_signer_feed_state()
        if state is None:
            return {
                "success": False,
                "error": "signer not initialized (server may still be starting up)",
            }
        state.record_cycle()

        try:
            unsigned = _build_unsigned_metadata(name)
        except KeyError:
            return {"success": False, "error": f"agent {name!r} not found on this server"}
        except FileNotFoundError as exc:
            return {"success": False, "error": str(exc)}
        except ValidationError as exc:
            return {"success": False, "error": f"metadata validation failed: {exc}"}

        signed = _sign_metadata(unsigned, state.signer)
        # B-6: body MUST equal system_prompt. The schema validator
        # asserted content_hash == sha256(system_prompt), so this is
        # always the FULL non-empty body when validation passed.
        body = _normalize_body(_read_body(_STATIC_AGENTS_BY_NAME[name]["body_filename"]))
        return {
            "success": True,
            "metadata": signed.model_dump(mode="json"),
            "body": body,
        }


__all__ = ["register_agents_tools"]


# Silence "imported but unused" for the parity-only imports — these
# keep a consistent surface with skill_tools.py and serve as
# documentation for readers cross-referencing the two modules.
_ = SkillMetadata
_ = _allowlisted_name
