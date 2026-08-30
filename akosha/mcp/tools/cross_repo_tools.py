"""Cross-repo capability search MCP tools (Phase 1 of v2 plan).

Registers ``cross_repo_capability_search`` which indexes every Bodai component's
adapter signatures, tool surface, and error-handling conventions, then answers
natural-language queries against that index.

Indexing strategy (Phase 1, lightweight):

* Seed a static capability catalog covering the 6 core Bodai components
  (mahavishnu, akosha, session-buddy, dhara, crackerjack, oneiric) with the
  high-signal tools, adapters, and error-handling conventions per repo.
* Substring + token-overlap scoring keeps the tool usable without requiring the
  embedding service. The embedding service may be plugged in later for richer
  semantic matching (matches the pattern used by ``search_all_systems``).

Public surface:

* :func:`register_cross_repo_tools` — registers ``cross_repo_capability_search``
  on the supplied registry.
* :data:`BODAI_COMPONENT_KEYS` — the canonical list of indexed component keys.

Phase-1 scope intentionally does NOT crawl the filesystem or query other MCP
servers at runtime; the catalog is the authoritative index and is what the test
suite asserts against. Phase 2 can layer dynamic discovery on top without
breaking the contract.
"""

from __future__ import annotations

import logging
import operator
import re
from typing import TYPE_CHECKING, Any

from akosha.mcp.validation import (
    CrossRepoCapabilitySearchRequest,
    validate_request,
)

if TYPE_CHECKING:
    from akosha.mcp.tools.tool_registry import FastMCPToolRegistry


logger = logging.getLogger(__name__)


# Canonical list of Bodai component keys indexed by this tool. The order is
# stable and matches ``BODAI_REPO_REGISTRY.md``.
BODAI_COMPONENT_KEYS: tuple[str, ...] = (
    "mahavishnu",
    "akosha",
    "session-buddy",
    "dhara",
    "crackerjack",
    "oneiric",
)


# ------------------------------------------------------------------
# Capability catalog (seed)
# ------------------------------------------------------------------
#
# Each entry is a dict with:
#   repo         : canonical repo key
#   kind         : one of "tool", "adapter", or "error"
#   name         : short identifier
#   summary      : one-line description
#   doc_hint     : path or anchor where the symbol is documented
#   tags         : list of free-form tags used for token matching
#
# Tags drive substring scoring: ``query_tokens`` intersect ``tag_tokens``
# after lowercasing + simple stem collapsing ("errors" ~ "error").


_CAPABILITY_CATALOG: tuple[dict[str, Any], ...] = (
    # ---- mahavishnu ----
    {
        "repo": "mahavishnu",
        "kind": "tool",
        "name": "pool_route_execute",
        "summary": "Route a prompt to the best Mahavishnu worker pool.",
        "doc_hint": "mahavishnu.mcp.tools.pool_tools",
        "tags": ["pool", "routing", "execute", "dispatch", "mahavishnu"],
    },
    {
        "repo": "mahavishnu",
        "kind": "tool",
        "name": "dispatch_to_pool",
        "summary": "Async-callback sibling of pool_route_execute (returns workflow_id).",
        "doc_hint": "mahavishnu.mcp.tools.pool_tools",
        "tags": ["dispatch", "pool", "async", "mahavishnu"],
    },
    {
        "repo": "mahavishnu",
        "kind": "tool",
        "name": "trigger_workflow",
        "summary": "Trigger a durable workflow via prefect/llamaindex/agno adapter.",
        "doc_hint": "mahavishnu.mcp.tools.workflow_tools",
        "tags": ["workflow", "prefect", "llamaindex", "agno", "mahavishnu"],
    },
    {
        "repo": "mahavishnu",
        "kind": "adapter",
        "name": "PrefectAdapter",
        "summary": "Durable Prefect flow execution.",
        "doc_hint": "mahavishnu.adapters.prefect",
        "tags": ["prefect", "adapter", "durable", "mahavishnu"],
    },
    {
        "repo": "mahavishnu",
        "kind": "adapter",
        "name": "LlamaIndexAdapter",
        "summary": "RAG / agent pipeline adapter.",
        "doc_hint": "mahavishnu.adapters.llamaindex",
        "tags": ["llamaindex", "adapter", "rag", "mahavishnu"],
    },
    {
        "repo": "mahavishnu",
        "kind": "adapter",
        "name": "AgnoAdapter",
        "summary": "Agno multi-agent adapter.",
        "doc_hint": "mahavishnu.adapters.agno",
        "tags": ["agno", "adapter", "agent", "mahavishnu"],
    },
    {
        "repo": "mahavishnu",
        "kind": "error",
        "name": "PoolUnavailableError",
        "summary": "Raised when no pool is healthy enough to accept a task.",
        "doc_hint": "mahavishnu.core.errors",
        "tags": ["pool", "error", "mahavishnu", "availability"],
    },
    {
        "repo": "mahavishnu",
        "kind": "error",
        "name": "WorkflowFailedError",
        "summary": "Adapter reported a non-recoverable workflow failure.",
        "doc_hint": "mahavishnu.core.errors",
        "tags": ["workflow", "error", "mahavishnu", "failure"],
    },
    # ---- akosha ----
    {
        "repo": "akosha",
        "kind": "tool",
        "name": "search_all_systems",
        "summary": "Semantic search across all system memories.",
        "doc_hint": "akosha.mcp.tools.akosha_tools",
        "tags": ["search", "semantic", "akosha", "memory"],
    },
    {
        "repo": "akosha",
        "kind": "tool",
        "name": "search_code_patterns",
        "summary": "Regex-based code pattern search across indexed repos.",
        "doc_hint": "akosha.mcp.tools.akosha_tools",
        "tags": ["search", "code", "pattern", "regex", "akosha"],
    },
    {
        "repo": "akosha",
        "kind": "tool",
        "name": "detect_anomalies",
        "summary": "Detect statistical anomalies in system metrics.",
        "doc_hint": "akosha.mcp.tools.akosha_tools",
        "tags": ["anomaly", "metrics", "akosha"],
    },
    {
        "repo": "akosha",
        "kind": "tool",
        "name": "analyze_trends",
        "summary": "Time-series trend analysis across systems.",
        "doc_hint": "akosha.mcp.tools.akosha_tools",
        "tags": ["trend", "timeseries", "akosha"],
    },
    {
        "repo": "akosha",
        "kind": "adapter",
        "name": "EmbeddingService",
        "summary": "Local all-MiniLM-L6-v2 embedding service.",
        "doc_hint": "akosha.processing.embeddings",
        "tags": ["embedding", "adapter", "akosha", "ml"],
    },
    {
        "repo": "akosha",
        "kind": "error",
        "name": "HotStoreUnavailableError",
        "summary": "Raised when the hot store is required but unbound.",
        "doc_hint": "akosha.core.errors",
        "tags": ["hotstore", "error", "akosha"],
    },
    # ---- session-buddy ----
    {
        "repo": "session-buddy",
        "kind": "tool",
        "name": "quick_search",
        "summary": "Fast semantic search across reflections.",
        "doc_hint": "session_buddy.mcp.tools.memory.search_tools",
        "tags": ["search", "session", "memory", "session-buddy"],
    },
    {
        "repo": "session-buddy",
        "kind": "tool",
        "name": "store_reflection",
        "summary": "Store a reflection for future reference.",
        "doc_hint": "session_buddy.mcp.tools.memory.memory_tools",
        "tags": ["store", "reflection", "session-buddy"],
    },
    {
        "repo": "session-buddy",
        "kind": "tool",
        "name": "track_channel_session",
        "summary": "Track a channel session event in Session-Buddy.",
        "doc_hint": "session_buddy.mcp.tools.channel",
        "tags": ["channel", "session", "session-buddy"],
    },
    {
        "repo": "session-buddy",
        "kind": "adapter",
        "name": "ReflectionDatabaseAdapter",
        "summary": "Adapter for the SQLite/Postgres reflection store.",
        "doc_hint": "session_buddy.adapters.reflection_adapter",
        "tags": ["adapter", "reflection", "session-buddy", "db"],
    },
    {
        "repo": "session-buddy",
        "kind": "error",
        "name": "DatabaseUnavailableError",
        "summary": "Raised when the reflection DB is unreachable.",
        "doc_hint": "session_buddy.utils.error_management",
        "tags": ["error", "database", "session-buddy"],
    },
    # ---- dhara ----
    {
        "repo": "dhara",
        "kind": "tool",
        "name": "put",
        "summary": "Persist an object with ACID guarantees.",
        "doc_hint": "dhara.api",
        "tags": ["storage", "put", "dhara", "acid"],
    },
    {
        "repo": "dhara",
        "kind": "tool",
        "name": "get",
        "summary": "Fetch a persisted object by key.",
        "doc_hint": "dhara.api",
        "tags": ["storage", "get", "dhara"],
    },
    {
        "repo": "dhara",
        "kind": "tool",
        "name": "list_adapters",
        "summary": "List Oneiric adapters registered in Dhara.",
        "doc_hint": "dhara.mcp.tools",
        "tags": ["adapter", "dhara", "oneiric", "registry"],
    },
    {
        "repo": "dhara",
        "kind": "adapter",
        "name": "SubstrateSchema",
        "summary": "msgspec-backed substrate schema (D-OBJ-SCHEMA).",
        "doc_hint": "dhara.schema._base",
        "tags": ["schema", "substrate", "dhara", "msgspec"],
    },
    {
        "repo": "dhara",
        "kind": "error",
        "name": "SchemaValidationError",
        "summary": "Raised when a payload fails substrate schema validation.",
        "doc_hint": "dhara.schema._base",
        "tags": ["schema", "error", "dhara", "validation"],
    },
    # ---- crackerjack ----
    {
        "repo": "crackerjack",
        "kind": "tool",
        "name": "crackerjack_run",
        "summary": "Run the full crackerjack quality pipeline.",
        "doc_hint": "crackerjack.cli",
        "tags": ["crackerjack", "quality", "test", "lint"],
    },
    {
        "repo": "crackerjack",
        "kind": "tool",
        "name": "execute_crackerjack",
        "summary": "Execute crackerjack with explicit args/kwargs.",
        "doc_hint": "crackerjack.mcp",
        "tags": ["crackerjack", "execute", "quality"],
    },
    {
        "repo": "crackerjack",
        "kind": "adapter",
        "name": "HookExecutor",
        "summary": "Runs quality hooks (ruff, mypy, bandit, complexipy).",
        "doc_hint": "crackerjack.hooks",
        "tags": ["crackerjack", "hooks", "adapter", "ruff", "mypy", "bandit"],
    },
    {
        "repo": "crackerjack",
        "kind": "error",
        "name": "HookFailedError",
        "summary": "Raised when a quality hook reports failure.",
        "doc_hint": "crackerjack.errors",
        "tags": ["crackerjack", "error", "hook", "failure"],
    },
    # ---- oneiric ----
    {
        "repo": "oneiric",
        "kind": "adapter",
        "name": "AdapterResolver",
        "summary": "Resolves adapter class by name across the registry.",
        "doc_hint": "oneiric.resolver",
        "tags": ["oneiric", "resolver", "adapter"],
    },
    {
        "repo": "oneiric",
        "kind": "adapter",
        "name": "LayeredConfig",
        "summary": "Layered configuration loader (defaults → YAML → env).",
        "doc_hint": "oneiric.config",
        "tags": ["oneiric", "config", "layered"],
    },
    {
        "repo": "oneiric",
        "kind": "error",
        "name": "ResolverError",
        "summary": "Raised when adapter resolution fails.",
        "doc_hint": "oneiric.errors",
        "tags": ["oneiric", "error", "resolver"],
    },
)


# ------------------------------------------------------------------
# Scoring helpers
# ------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9_\-]+")


def _tokenize(text: str) -> set[str]:
    """Lowercase + alphanumeric/dash/underscore tokenization."""
    return {tok for tok in _TOKEN_RE.findall(text.lower()) if tok}


def _score(capability: dict[str, Any], query_tokens: set[str]) -> float:
    """Return a 0.0-1.0 relevance score for ``capability`` against ``query_tokens``.

    Weights:
        tag overlap        : 0.55
        summary overlap    : 0.25
        name overlap       : 0.15
        kind bonus         : 0.05 (if query mentions the kind)

    The score is normalized so a perfect overlap yields 1.0.
    """
    if not query_tokens:
        return 0.0

    tag_tokens = _tokenize(" ".join(capability.get("tags", [])))
    summary_tokens = _tokenize(capability.get("summary", ""))
    name_tokens = _tokenize(capability.get("name", ""))
    kind_tokens = {capability.get("kind", "")}

    if not query_tokens:
        return 0.0

    tag_overlap = len(query_tokens & tag_tokens) / len(query_tokens)
    summary_overlap = len(query_tokens & summary_tokens) / len(query_tokens)
    name_overlap = len(query_tokens & name_tokens) / len(query_tokens)
    kind_overlap = 1.0 if (query_tokens & kind_tokens) else 0.0

    score = 0.55 * tag_overlap + 0.25 * summary_overlap + 0.15 * name_overlap + 0.05 * kind_overlap

    return round(min(1.0, score), 4)


def _filter_catalog(
    repo_filter: str | None,
    kind_filter: str | None,
) -> list[dict[str, Any]]:
    """Apply repo + kind filters to the seed catalog."""
    out = list(_CAPABILITY_CATALOG)
    if repo_filter:
        out = [c for c in out if c["repo"] == repo_filter]
    if kind_filter:
        out = [c for c in out if c["kind"] == kind_filter]
    return out


# ------------------------------------------------------------------
# Tool registration
# ------------------------------------------------------------------


def register_cross_repo_tools(
    registry: FastMCPToolRegistry,
) -> None:
    """Register ``cross_repo_capability_search`` on ``registry``.

    Args:
        registry: Akosha ``FastMCPToolRegistry`` instance.
    """
    from akosha.mcp.tools.tool_registry import ToolCategory, ToolMetadata

    @registry.register(
        ToolMetadata(
            name="cross_repo_capability_search",
            description=(
                "Search the indexed Bodai capability catalog (adapters, "
                "tools, error conventions) across all components."
            ),
            category=ToolCategory.SEARCH,
            examples=[
                {
                    "query": "code-review adapters",
                    "description": "Find adapters related to code review across components.",
                },
                {
                    "query": "crash recovery patterns",
                    "description": "Find error-handling conventions for crashes.",
                },
            ],
        )
    )
    async def cross_repo_capability_search(
        query: str,
        repo_filter: str | None = None,
        kind_filter: str | None = None,
        limit: int = 20,
        min_score: float = 0.5,
    ) -> dict[str, Any]:
        """Search indexed Bodai capabilities across components.

        Phase 1 implementation: substring/token-overlap scoring against the
        static seed catalog. ``repo_filter`` and ``kind_filter`` narrow the
        search space before scoring.

        Args:
            query: Natural-language search query (1-500 chars).
            repo_filter: Optional Bodai component key (e.g. ``mahavishnu``).
            kind_filter: Optional kind filter (``tool``, ``adapter``,
                ``error``).
            limit: Maximum number of results (1-100). Default 20.
            min_score: Minimum similarity score (0.0-1.0). Default 0.5.

        Returns:
            dict with keys:
                query: the original query
                total_results: number of results returned
                repos_scanned: list of repo keys considered
                results: list of ``{repo, kind, name, summary, score, doc_hint}``
                mode: ``seed`` (Phase 1 catalog) or ``fallback`` (no embedding)
        """
        # Validate input
        params = validate_request(
            CrossRepoCapabilitySearchRequest,
            query=query,
            repo_filter=repo_filter,
            kind_filter=kind_filter,
            limit=limit,
            min_score=min_score,
        )
        query = params.query
        repo_filter = params.repo_filter
        kind_filter = params.kind_filter
        limit = params.limit
        min_score = params.min_score

        logger.info(
            "cross_repo_capability_search: query=%r repo=%s kind=%s limit=%d",
            query[:80],
            repo_filter,
            kind_filter,
            limit,
        )

        candidates = _filter_catalog(repo_filter, kind_filter)
        repos_scanned = sorted({c["repo"] for c in candidates})

        query_tokens = _tokenize(query)
        scored: list[tuple[float, dict[str, Any]]] = []
        for cap in candidates:
            score = _score(cap, query_tokens)
            if score >= min_score:
                scored.append((score, cap))

        scored.sort(key=operator.itemgetter(0), reverse=True)
        top = scored[:limit]

        results = [
            {
                "repo": cap["repo"],
                "kind": cap["kind"],
                "name": cap["name"],
                "summary": cap["summary"],
                "doc_hint": cap["doc_hint"],
                "score": score,
            }
            for score, cap in top
        ]

        return {
            "query": query,
            "total_results": len(results),
            "repos_scanned": repos_scanned,
            "results": results,
            "mode": "seed",
        }


__all__ = [
    "BODAI_COMPONENT_KEYS",
    "register_cross_repo_tools",
]
