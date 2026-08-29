"""Tests for ``akosha.mcp.tools.cross_repo_tools`` (Phase 1).

Covers:
    * Tokenization + scoring helpers (``_tokenize``, ``_score``)
    * ``_filter_catalog`` repo/kind narrowing
    * ``cross_repo_capability_search`` tool: registration, basic call shape,
      repo filter, kind filter, multi-component results
    * Profile wiring: ``register_cross_repo_group`` registers the tool on
      a dummy FastMCP app.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from akosha.mcp.tools.cross_repo_tools import (
    BODAI_COMPONENT_KEYS,
    _filter_catalog,
    _score,
    _tokenize,
    register_cross_repo_tools,
)
from akosha.mcp.tools.tool_registry import FastMCPToolRegistry


# ---------------------------------------------------------------------------
# _tokenize / _score
# ---------------------------------------------------------------------------


class TestTokenize:
    def test_lowercases(self) -> None:
        assert _tokenize("CODE Review Adapter") == {"code", "review", "adapter"}

    def test_drops_punctuation(self) -> None:
        # Punctuation between tokens is dropped, but dashes *within* a token
        # are preserved (so "session-buddy" tokenises intact).
        assert _tokenize("code-review, adapters!") == {"code-review", "adapters"}

    def test_drops_empty(self) -> None:
        assert _tokenize("") == set()
        assert _tokenize("   ") == set()

    def test_keeps_dashes_and_underscores(self) -> None:
        assert _tokenize("session-buddy tool_name") == {"session-buddy", "tool_name"}


class TestScore:
    def test_no_query_tokens_returns_zero(self) -> None:
        cap = {"tags": ["foo"], "summary": "bar", "name": "baz", "kind": "tool"}
        assert _score(cap, set()) == 0.0

    def test_perfect_tag_overlap_scores_high(self) -> None:
        cap = {
            "tags": ["code", "review", "adapter"],
            "summary": "irrelevant",
            "name": "irrelevant",
            "kind": "tool",
        }
        score = _score(cap, {"code", "review", "adapter"})
        assert score >= 0.55  # tag-only contribution

    def test_kind_bonus_applied(self) -> None:
        # Adding the kind name to the query must contribute the 0.05 kind bonus.
        cap = {
            "tags": ["foo"],
            "summary": "bar",
            "name": "baz",
            "kind": "adapter",
        }
        # Use the same query tokens except one is the kind. Difference
        # should equal the kind bonus (0.05) exactly.
        score_with_kind = _score(cap, {"foo", "x"})
        score_with_kind_match = _score(cap, {"foo", "adapter"})
        # The diff includes: -0.55/3 + 0.55/3 + 0 - 0 + 0.05 = 0.05
        assert score_with_kind_match - score_with_kind == pytest.approx(0.05)

    def test_score_capped_at_one(self) -> None:
        cap = {
            "tags": ["code", "review", "adapter", "foo"],
            "summary": "code review adapter foo bar",
            "name": "code_review_adapter",
            "kind": "tool",
        }
        score = _score(cap, {"code", "review", "adapter", "foo", "bar", "tool"})
        assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# _filter_catalog
# ---------------------------------------------------------------------------


class TestFilterCatalog:
    def test_no_filters_returns_full_catalog(self) -> None:
        catalog = _filter_catalog(None, None)
        assert len(catalog) > 0
        repos = {c["repo"] for c in catalog}
        assert repos == set(BODAI_COMPONENT_KEYS)

    def test_repo_filter_narrows(self) -> None:
        catalog = _filter_catalog("mahavishnu", None)
        assert len(catalog) > 0
        assert all(c["repo"] == "mahavishnu" for c in catalog)

    def test_kind_filter_narrows(self) -> None:
        catalog = _filter_catalog(None, "error")
        assert len(catalog) > 0
        assert all(c["kind"] == "error" for c in catalog)

    def test_repo_and_kind_filter_combine(self) -> None:
        catalog = _filter_catalog("mahavishnu", "tool")
        assert len(catalog) > 0
        assert all(c["repo"] == "mahavishnu" and c["kind"] == "tool" for c in catalog)


# ---------------------------------------------------------------------------
# Tool registration + invocation
# ---------------------------------------------------------------------------


class _DummyFastMCP:
    """Minimal FastMCP substitute that records tool() decorations."""

    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, *_args: Any, **_kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.registered[fn.__name__] = fn
            return fn

        return decorator


def _make_registry() -> tuple[_DummyFastMCP, FastMCPToolRegistry]:
    app = _DummyFastMCP()
    registry = FastMCPToolRegistry(app)  # type: ignore[arg-type]
    register_cross_repo_tools(registry)
    return app, registry


class TestRegisterCrossRepoTools:
    def test_tool_is_registered(self) -> None:
        app, _registry = _make_registry()
        assert "cross_repo_capability_search" in app.registered

    def test_tool_is_coroutine(self) -> None:
        _app, _registry = _make_registry()
        fn = _app.registered["cross_repo_capability_search"]
        import inspect

        assert inspect.iscoroutinefunction(fn)


class TestCrossRepoCapabilitySearchInvocation:
    @pytest.mark.asyncio
    async def test_basic_shape(self) -> None:
        _app, _registry = _make_registry()
        fn = _app.registered["cross_repo_capability_search"]
        result = await fn("code-review adapters")
        assert isinstance(result, dict)
        assert result["query"] == "code-review adapters"
        assert "total_results" in result
        assert "results" in result
        assert "repos_scanned" in result
        assert result["mode"] == "seed"

    @pytest.mark.asyncio
    async def test_query_returns_code_review_related(self) -> None:
        _app, _registry = _make_registry()
        fn = _app.registered["cross_repo_capability_search"]
        result = await fn("adapter", limit=20, min_score=0.3)
        # We seeded review-related entries; the result must contain at least
        # one capability.
        assert result["total_results"] > 0
        names = [r["name"] for r in result["results"]]
        # Either a mahavishnu adapter or crackerjack tool should be in scope.
        assert any(
            name in {"PrefectAdapter", "LlamaIndexAdapter", "AgnoAdapter"}
            for name in names
        )

    @pytest.mark.asyncio
    async def test_repo_filter_narrows_repos_scanned(self) -> None:
        _app, _registry = _make_registry()
        fn = _app.registered["cross_repo_capability_search"]
        result = await fn(
            "search",
            repo_filter="mahavishnu",
            limit=50,
            min_score=0.1,
        )
        assert result["repos_scanned"] == ["mahavishnu"]
        assert all(r["repo"] == "mahavishnu" for r in result["results"])

    @pytest.mark.asyncio
    async def test_kind_filter_narrows(self) -> None:
        _app, _registry = _make_registry()
        fn = _app.registered["cross_repo_capability_search"]
        result = await fn("schema", kind_filter="error", limit=50, min_score=0.1)
        assert all(r["kind"] == "error" for r in result["results"])

    @pytest.mark.asyncio
    async def test_returns_at_least_3_components_for_crash_recovery(self) -> None:
        """Plan exit-criteria gate: error-handling must span 3+ components."""
        _app, _registry = _make_registry()
        fn = _app.registered["cross_repo_capability_search"]
        # ``error`` token directly matches every ``kind="error"`` capability
        # and several tag entries (PoolUnavailableError, HotStoreUnavailable,
        # etc.). Low threshold to keep the test stable.
        result = await fn("error", limit=20, min_score=0.1, kind_filter="error")
        repos_in_results = {r["repo"] for r in result["results"]}
        assert result["total_results"] > 0
        assert len(repos_in_results) >= 3, (
            f"Expected error-kind results spanning 3+ components; "
            f"got {repos_in_results}"
        )

    @pytest.mark.asyncio
    async def test_min_score_threshold_drops_low_results(self) -> None:
        _app, _registry = _make_registry()
        fn = _app.registered["cross_repo_capability_search"]
        high = await fn("code-review adapters", limit=20, min_score=0.0)
        low = await fn("code-review adapters", limit=20, min_score=0.99)
        assert low["total_results"] <= high["total_results"]

    @pytest.mark.asyncio
    async def test_each_result_has_required_fields(self) -> None:
        _app, _registry = _make_registry()
        fn = _app.registered["cross_repo_capability_search"]
        result = await fn("adapter", limit=20, min_score=0.0)
        for r in result["results"]:
            assert {"repo", "kind", "name", "summary", "doc_hint", "score"} <= set(r)
            assert isinstance(r["score"], float)
            assert 0.0 <= r["score"] <= 1.0

    @pytest.mark.asyncio
    async def test_limit_caps_results(self) -> None:
        _app, _registry = _make_registry()
        fn = _app.registered["cross_repo_capability_search"]
        result = await fn("a", limit=2, min_score=0.0)
        assert result["total_results"] <= 2


# ---------------------------------------------------------------------------
# Validation: input shape
# ---------------------------------------------------------------------------


class TestCrossRepoCapabilitySearchValidation:
    @pytest.mark.asyncio
    async def test_empty_query_rejected(self) -> None:
        _app, _registry = _make_registry()
        fn = _app.registered["cross_repo_capability_search"]
        with pytest.raises(Exception):  # Pydantic ValidationError
            await fn("")

    @pytest.mark.asyncio
    async def test_repo_filter_invalid_chars_rejected(self) -> None:
        _app, _registry = _make_registry()
        fn = _app.registered["cross_repo_capability_search"]
        with pytest.raises(Exception):
            await fn("foo", repo_filter="bad;rm -rf /")

    @pytest.mark.asyncio
    async def test_limit_too_high_rejected(self) -> None:
        _app, _registry = _make_registry()
        fn = _app.registered["cross_repo_capability_search"]
        with pytest.raises(Exception):
            await fn("foo", limit=9999)

    @pytest.mark.asyncio
    async def test_min_score_out_of_range_rejected(self) -> None:
        _app, _registry = _make_registry()
        fn = _app.registered["cross_repo_capability_search"]
        with pytest.raises(Exception):
            await fn("foo", min_score=1.5)


# ---------------------------------------------------------------------------
# Profile wiring smoke test
# ---------------------------------------------------------------------------


def test_profile_includes_cross_repo_group() -> None:
    """The FULL profile must include register_cross_repo_tools."""
    from akosha.mcp.tools.profiles import FULL_REGISTRATIONS

    assert "register_cross_repo_tools" in FULL_REGISTRATIONS


def test_cross_repo_group_registers_tool() -> None:
    """register_cross_repo_group must register cross_repo_capability_search on app."""
    from akosha.mcp.tools.group_registers import register_cross_repo_group

    app = _DummyFastMCP()
    register_cross_repo_group(app)  # type: ignore[arg-type]
    assert "cross_repo_capability_search" in app.registered


def test_registration_map_includes_cross_repo() -> None:
    """REGISTRATION_MAP must route register_cross_repo_tools to the wrapper."""
    from akosha.mcp.tools.profiles import REGISTRATION_MAP

    assert "register_cross_repo_tools" in REGISTRATION_MAP
    assert REGISTRATION_MAP["register_cross_repo_tools"].__name__ == "register_cross_repo_group"
