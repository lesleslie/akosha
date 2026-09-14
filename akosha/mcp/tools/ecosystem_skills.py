"""Phase 4 federation MCP tool for the bodai-skill-agent-distribution plan.

Provides :func:`register_ecosystem_skills`, which registers a single
``akosha_list_ecosystem_skills`` tool that fans out to every Bodai
server's ``list_skills`` MCP endpoint and aggregates the responses
into one :class:`EcosystemSkillsResponse`.

Plan references:
    * §5 Phase 4 (exit criteria + task list)
    * §11 H-1 (resilience requirements: 1s per-server timeout,
      circuit breaker, ``asyncio.gather(return_exceptions=True)``,
      partial-failure visibility)
    * §11 M-6 (reconcile with installed files at ``~/.claude/skills/``)
    * §11 F-2 (cursor-based pagination, day-1)
    * §5 task #5 (deterministic tie-break:
      ``(relevance DESC, server_key ASC, name ASC)``)

Public surface:
    * :func:`register_ecosystem_skills` — registers ``akosha_list_ecosystem_skills``
      on the supplied FastMCP app.
    * :class:`EcosystemSkillsResponse` — the response model (Pydantic v2).
    * :data:`FEDERATION_SERVERS` — the canonical server list with
      transport URLs.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx2 as httpx
from pydantic import BaseModel, ConfigDict, Field

from akosha.mcp.skill_schema import SkillMetadata
from akosha.mcp.tools.ecosystem_skills_cache import (
    EcosystemSkillsCache,
)
from akosha.mcp.tools.ecosystem_skills_circuit import (
    EcosystemCircuitRegistry,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)


# Hard-coded list of the 5 Bodai servers the federation tool fans out to.
# Ports match the canonical Bodai ecosystem portmap (Crackerjack 8676,
# Session-Buddy 8678, Mahavishnu 8680, AkoSHA 8682, Dhara 8683 — akosha
# self-fans to port 8682 below).
#
# The MCP transport is streamable-http. We POST to ``/mcp`` for the JSON-
# RPC handshake per the FastMCP wire spec.
class _FederationServer:
    """Static descriptor for a Bodai MCP server the federation tool can reach.

    Attributes:
        server_key: short stable identifier used as the ``errors`` dict key.
        tool_name: the per-server MCP tool that returns skill metadata.
        base_url: streamable-http base URL.
        description: human-readable summary used in error messages.
    """

    __slots__ = ("base_url", "description", "server_key", "tool_name")

    def __init__(
        self,
        server_key: str,
        tool_name: str,
        base_url: str,
        description: str,
    ) -> None:
        self.server_key = server_key
        self.tool_name = tool_name
        self.base_url = base_url
        self.description = description


def _env(name: str, default: str) -> str:
    """Read environment override or fall back to ``default``."""
    value = os.getenv(name)
    return value or default


FEDERATION_SERVERS: tuple[_FederationServer, ...] = (
    _FederationServer(
        server_key="akosha",
        tool_name="akosha_list_skills",
        base_url=_env("AKOSHA_MCP_URL", "http://localhost:8682/mcp"),
        description="Universal Memory Aggregation System",
    ),
    _FederationServer(
        server_key="mahavishnu",
        tool_name="mahavishnu_list_skills",
        base_url=_env("MAHAVISHNU_MCP_URL", "http://localhost:8680/mcp"),
        description="Bodai orchestrator (Multi-pool, workflow, MCP)",
    ),
    _FederationServer(
        server_key="session-buddy",
        tool_name="session_buddy_list_skills",
        base_url=_env("SESSION_BUDDY_MCP_URL", "http://localhost:8678/mcp"),
        description="Builder (Memory) MCP — semantic search substrate",
    ),
    _FederationServer(
        server_key="dhara",
        tool_name="dhara_list_skills",
        base_url=_env("DHARA_MCP_URL", "http://localhost:8683/mcp"),
        description="Curator (State) — ACID object storage",
    ),
    _FederationServer(
        server_key="crackerjack",
        tool_name="crackerjack_list_skills",
        base_url=_env("CRACKERJACK_MCP_URL", "http://localhost:8676/mcp"),
        description="Inspector (Quality) — repo-wide quality gates",
    ),
)


# Per-server fan-out timeout per H-1 (≤1s). The aggregate call therefore
# has a worst-case latency close to this bound regardless of which server
# is slowest, because ``asyncio.gather(return_exceptions=True)`` does not
# block stragglers once they've timed out.
PER_SERVER_TIMEOUT_SECONDS = 1.0
DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 100


class EcosystemSkillsResponse(BaseModel):
    """Federation response model (canonical shape from plan §5 Phase 4 task #3).

    The model is exposed to clients verbatim — every field is documented
    as part of the wire contract. Optional fields use ``None`` defaults
    so a partially-populated cache snapshot still validates.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: int = 1
    data: list[dict[str, Any]] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)
    per_server_latency_ms: dict[str, int] = Field(default_factory=dict)
    cache: dict[str, Any] = Field(default_factory=dict)
    pagination: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Federation helpers (kept module-private; the tool handler composes them).
# ---------------------------------------------------------------------------


async def _fetch_server_skills(
    server: _FederationServer,
    *,
    timeout: float,
    client: httpx.AsyncClient,
) -> tuple[list[SkillMetadata], int]:
    """Fetch one server's ``list_skills`` over streamable-http.

    Returns ``(skills, latency_ms)``. Raises on any error — the caller
    wraps the call in the circuit breaker + ``return_exceptions``.
    """
    start = time.monotonic()
    request_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": server.tool_name, "arguments": {}},
    }
    response = await client.post(
        server.base_url,
        json=request_body,
        timeout=timeout,
    )
    response.raise_for_status()
    body = response.json()
    elapsed_ms = int((time.monotonic() - start) * 1000)
    items = _extract_skill_items(body)
    return items, elapsed_ms


def _extract_skill_items(body: dict[str, Any]) -> list[SkillMetadata]:
    """Unwrap a FastMCP JSON-RPC ``tools/call`` response into ``SkillMetadata`` items.

    FastMCP returns ``{"result": {"content": [{"type": "text", "text": "..."}]}}``
    where ``text`` is a JSON string of the tool's actual return value. For
    federation, the actual return value is ``list[dict]`` matching
    :class:`SkillMetadata`. Return only the successfully-parsed items so a
    single schema miss doesn't poison the whole server's response.
    """
    content = body.get("result", {}).get("content", [])
    if not isinstance(content, list) or not content:
        return []
    first = content[0]
    if not isinstance(first, dict) or first.get("type") != "text":
        return []
    import json

    raw_text = first.get("text", "")
    if not isinstance(raw_text, str):
        return []
    try:
        parsed = json.loads(raw_text)
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    out: list[SkillMetadata] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        try:
            out.append(SkillMetadata.model_validate(entry))
        except Exception as exc:  # Pydantic ValidationError or any other
            logger.debug(
                "skipping skill entry from federation: %s (%s)",
                entry.get("id", "<unknown>"),
                exc,
            )
            continue
    return out


def _score_for_ranking(skill: SkillMetadata, query_tokens: set[str]) -> float:
    """Lexical overlap score used for sort + pagination.

    Mirrors ``cross_repo_tools._score`` weight hints: tokens intersecting
    description + name drive the ranking. Tokens that hit ``tool_refs``
    count as a bonus. Returns ``0.0`` when ``query_tokens`` is empty so
    the unfiltered list stays stable.
    """
    if not query_tokens:
        return 0.0
    haystacks = [skill.description.lower(), skill.name.lower()]
    tool_ref_blob = " ".join(skill.tool_refs).lower()
    text_blob = " ".join([*haystacks, tool_ref_blob])
    text_tokens = {tok for tok in text_blob.replace(":", " ").replace("/", " ").split() if tok}
    overlap = len(query_tokens & text_tokens)
    if overlap == 0:
        return 0.0
    return round(min(1.0, overlap / max(1, len(query_tokens))), 4)


def _paginate(
    scored: list[tuple[SkillMetadata, float]],
    *,
    limit: int,
    cursor: str | None,
) -> tuple[list[dict[str, Any]], str | None, bool]:
    """Cursor-paginate ``scored`` into ``(page, next_cursor, has_more)``.

    The cursor encodes the absolute index into ``scored`` (after sort);
    passing ``cursor=None`` returns the first page. Items are dropped
    via the cursor anchor (exclusive), then sliced to ``limit``.
    """
    sorted_items = sorted(
        scored,
        key=lambda pair: (-pair[1], pair[0].server, pair[0].name),
    )
    start = 0
    if cursor:
        try:
            start = max(0, int(cursor))
        except ValueError:
            start = 0
    end = start + limit
    page = sorted_items[start:end]
    next_cursor = str(end) if end < len(sorted_items) else None
    has_more = next_cursor is not None
    payload = [skill.model_dump(mode="json") for skill, _score in page]
    return payload, next_cursor, has_more


def _load_installed_skill_paths() -> list[Path]:
    """Return the paths of locally installed skills (M-6 reconciliation).

    Reads ``~/.claude/skills/.install-manifest.json`` if present — the
    manifest is the canonical source per plan §5 task #4 (B-3) — and
    falls back to scanning the directory. Each entry becomes a stub
    :class:`SkillMetadata` so the federation response can surface
    "installed but no longer in any server's catalog" entries as
    ``content_type: 'skill'`` with an ``archived`` tag.
    """
    skills_dir = Path("~/.claude/skills").expanduser()
    if not skills_dir.is_dir():
        return []
    paths: list[Path] = []
    manifest_path = skills_dir / ".install-manifest.json"
    if manifest_path.is_file():
        try:
            import json

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            manifest = {}
        if isinstance(manifest, dict):
            for entry in manifest.values():
                if isinstance(entry, dict):
                    raw_path = entry.get("install_path")
                    if isinstance(raw_path, str):
                        path = Path(raw_path).expanduser()
                        if path.is_file():
                            paths.append(path)
    for child in skills_dir.glob("*/SKILL.md"):
        if child.is_file() and child not in paths:
            paths.append(child)
    return paths


def _installed_to_metadata(path: Path) -> SkillMetadata | None:
    """Convert an installed ``SKILL.md`` to a metadata stub for M-6 recon.

    Reads frontmatter (or the first ``# `` heading) and builds a minimal
    :class:`SkillMetadata` with ``content_hash = sha256(body)``. Returns
    ``None`` when the file cannot be parsed or violates the B-4 allowlist.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    name = path.parent.name
    description = ""
    if text.startswith("---"):
        end = text.find("\n---", 4)
        if end > 0:
            header = text[4:end]
            for line in header.splitlines():
                stripped = line.strip()
                if stripped.startswith("description:"):
                    description = stripped.split(":", 1)[1].strip().strip('"\'')
                    break
    if not description:
        for line in text.splitlines():
            if line.strip().startswith("# "):
                description = line.strip()[2:].strip()
                break
    if not description:
        description = name
    try:
        server_key, _, skill_name = name.partition("-")
        if not server_key or not skill_name:
            return None
        import re as _re

        allowlist = _re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
        if not allowlist.fullmatch(server_key) or not allowlist.fullmatch(skill_name):
            return None
        body_bytes = text.encode("utf-8")
        return SkillMetadata(
            id=f"{server_key}:{skill_name}:installed",
            server=server_key,
            name=skill_name,
            description=description[:1024],
            version="0.0.0",
            tool_refs=[],
            dependencies=[],
            content_type="skill",
            content_hash=hashlib.sha256(body_bytes).hexdigest(),
            body_size=len(body_bytes),
            body_format="yaml-frontmatter+markdown",
            allowed_tools=[],
            timestamp=datetime.now(UTC).timestamp(),
        )
    except Exception as exc:
        logger.debug("could not build installed stub for %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def register_ecosystem_skills(
    app: FastMCP,
    *,
    cache: EcosystemSkillsCache | None = None,
    circuit: EcosystemCircuitRegistry | None = None,
    http_client_factory: Any = None,
) -> None:
    """Register ``akosha_list_ecosystem_skills`` on ``app``.

    Args:
        app: FastMCP application.
        cache: optional file-based cache override (default: module singleton
            at ``~/.akosha/cache/ecosystem_skills.json``).
        circuit: optional circuit-breaker registry override (default:
            module singleton — one breaker per server key).
        http_client_factory: optional callable returning a fresh
            :class:`httpx.AsyncClient`; defaults to ``httpx.AsyncClient``.
            Tests inject a mock client here.
    """
    cache_singleton: EcosystemSkillsCache = cache if cache is not None else EcosystemSkillsCache()
    circuit_singleton: EcosystemCircuitRegistry = (
        circuit if circuit is not None else EcosystemCircuitRegistry()
    )
    client_factory = http_client_factory

    @app.tool(name="akosha_list_ecosystem_skills")
    async def akosha_list_ecosystem_skills(
        query: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        cursor: str | None = None,
        include_installed: bool = True,
    ) -> dict[str, Any]:
        """List skills across all 5 Bodai servers with partial-failure visibility.

        Fans out to ``mcp__<server>__list_skills`` on every configured
        Bodai server (akosha, mahavishnu, session-buddy, dhara,
        crackerjack). Each call has a 1-second budget; per-server
        circuit breakers skip a server that has failed ≥3 times in the
        last 30 seconds. Failures surface in the ``errors`` dict so the
        caller can distinguish "server down" from "server returned
        nothing".

        Args:
            query: optional natural-language filter; entries are ranked
                by token-overlap score.
            limit: max entries per page (default 20, max 100).
            cursor: opaque pagination cursor returned by a previous call.
            include_installed: when ``True``, locally installed skills
                (``~/.claude/skills/<server>-*/SKILL.md``) are merged
                into the response so the picker can surface them even
                when the originating server has dropped them from its
                catalog (M-6).

        Returns:
            dict matching :class:`EcosystemSkillsResponse` (``data``,
            ``errors``, ``per_server_latency_ms``, ``cache``,
            ``pagination``).
        """
        # Coerce + clamp page-size early so the cached-payload
        # signature lines up with the canonical request shape.
        if limit <= 0:
            limit = DEFAULT_PAGE_LIMIT
        if limit > MAX_PAGE_LIMIT:
            limit = MAX_PAGE_LIMIT

        cache_key = _cache_key(query=query, limit=limit, cursor=cursor, include=include_installed)

        snapshot, stale = cache_singleton.get(cache_key)
        if snapshot is not None and not stale:
            cached = dict(snapshot)
            cached.setdefault("cache", {})
            cached["cache"] = {
                "fetched_at": cached.get("_fetched_at"),
                "ttl_seconds": cache_singleton.ttl_seconds,
                "stale": False,
            }
            cached.pop("_fetched_at", None)
            return cached

        # Live fan-out. The client is created per call to avoid cross-
        # call bleed when many concurrent requests share the singleton
        # state (httpx.AsyncClient's connection pool is fine but a
        # per-call fresh client keeps the test injection trivial).
        async with _make_client(client_factory) as client:
            tasks = [
                _gather_one(
                    server,
                    circuit=circuit_singleton,
                    client=client,
                )
                for server in FEDERATION_SERVERS
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        all_skills: list[SkillMetadata] = []
        errors: dict[str, str] = {}
        per_server_latency_ms: dict[str, int] = {}
        for server, outcome in zip(FEDERATION_SERVERS, results, strict=True):
            if isinstance(outcome, BaseException):
                errors[server.server_key] = f"{type(outcome).__name__}: {outcome}"
                per_server_latency_ms[server.server_key] = _TIMEOUT_MS
                continue
            skills, latency_ms = outcome
            per_server_latency_ms[server.server_key] = latency_ms
            all_skills.extend(skills)

        # M-6 reconciliation: installed files surface even when not
        # advertised by any server. We dedup against ``all_skills`` by
        # ``(server, name)`` so a re-installed skill does not double up.
        if include_installed:
            advertised = {(skill.server, skill.name) for skill in all_skills}
            for path in _load_installed_skill_paths():
                installed = _installed_to_metadata(path)
                if installed is None:
                    continue
                key = (installed.server, installed.name)
                if key in advertised:
                    continue
                advertised.add(key)
                all_skills.append(installed)

        query_tokens = _tokenize(query or "")
        scored: list[tuple[SkillMetadata, float]] = [
            (skill, _score_for_ranking(skill, query_tokens)) for skill in all_skills
        ]
        page_data, next_cursor, has_more = _paginate(scored, limit=limit, cursor=cursor)

        payload = EcosystemSkillsResponse(
            schema_version=1,
            data=page_data,
            errors=errors,
            per_server_latency_ms=per_server_latency_ms,
            cache={
                "fetched_at": time.time(),
                "ttl_seconds": cache_singleton.ttl_seconds,
                "stale": False,
            },
            pagination={
                "next_cursor": next_cursor,
                "has_more": has_more,
                "limit": limit,
            },
        ).model_dump(mode="json")

        # Cache only on a fully-resolved fan-out (errors dict may be
        # non-empty — partial failures ARE cacheable, they were just
        # observed). Stale-served responses were already returned above.
        cache_singleton.put(cache_key, payload)
        return payload


async def _gather_one(
    server: _FederationServer,
    *,
    circuit: EcosystemCircuitRegistry,
    client: httpx.AsyncClient,
) -> tuple[list[SkillMetadata], int]:
    """Fan-out one server, going through the circuit breaker first.

    On a closed breaker this is just a thin wrapper around
    :func:`_fetch_server_skills`. On an open breaker we raise
    :class:`CircuitBreakerOpen` immediately so the outer
    ``return_exceptions`` picks it up and the caller logs it under
    ``errors[server_key]``.
    """
    breaker = circuit.for_server(server.server_key)
    breaker.allow()
    try:
        skills, latency_ms = await _fetch_server_skills(
            server,
            timeout=PER_SERVER_TIMEOUT_SECONDS,
            client=client,
        )
    except Exception:
        breaker.record_failure()
        raise
    breaker.record_success()
    return skills, latency_ms


def _make_client(factory: Any) -> Any:
    """Return an async context manager yielding an :class:`httpx.AsyncClient`.

    ``factory`` is test-friendly: when callable, it is invoked with no
    args and must return an object usable in ``async with``. Otherwise
    we build the default client.
    """
    if factory is not None and callable(factory):
        ctx = factory()
        return _AsyncClientContext(ctx)
    default = httpx.AsyncClient(timeout=PER_SERVER_TIMEOUT_SECONDS)
    return _AsyncClientContext(default)


class _AsyncClientContext:
    """Adapter so any async-context-manager object can be ``async with``-ed.

    The default branch already returns an :class:`httpx.AsyncClient`,
    which is itself an async context manager; this wrapper accepts both
    the client and any context-manager-shaped test double (e.g.
    ``MagicMock`` with ``__aenter__``/``__aexit__``).
    """

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx

    async def __aenter__(self) -> Any:
        return await self._ctx.__aenter__()

    async def __aexit__(self, *args: Any) -> Any:
        return await self._ctx.__aexit__(*args)


def _cache_key(*, query: str | None, limit: int, cursor: str | None, include: bool) -> str:
    """Stable cache key for the (query, page, installed) request shape.

    Encodes the inputs as a short base64 token so the file-on-disk view
    is greppable; collisions across request shapes are not a concern
    because the breaker / TTL govern staleness.
    """
    raw = f"{query or ''}|{limit}|{cursor or ''}|{int(include)}".encode()
    return base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")[:24]


def _tokenize(text: str) -> set[str]:
    """Tokenize ``text`` for ranking. Mirrors ``cross_repo_tools._tokenize``."""
    out: set[str] = set()
    for token in text.lower().split():
        cleaned = "".join(ch for ch in token if ch.isalnum() or ch in "-_")
        if cleaned:
            out.add(cleaned)
    return out


# Sentinel used to surface a per-server call that errored without a
# measured latency (the breaker + ``return_exceptions`` runs before any
# successful measurement).
_TIMEOUT_MS = 0


__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "FEDERATION_SERVERS",
    "MAX_PAGE_LIMIT",
    "PER_SERVER_TIMEOUT_SECONDS",
    "EcosystemSkillsResponse",
    "register_ecosystem_skills",
]
