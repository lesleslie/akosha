"""Partial-failure integration tests for the Phase 4 federation tool.

Exercises the plan's H-1 partial-failure visibility: when any subset
of the 5 Bodai servers is unreachable / timing out / returning invalid
data, the response ``errors`` dict carries per-server messages while
``data`` carries everything the surviving servers returned.

The tests use a synthetic per-server post callable that throws /
returns-empty for selected servers. They do NOT require the real
Bodai MCP servers to be running.
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


def _seed_metadata(server_key: str, name: str, version: str = "1.0.0") -> dict[str, Any]:
    return SkillMetadata(
        id=f"{server_key}:{name}:{version}",
        server=server_key,
        name=name,
        description=(
            f"Use ONLY when the user explicitly types /{server_key}:{name} "
            f"to invoke the {server_key} {name} skill. Do not auto-trigger."
        ),
        version=version,
        tool_refs=[],
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
    """In-process stub matching ``httpx.AsyncClient`` semantics.

    Plain class — MagicMock auto-wraps ``__aenter__`` and rebinds
    ``self`` which breaks the async-context-manager protocol.
    """

    def __init__(self, post_callable: Any) -> None:
        self._post = post_callable
        self.post_call_count = 0

    async def __aenter__(self) -> _StubHttpClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        self.post_call_count += 1
        return await self._post(url, json=json, timeout=timeout)


def _build_partial_post(
    *,
    healthy_servers: set[str],
    empty_servers: set[str] | None = None,
    raised_servers: set[str] | None = None,
) -> _StubHttpClient:
    """Build a stub post that varies per server.

    * ``healthy_servers`` return 3 seeded skills.
    * ``empty_servers`` return an empty list (technically healthy).
    * ``raised_servers`` raise an exception (call surfaces as error).
    """
    if empty_servers is None:
        empty_servers = set()
    if raised_servers is None:
        raised_servers = set()

    url_to_server = {srv.base_url: srv.server_key for srv in FEDERATION_SERVERS}

    async def post(url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        server_key = url_to_server.get(url)
        if server_key is None:
            raise RuntimeError(f"unexpected URL: {url}")
        if server_key in raised_servers:
            raise RuntimeError(f"simulated transport failure for {server_key}")
        payload = (
            [_seed_metadata(server_key, f"{server_key}-skill-{i}") for i in range(3)]
            if server_key in healthy_servers
            else []
        )
        return _StubHttpResponse(
            {
                "result": {
                    "content": [
                        {"type": "text", "text": _json.dumps(payload)},
                    ]
                }
            }
        )

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


def _mock_no_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch _load_installed_skill_paths to return [] for the duration of the test."""
    monkeypatch.setattr(
        "akosha.mcp.tools.ecosystem_skills._load_installed_skill_paths",
        lambda: [],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPartialFailure:
    def test_killing_one_server_populates_errors_dict(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A single server (mahavishnu) is down; errors['mahavishnu'] is populated."""
        _mock_no_installed(monkeypatch)
        asyncio.run(self._kill_one_async(tmp_path))

    async def _kill_one_async(self, tmp_path: Path) -> None:
        healthy = {srv.server_key for srv in FEDERATION_SERVERS} - {"mahavishnu"}
        mock_client = _build_partial_post(
            healthy_servers=healthy, raised_servers={"mahavishnu"}
        )
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
        result = await fn()

        # Healthy 4 servers each return 3 skills -> 12 entries.
        assert len(result["data"]) == 12
        # Mahavishnu surfaces in errors.
        assert "mahavishnu" in result["errors"]
        assert "mahavishnu" in result["per_server_latency_ms"]

    def test_partial_failure_does_not_drop_other_servers(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """H-1: asyncio.gather(return_exceptions=True) keeps healthy servers contributing."""
        _mock_no_installed(monkeypatch)
        asyncio.run(self._partial_doesnt_drop_async(tmp_path))

    async def _partial_doesnt_drop_async(self, tmp_path: Path) -> None:
        healthy = {"akosha", "mahavishnu"}
        down = {"session-buddy", "dhara", "crackerjack"}
        mock_client = _build_partial_post(healthy_servers=healthy, raised_servers=down)
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

        result = await fn()
        assert len(result["data"]) == 6  # 2 servers * 3 skills
        for down_key in down:
            assert down_key in result["errors"]
        # All 5 servers still have a latency entry.
        assert set(result["per_server_latency_ms"].keys()) == {
            srv.server_key for srv in FEDERATION_SERVERS
        }

    def test_all_servers_down_returns_empty_data_with_errors(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _mock_no_installed(monkeypatch)
        asyncio.run(self._all_down_async(tmp_path))

    async def _all_down_async(self, tmp_path: Path) -> None:
        mock_client = _build_partial_post(
            healthy_servers=set(),
            raised_servers={srv.server_key for srv in FEDERATION_SERVERS},
        )
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

        result = await fn()
        assert result["data"] == []
        assert set(result["errors"].keys()) == {
            srv.server_key for srv in FEDERATION_SERVERS
        }

    def test_circuit_breaker_short_circuits_after_threshold(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """H-1: 3 failures per server trips the breaker; subsequent calls do not re-issue transport.

        Threshold is 3 failures per server; we make 3 calls so every
        server trips, then verify the 4th call issues no transport
        attempts because every breaker is in its cooldown window.
        """
        _mock_no_installed(monkeypatch)
        asyncio.run(self._circuit_short_circuit_async(tmp_path))

    async def _circuit_short_circuit_async(self, tmp_path: Path) -> None:
        # All servers fail.
        mock_client = _build_partial_post(
            healthy_servers=set(),
            raised_servers={srv.server_key for srv in FEDERATION_SERVERS},
        )
        app = _DummyFastMCP()
        cache = EcosystemSkillsCache(path=tmp_path / "cache.json", ttl_seconds=60)
        circuit = EcosystemCircuitRegistry()
        register_ecosystem_skills(  # type: ignore[arg-type]
            app,
            cache=cache,
            circuit=circuit,
            http_client_factory=_http_client_factory(mock_client),
        )
        fn = app.registered["akosha_list_ecosystem_skills"]

        # Three calls: each makes 5 transport attempts; after the third
        # call every server's breaker is in cooldown (3 failures in
        # window, threshold tripped).
        for _ in range(3):
            cache.invalidate()
            await fn(limit=20)
        assert mock_client.post_call_count == 3 * len(FEDERATION_SERVERS)

        # Fourth call: all breakers are open -> 0 transport attempts.
        cache.invalidate()
        await fn(limit=20)
        assert mock_client.post_call_count == 3 * len(FEDERATION_SERVERS)


def test_partial_failure_does_not_skip_cache_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even when some servers are down, the partial-response is cached for the next caller."""
    _mock_no_installed(monkeypatch)
    asyncio.run(_cache_partial_failure_async(tmp_path))


async def _cache_partial_failure_async(tmp_path: Path) -> None:
    healthy = {"akosha", "mahavishnu"}
    down = {"session-buddy", "dhara", "crackerjack"}
    mock_client = _build_partial_post(healthy_servers=healthy, raised_servers=down)
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
    assert set(first["errors"].keys()) == down

    second = await fn(limit=20)
    # Second call should hit the cache and yield an identical body.
    assert second["data"] == first["data"]
    assert set(second["errors"].keys()) == down
