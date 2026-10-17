"""Phase 3 server-published agents tools (Phase 3 of bodai-skill-agent-distribution).

Exposes the ``mcp__akosha__list_agents`` and
``mcp__akosha__get_agent`` tools that advertise the
server-defined specialist agents to the Phase 3 installer and the
Claude Code picker. Agents live as markdown bodies under
``akosha/mcp/tools/agents/<name>.md``.

This module is now a thin server-specific wrapper around
:func:`mcp_common.agents_tools.build_register_agents_tools`. The
factory owns the mechanism — the B-4 allowlist, B-6 body
normalization + content-hash, the wire shape, the success/error
envelope, and the tool registration plumbing. Akosha owns the
policy: the catalog, where bodies live, how the signer is
acquired (lifespan via ``signer_feed.get_signer_feed_state()``),
and the signing function (canonicalization via akosha's local
``canonical_payload_for_signing``).

Security gates (per plan §11):

- **B-1** — every ``get_agent`` response carries an ed25519 signature
  over the canonicalized metadata. The Phase 3 installer verifies
  this before any write to ``~/.claude/agents/akosha-<name>.md``.
- **B-4** — the path-traversal allowlist
  ``^[a-z0-9][a-z0-9._-]{0,62}$`` lives in
  ``mcp_common.canonical_schemas._validators.NAME_OR_SERVER_RE``;
  the factory enforces it at the API boundary. The per-server
  regex copy that used to live here is gone.
- **B-6** — ``get_agent`` returns ``body == system_prompt``. The
  factory computes ``content_hash`` from the stripped body and
  the schema's body-integrity model_validator asserts the hash
  matches the system_prompt bytes.
- **B-7** — each successful tool call invokes ``on_call``, which
  bumps ``SignerFeedState.cycles_total`` via
  :meth:`record_cycle` so the four mandatory feed signals stay
  accurate.

Non-goals:

- Body content is loaded at request time (L-6). No session-start
  pre-load.
- The 3 starter agents ship as static markdown files in
  ``akosha/mcp/tools/agents/``; Phase 4's federation layer
  (``mcp__akosha__list_ecosystem_agents``) is what exposes them
  alongside the other 4 servers' catalogs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp_common.agents_tools import build_register_agents_tools
from mcp_common.canonical_schemas.agent import AgentCanonicalSchema
from mcp_common.signing import SkillsSigner  # re-exported for type compat

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)


# Catalog lives next to this module so deployment paths stay self-
# contained. The catalog is static — new agents are added by dropping a
# new ``.md`` file under ``agents/`` AND adding an entry to
# ``_STATIC_AGENTS``.
_AGENTS_DIR = Path(__file__).parent / "agents"


# Phase 3: 3 starter agents with real content. Each dict is the body's
# filename (relative to ``agents/``), the semantic version, the agent's
# display title, the picker description, the Claude Code model
# (``"sonnet"`` / ``"opus"``), the EXACT list of MCP tool names the
# agent may invoke, and the optional governance metadata.
#
# Adding a new server-published agent: drop ``<name>.md`` under
# ``agents/`` AND add an entry below. The factory's catalog validation
# runs at module load — a missing file or duplicate name fails fast.
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
            "mcp__akosha__search_all_systems, "
            "mcp__akosha__detect_anomalies, "
            "mcp__akosha__correlate_systems, and "
            "mcp__akosha__query_knowledge_graph."
        ),
        "model": "opus",
        "tools": [
            "mcp__akosha__search_all_systems",
            "mcp__akosha__detect_anomalies",
            "mcp__akosha__analyze_trends",
            "mcp__akosha__correlate_systems",
            "mcp__akosha__query_knowledge_graph",
            "Read",
        ],
        "tool_refs": [
            "mcp__akosha__search_all_systems",
            "mcp__akosha__detect_anomalies",
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
            'named Bodai system (e.g. "find anything in session-buddy '
            'about migration"). Narrower than akosha-specialist — '
            "routes through mcp__akosha__search_all_systems "
            "with a system_id filter."
        ),
        "model": "sonnet",
        "tools": [
            "mcp__akosha__search_all_systems",
            "Read",
        ],
        "tool_refs": [
            "mcp__akosha__search_all_systems",
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
            "mcp__akosha__detect_anomalies, "
            "mcp__akosha__analyze_trends, and "
            "mcp__akosha__correlate_systems."
        ),
        "model": "opus",
        "tools": [
            "mcp__akosha__detect_anomalies",
            "mcp__akosha__analyze_trends",
            "mcp__akosha__correlate_systems",
            "mcp__akosha__run_fitness_analysis",
            "Read",
        ],
        "tool_refs": [
            "mcp__akosha__detect_anomalies",
            "mcp__akosha__analyze_trends",
            "mcp__akosha__correlate_systems",
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


# Name-indexed view of the static catalog — built once at module load
# so the body loader and RuntimeFactsBuilder can do O(1) lookups
# instead of scanning the list.
_STATIC_AGENTS_BY_NAME: dict[str, dict[str, Any]] = {
    entry["name"]: entry for entry in _STATIC_AGENTS
}


# --- Body source (server-specific) ------------------------------------


def _read_body(filename: str) -> str:
    """Load an agent body from the catalog, asserting the file exists.

    The factory's catalog validation at module-load time catches
    missing files before any MCP request reaches the runtime path.
    Returns the body as UTF-8 text.
    """
    path = _AGENTS_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(f"Agent body {filename!r} missing from catalog at {path}")
    return path.read_text(encoding="utf-8")


def _normalize_body(raw_body: str) -> str:
    """Strip leading/trailing whitespace so the body matches what
    the canonical schema keeps.

    The schema's AgentCanonicalSchema does NOT set
    ``str_strip_whitespace`` (so the hash is over the exact body
    bytes); the factory's body_loader strips the same way and the
    schema's body-integrity model_validator asserts the hash
    matches.
    """
    return raw_body.strip()


def _body_loader(name: str) -> str:
    """Factory's body source: read the file from disk and normalize.

    The factory's ``_build_unsigned_metadata`` calls this on demand
    for any catalog entry that doesn't carry ``system_prompt``
    inline. The returned string is the canonical body bytes.
    """
    return _normalize_body(_read_body(_STATIC_AGENTS_BY_NAME[name]["body_filename"]))


# --- Metadata builder for RuntimeFactsBuilder -------------------------


def _build_unsigned_metadata(
    name: str,
    server_key: str = _SERVER_KEY,
) -> AgentCanonicalSchema:
    """Build an :class:`AgentCanonicalSchema` for one static agent.

    Kept here (not in the factory) because ``RuntimeFactsBuilder``
    iterates the catalog and needs the same metadata shape for
    the Oneiric boot push. Duplicates the factory's private
    ``_build_unsigned_metadata`` (~15 lines) so the cross-server
    abstraction stays clean; both compute the canonical B-6
    ``content_hash`` from the stripped body.
    """
    from mcp_common.agents_tools import _content_hash, _strip_body  # type: ignore[attr-defined]

    entry = _STATIC_AGENTS_BY_NAME[name]
    body = _strip_body(_read_body(entry["body_filename"]))
    version = entry["version"]
    return AgentCanonicalSchema(
        id=f"{server_key}:{name}:{version}",
        server_key=server_key,
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
        status=entry.get("status", "active"),
        last_reviewed=entry.get("last_reviewed"),
        scope=entry.get("scope", "user-global"),
        content_hash=_content_hash(body),
        schema_version=1,
    )


# --- Signing policy (server-specific) ---------------------------------


def _sign(metadata: AgentCanonicalSchema) -> AgentCanonicalSchema:
    """Apply an ed25519 signature using the lifespan-owned signer.

    The factory hands the unsigned metadata to this callable and
    receives the signed copy. Acquires the signer via
    :func:`akosha.mcp.signer_feed.get_signer_feed_state` (lifespan-
    owned), canonicalizes via akosha's local
    :func:`canonical_payload_for_signing` (which strips the
    ``signature`` + ``server_pubkey_id`` fields before signing so
    the signature doesn't cover itself), and attaches the result.

    Raises if the signer feed state isn't initialized (server
    still starting up, or pre-Phase-1.5 lite mode). The factory
    converts the exception into the standard error envelope.
    """
    from akosha.mcp.signer_feed import get_signer_feed_state
    from akosha.skills_signer import canonical_payload_for_signing

    state = get_signer_feed_state()
    if state is None:
        raise RuntimeError("signer not initialized (server may still be starting up)")
    if state.signer is None:
        # ``SignerFeedState.signer`` is typed ``SkillsSigner | None`` so
        # feed-state tests can construct the state with just a manifest
        # (the four mandatory feed signals don't require the signing
        # capability). ``init_signer_feed_state()`` always installs a
        # real signer in production, so reaching this branch means the
        # caller wired a test-only state into a tool path — fail loud
        # rather than silently producing an unsigned response.
        raise RuntimeError("signer not configured on feed state")

    canonical = canonical_payload_for_signing(metadata.to_dict())
    signed = state.signer.sign(canonical)
    return metadata.model_copy(
        update={
            "signature": signed.signature_b64,
            "server_pubkey_id": signed.key_id,
        }
    )


# --- B-7 cycle bump (server-specific) ---------------------------------


def _on_call() -> None:
    """B-7: bump ``SignerFeedState.cycles_total`` on every tool call.

    The factory invokes this at the start of every list_agents and
    get_agent call so the four mandatory feed signals stay
    accurate. No-op if the signer feed state isn't initialized
    yet (matches the pre-refactor behavior).
    """
    from akosha.mcp.signer_feed import get_signer_feed_state

    state = get_signer_feed_state()
    if state is not None:
        state.record_cycle()


# --- Tool registration ------------------------------------------------


def register_agents_tools(app: FastMCP) -> None:
    """Register ``list_agents`` and ``get_agent`` MCP tools.

    Idempotent at module level (the static catalog is loaded once
    at import). Calling this twice is safe — FastMCP's
    ``@app.tool`` decorator is idempotent within a single
    ``app`` instance.

    Delegates to :func:`mcp_common.agents_tools.build_register_agents_tools`
    with akosha's catalog, body source, signing policy, and
    cycle-bump hook. The wire shape, allowlist enforcement, and
    envelope contract all live in the factory; akosha only owns
    the data and the signer.
    """
    build_register_agents_tools(
        server_key=_SERVER_KEY,
        catalog=_STATIC_AGENTS,
        body_loader=_body_loader,
        sign=_sign,
        on_call=_on_call,
        # tool_name_prefix="" preserves akosha's existing wire names
        # "list_agents" and "get_agent" (no prefix). The hook and
        # any in-flight clients expect the unprefixed names.
    )(app)


__all__ = ["AgentCanonicalSchema", "register_agents_tools"]


# Silence "imported but unused" for the SkillsSigner re-export —
# it preserves the legacy surface for downstream consumers that
# import SkillsSigner from this module (matches the old
# parity-only import in the pre-refactor version).
_ = SkillsSigner
