"""Integration tests for ``akosha.mcp.tools.ecosystem_skills`` (Phase 4).

Drives the federation aggregator end-to-end with a synthetic FastMCP
substitute: each per-server ``list_skills`` tool is replaced by an in-
process callable that returns a seeded ``SkillMetadata`` list. The
tests do NOT require the real Bodai MCP servers to be running.

Tests assert:

* Tool is registered on a FastMCP app via ``register_ecosystem_skills``.
* Tool name matches the picker-facing convention (``akosha_list_ecosystem_skills``).
* Federation aggregates >=12 entries across the 4 mock servers (3 per
  server — matches plan section 5 Phase 4 exit criteria, post-Dhara).
* Response shape matches ``EcosystemSkillsResponse`` (data, errors,
  per_server_latency_ms, cache, pagination).
* ``include_installed=False`` skips M-6 reconciliation.
* Pagination cursor advances through the dataset.
* Cached responses serve from the file cache when fresh.
"""

from __future__ import annotations

import asyncio
import json
import json as _json
from pathlib import Path
from typing import Any

import pytest

from akosha.mcp.skill_schema import SkillMetadata
from akosha.mcp.tools.ecosystem_skills import (
    FEDERATION_SERVERS,
    register_ecosystem_skills,
)
from akosha.mcp.tools.ecosystem_skills_cache import EcosystemSkillsCache
from akosha.mcp.tools.ecosystem_skills_circuit import EcosystemCircuitRegistry


# ---------------------------------------------------------------------------
# Test fixtures — synthetic per-server MCP clients + FastMCP substitute
# ---------------------------------------------------------------------------


def _seed_metadata(server_key: str, name: str, version: str = "1.0.0") -> dict[str, Any]:
    """Build a SkillMetadata-shaped dict for a synthetic per-server list_skills response."""
    return SkillMetadata(
        id=f"{server_key}:{name}:{version}",
        server=server_key,
        name=name,
        description=(
            f"Use ONLY when the user explicitly types /{server_key}:{name} to "
            f"invoke the {server_key} {name} skill. Do not auto-trigger."
        ),
        version=version,
        tool_refs=[f"mcp__{server_key}__call"],
        dependencies=[],
        content_type="skill",
        content_hash="deadbeef" * 8,
        body_size=128,
        body_format="yaml-frontmatter+markdown",
        allowed_tools=[],
        timestamp=1736000000.0,
    ).model_dump(mode="json")


class _StubHttpResponse:
    """Mimics httpx.Response for the bits ``_fetch_server_skills`` touches."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _StubHttpClient:
    """In-process stub for ``httpx.AsyncClient`` used by the federation tests.

    MagicMock wraps attribute access through auto-generated ``__aenter__``
    stubs that bind ``self`` (a MagicMock quirk). This plain class keeps
    the async-context-manager protocol straightforward.
    """

    def __init__(self, post_callable: Any) -> None:
        self._post = post_callable

    async def __aenter__(self) -> _StubHttpClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        return await self._post(url, json=json, timeout=timeout)


def _build_mock_post(
    per_server_payloads: dict[str, list[dict[str, Any]]],
) -> _StubHttpClient:
    """Build a stub ``httpx.AsyncClient``-shaped ``post`` callable.

    The stub encloses a per-server payload map. ``post(url)`` matches on
    each server's ``base_url``; if there is no matching payload the
    caller surfaces ``RuntimeError`` (so the partial-failure tests can
    exercise the error path).
    """
    per_server_url = {
        srv.base_url: per_server_payloads.get(srv.server_key, [])
        for srv in FEDERATION_SERVERS
    }

    async def post(url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        server_payloads = per_server_url.get(url)
        if server_payloads is None:
            raise RuntimeError(f"unexpected federation URL: {url}")
        return _StubHttpResponse({
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": _json.dumps(server_payloads),
                    }
                ]
            }
        })

    return _StubHttpClient(post)


def _http_client_factory(client: _StubHttpClient) -> Any:
    def factory() -> _StubHttpClient:
        return client

    return factory


class _DummyFastMCP:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, *_args: Any, name: str | None = None, **_kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            key = name if name else fn.__name__
            self.registered[key] = fn
            return fn

        return decorator


# ---------------------------------------------------------------------------
# Default config helper
# ---------------------------------------------------------------------------


def _balanced_payloads() -> dict[str, list[dict[str, Any]]]:
    """Return 3 skills per Bodai server (12 total after Dhara decommissioning).

    The historical count was 15 when Dhara was indexed (5 servers × 3
    skills). After Dhara was decommissioned, the federation fans out to
    4 servers, so the aggregate count drops to 12.
    """
    return {
        server.server_key: [
            _seed_metadata(server.server_key, f"{server.server_key}-skill-{i}")
            for i in range(3)
        ]
        for server in FEDERATION_SERVERS
    }


def _mock_no_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch _load_installed_skill_paths to return [] for the duration of the test."""
    monkeypatch.setattr(
        "akosha.mcp.tools.ecosystem_skills._load_installed_skill_paths",
        lambda: [],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_tool_registered_with_canonical_name(self) -> None:
        app = _DummyFastMCP()
        register_ecosystem_skills(app)  # type: ignore[arg-type]
        assert "akosha_list_ecosystem_skills" in app.registered

    def test_tool_is_async_coroutine(self) -> None:
        app = _DummyFastMCP()
        register_ecosystem_skills(app)  # type: ignore[arg-type]
        fn = app.registered["akosha_list_ecosystem_skills"]
        import inspect

        assert inspect.iscoroutinefunction(fn)


class TestFederationAggregation:
    def test_aggregates_12_skills_across_4_servers(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Plan section 5 Phase 4 exit criteria: >=12 entries (3+ per
        component) after Dhara was decommissioned from the federation.
        """
        _mock_no_installed(monkeypatch)
        asyncio.run(self._aggregate_async(tmp_path))

    async def _aggregate_async(self, tmp_path: Path) -> None:
        payloads = _balanced_payloads()
        mock_client = _build_mock_post(payloads)
        app = _DummyFastMCP()
        cache = EcosystemSkillsCache(path=tmp_path / "cache.json", ttl_seconds=60)
        circuit = EcosystemCircuitRegistry()

        register_ecosystem_skills(  # type: ignore[arg-type]
            app,
            cache=cache,
            circuit=circuit,
            http_client_factory=_http_client_factory(mock_client),
        )
        # Wipe cache from any prior test
        cache.invalidate()
        fn = app.registered["akosha_list_ecosystem_skills"]
        result = await fn()

        assert isinstance(result, dict)
        assert result["schema_version"] == 1
        # First page is the first 20 (limit default), so 12 fits in one page.
        assert len(result["data"]) == 12
        assert result["errors"] == {}
        assert set(result["per_server_latency_ms"].keys()) == {
            srv.server_key for srv in FEDERATION_SERVERS
        }
        # 3 entries from each server
        per_server_counts: dict[str, int] = {}
        for skill in result["data"]:
            per_server_counts[skill["server"]] = per_server_counts.get(skill["server"], 0) + 1
        for srv in FEDERATION_SERVERS:
            assert per_server_counts.get(srv.server_key) == 3, srv.server_key

        # Cache + pagination metadata populated
        assert "cache" in result
        assert result["cache"]["stale"] is False
        assert result["pagination"]["has_more"] is False
        assert result["pagination"]["next_cursor"] is None


class TestPagination:
    def test_pagination_caps_response_to_limit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_no_installed(monkeypatch)
        asyncio.run(self._paginate_async(tmp_path))

    async def _paginate_async(self, tmp_path: Path) -> None:
        # Generate 24 skills across the 4 servers (6 per server).
        # Federation was 5 servers (30 skills) before Dhara was
        # decommissioned; now 4 servers (24 skills).
        payloads = {
            server.server_key: [
                _seed_metadata(server.server_key, f"{server.server_key}-skill-{i}")
                for i in range(6)
            ]
            for server in FEDERATION_SERVERS
        }
        mock_client = _build_mock_post(payloads)
        app = _DummyFastMCP()
        cache = EcosystemSkillsCache(path=tmp_path / "cache.json", ttl_seconds=60)
        circuit = EcosystemCircuitRegistry()

        register_ecosystem_skills(  # type: ignore[arg-type]
            app,
            cache=cache,
            circuit=circuit,
            http_client_factory=_http_client_factory(mock_client),
        )
        cache.invalidate()
        fn = app.registered["akosha_list_ecosystem_skills"]

        page1 = await fn(limit=10, cursor=None)
        assert len(page1["data"]) == 10
        assert page1["pagination"]["has_more"] is True
        assert page1["pagination"]["next_cursor"] is not None

        page2 = await fn(limit=10, cursor=page1["pagination"]["next_cursor"])
        assert len(page2["data"]) == 10
        assert page2["pagination"]["next_cursor"] is not None

        page3 = await fn(limit=10, cursor=page2["pagination"]["next_cursor"])
        # Last page has the remainder (24 - 20 = 4 entries).
        assert len(page3["data"]) == 4
        assert page3["pagination"]["has_more"] is False
        assert page3["pagination"]["next_cursor"] is None

        # Page 1 + page 2 + page 3 cover all 24 unique entries.
        seen: set[tuple[str, str]] = set()
        for page in (page1, page2, page3):
            for skill in page["data"]:
                seen.add((skill["server"], skill["name"]))
        assert len(seen) == 24


class TestCache:
    def test_cached_response_returned_within_ttl(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_no_installed(monkeypatch)
        asyncio.run(self._cache_async(tmp_path))

    async def _cache_async(self, tmp_path: Path) -> None:
        payloads = _balanced_payloads()
        mock_client = _build_mock_post(payloads)
        app = _DummyFastMCP()
        cache = EcosystemSkillsCache(path=tmp_path / "cache.json", ttl_seconds=600)
        circuit = EcosystemCircuitRegistry()

        register_ecosystem_skills(  # type: ignore[arg-type]
            app,
            cache=cache,
            circuit=circuit,
            http_client_factory=_http_client_factory(mock_client),
        )
        cache.invalidate()
        fn = app.registered["akosha_list_ecosystem_skills"]

        first = await fn(limit=20)
        # Subsequent call within TTL should match (cached on file)
        second = await fn(limit=20)
        assert first["data"] == second["data"]


class TestIncludeInstalled:
    def test_include_installed_false_skips_reconciliation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        asyncio.run(self._include_installed_async(tmp_path, monkeypatch))

    async def _include_installed_async(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Server returns no skills; only the installed stub should appear
        # if include_installed=True, and nothing if False.
        empty_payloads: dict[str, list[dict[str, Any]]] = {
            srv.server_key: [] for srv in FEDERATION_SERVERS
        }
        mock_client = _build_mock_post(empty_payloads)
        app = _DummyFastMCP()
        cache = EcosystemSkillsCache(path=tmp_path / "cache.json", ttl_seconds=60)
        circuit = EcosystemCircuitRegistry()
        register_ecosystem_skills(  # type: ignore[arg-type]
            app,
            cache=cache,
            circuit=circuit,
            http_client_factory=_http_client_factory(mock_client),
        )
        cache.invalidate()
        fn = app.registered["akosha_list_ecosystem_skills"]

        # Provide a stub installed path so include_installed=True works.
        fake_skill_dir = tmp_path / "akosha-installed-stub"
        fake_skill_dir.mkdir()
        skill_path = fake_skill_dir / "SKILL.md"
        skill_path.write_text(
            "---\ndescription: installed stub\n---\n# stub\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "akosha.mcp.tools.ecosystem_skills._load_installed_skill_paths",
            lambda: [skill_path],
        )

        with_installed = await fn(include_installed=True)
        assert len(with_installed["data"]) == 1
        assert with_installed["data"][0]["name"] == "installed-stub"

        cache.invalidate()
        without_installed = await fn(include_installed=False)
        assert len(without_installed["data"]) == 0


def test_federation_server_count_is_4() -> None:
    """Federation fans out to 4 Bodai servers (mahavishnu, akosha,
    session-buddy, crackerjack). The historical Dhara entry was
    removed when Dhara was decommissioned on 2026-09-26.
    """
    assert len(FEDERATION_SERVERS) == 4
