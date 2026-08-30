"""Tests for Sub-plan C wiring of ``search_all_systems`` to ``HotStore.search_similar``.

Replaces the hard-coded mock in ``akosha/mcp/tools/akosha_tools.py:search_all_systems``
(``content = f"Mock result for: {query}"``) with a real call to ``HotStore.search_similar``
on the websocket invocations corpus (``system_id="mahavishnu"``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from akosha.mcp.tools.akosha_tools import (
    register_akosha_tools,
    register_search_tools,
)


class Vector(list):
    """List subclass that mimics numpy.ndarray by exposing ``.tolist()``."""

    def tolist(self) -> list[float]:
        return list(self)


@dataclass
class CapturingRegistry:
    """Minimal registry that captures registered tools keyed by name."""

    tools: dict[str, object]

    def __init__(self) -> None:
        self.tools = {}

    def register(self, metadata):
        def decorator(func):
            self.tools[metadata.name] = func
            return func

        return decorator


@pytest.fixture(autouse=True)
def _disable_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable ``@require_auth`` so tests can call the tool directly."""
    monkeypatch.delenv("AKOSHA_API_TOKEN", raising=False)
    monkeypatch.setenv("AKOSHA_AUTH_ENABLED", "false")


def _make_embedding_service(query_vec: list[float]) -> MagicMock:
    """Return a mock embedding service that yields ``query_vec`` on demand."""
    service = MagicMock()
    service.generate_embedding = AsyncMock(return_value=Vector(query_vec))
    service.is_available = MagicMock(return_value=True)
    return service


def _make_hot_store(rows: list[dict] | None) -> MagicMock:
    """Return a mock HotStore whose ``search_similar`` returns ``rows``.

    When ``rows`` is ``None``, the mock is configured to raise — used to
    simulate a hot-store lookup failure.
    """
    store = MagicMock()
    if rows is None:
        store.search_similar = AsyncMock(side_effect=RuntimeError("hot_store down"))
    else:
        store.search_similar = AsyncMock(return_value=rows)
    return store


def _make_row(
    conversation_id: str,
    content: str,
    similarity: float = 0.9,
    system_id: str = "mahavishnu",
    timestamp: datetime | None = None,
) -> dict:
    """Return a HotStore.search_similar-shaped row for fixture construction."""
    return {
        "system_id": system_id,
        "conversation_id": conversation_id,
        "content": content,
        "timestamp": timestamp or datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC),
        "metadata": {"tool": "websocket_get_status", "result": "success"},
        "similarity": similarity,
    }


@pytest.mark.asyncio
async def test_search_all_systems_returns_real_results_when_hot_store_populated() -> None:
    """When hot_store returns rows, search_all_systems surfaces them in order.

    Asserts:
    - ``total_results`` equals the number of hot_store rows
    - ``mode`` is ``"real"``
    - The operator's user-supplied ``system_id`` is IGNORED — the corpus is
      always the websocket invocations table (``system_id="mahavishnu"``)
    - Each result row is mapped 1:1 from the hot_store shape
    """
    rows = [
        _make_row("conv-1", "websocket_get_status ok", similarity=0.95),
        _make_row("conv-2", "websocket_get_status error timeout", similarity=0.88),
        _make_row("conv-3", "websocket_get_status success", similarity=0.82),
    ]
    embedding_service = _make_embedding_service([0.1] * 384)
    hot_store = _make_hot_store(rows)

    registry = CapturingRegistry()
    register_search_tools(registry, embedding_service, hot_store=hot_store)
    search_all_systems = registry.tools["search_all_systems"]

    result = await search_all_systems(
        query="websocket_get_status failures 2026-08-29",
        limit=10,
        threshold=0.7,
        system_id="some-other-system",  # MUST be ignored per spec
    )

    assert result["total_results"] == 3
    assert result["mode"] == "real"
    assert [r["conversation_id"] for r in result["results"]] == [
        "conv-1",
        "conv-2",
        "conv-3",
    ]
    assert all(r["system_id"] == "mahavishnu" for r in result["results"])

    # User-supplied system_id must NOT reach the hot store — the spec mandates
    # a fixed corpus filter so operator queries always hit the websocket
    # invocations table.
    hot_store.search_similar.assert_awaited_once()
    kwargs = hot_store.search_similar.await_args.kwargs
    assert kwargs["system_id"] == "mahavishnu"
    assert "some-other-system" not in str(kwargs)


@pytest.mark.asyncio
async def test_search_all_systems_falls_back_to_informational_when_hot_store_empty() -> None:
    """When hot_store returns no rows, fall back to a single informational row.

    Asserts:
    - ``total_results == 1``
    - ``mode == "fallback"``
    - The fallback ``content`` begins with
      ``"No websocket invocations indexed yet"``
    - No hard-coded ``Mock result for:`` string is returned
    """
    embedding_service = _make_embedding_service([0.1] * 384)
    hot_store = _make_hot_store([])  # empty result set

    registry = CapturingRegistry()
    register_search_tools(registry, embedding_service, hot_store=hot_store)
    search_all_systems = registry.tools["search_all_systems"]

    result = await search_all_systems(
        query="anything at all",
        limit=10,
        threshold=0.7,
    )

    assert result["total_results"] == 1
    assert result["mode"] == "fallback"
    assert result["results"][0]["content"].startswith(
        "No websocket invocations indexed yet"
    )
    # Defensive: ensure the legacy mock string is fully gone.
    combined = str(result["results"])
    assert "Mock result for:" not in combined


@pytest.mark.asyncio
async def test_search_all_systems_falls_back_when_hot_store_is_none() -> None:
    """When hot_store is ``None`` (lite mode / CI), fall back to informational.

    This proves the function tolerates the optional ``hot_store`` parameter
    being absent — callers that don't thread one through still get a
    well-formed response instead of a crash.
    """
    embedding_service = _make_embedding_service([0.1] * 384)

    registry = CapturingRegistry()
    register_search_tools(registry, embedding_service, hot_store=None)
    search_all_systems = registry.tools["search_all_systems"]

    result = await search_all_systems(query="hello", limit=10, threshold=0.7)

    assert result["mode"] == "fallback"
    assert result["total_results"] == 1
    assert result["results"][0]["content"].startswith(
        "No websocket invocations indexed yet"
    )


@pytest.mark.asyncio
async def test_register_akosha_tools_threads_hot_store_through() -> None:
    """``register_akosha_tools`` must accept and forward ``hot_store=`` kwarg.

    The high-level registrar must propagate ``hot_store`` to
    ``register_search_tools`` so lifespan wiring threads the handle
    end-to-end without a separate manual call.
    """
    embedding_service = _make_embedding_service([0.1] * 384)
    hot_store = _make_hot_store([_make_row("conv-x", "ok", similarity=0.9)])

    registry = CapturingRegistry()
    register_akosha_tools(registry, embedding_service, hot_store=hot_store)
    search_all_systems = registry.tools["search_all_systems"]

    result = await search_all_systems(query="hello", limit=10, threshold=0.7)

    assert result["total_results"] == 1
    assert result["mode"] == "real"
    hot_store.search_similar.assert_awaited_once()
