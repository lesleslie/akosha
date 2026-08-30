"""Tests for the W0 per-group register_* wrappers.

Regression suite for the embedding-service / hot-store wiring bug surfaced
on 2026-08-22: ``register_akosha_group`` was hardcoding ``embedding_service=None``
so the lifespan's real services never reached the tools. Likewise
``_try_create_hot_store`` was returning an uninitialized store. And the
``code_graphs`` table was never created during ``HotStore.initialize``.

These tests pin the expected behaviour so the wiring cannot silently regress
again.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from akosha.mcp.tools.group_registers import (
    _try_create_hot_store,
    register_akosha_group,
)


class _DummyFastMCP:
    """Minimal FastMCP stand-in that records ``tool()`` registrations."""

    def __init__(self) -> None:
        self.registered: dict[str, Callable[..., Any]] = {}

    def tool(
        self,
        *_args: Any,
        **_kwargs: Any,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.registered[fn.__name__] = fn
            return fn

        return decorator


def _make_embedding_service_stub() -> MagicMock:
    """Build an EmbeddingService-shaped mock that the registry will accept."""
    service = MagicMock()
    service.generate_embedding = AsyncMock(return_value=[0.0] * 384)
    service.generate_batch_embeddings = AsyncMock(return_value=[[0.0] * 384])
    service.is_available = MagicMock(return_value=False)
    return service


@pytest.mark.asyncio
async def test_register_akosha_group_wires_embedding_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """register_akosha_group must reach register_akosha_tools with a real
    EmbeddingService — not ``None`` — so that embedding, search, and graph
    tools get registered instead of being silently dropped."""
    app = _DummyFastMCP()
    embedding_stub = _make_embedding_service_stub()
    analytics_stub = MagicMock()
    graph_stub = MagicMock()

    monkeypatch.setattr(
        "akosha.processing.embeddings.get_embedding_service",
        lambda: embedding_stub,
    )
    monkeypatch.setattr(
        "akosha.processing.analytics.TimeSeriesAnalytics",
        lambda: analytics_stub,
    )
    monkeypatch.setattr(
        "akosha.processing.knowledge_graph.KnowledgeGraphBuilder",
        lambda: graph_stub,
    )

    await register_akosha_group(app)

    # The hardcoded ``None`` regression made every tool group skip registration.
    # After the fix, embedding tools must appear in the registry.
    assert "generate_embedding" in app.registered, (
        "register_akosha_group hardcoded embedding_service=None; "
        "embedding tools were silently skipped at startup."
    )


@pytest.mark.asyncio
async def test_register_akosha_group_wires_analytics_and_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """register_akosha_group must also pass analytics_service and graph_builder
    so that analytics and knowledge-graph tools register too."""
    app = _DummyFastMCP()
    embedding_stub = _make_embedding_service_stub()
    analytics_stub = MagicMock()
    graph_stub = MagicMock()

    monkeypatch.setattr(
        "akosha.processing.embeddings.get_embedding_service",
        lambda: embedding_stub,
    )
    monkeypatch.setattr(
        "akosha.processing.analytics.TimeSeriesAnalytics",
        lambda: analytics_stub,
    )
    monkeypatch.setattr(
        "akosha.processing.knowledge_graph.KnowledgeGraphBuilder",
        lambda: graph_stub,
    )

    await register_akosha_group(app)

    # detect_anomalies comes from register_analytics_tools; query_knowledge_graph
    # comes from register_graph_tools. Both should be present when services wire.
    assert "detect_anomalies" in app.registered, (
        "analytics tools skipped — analytics_service never reached register_akosha_tools"
    )
    assert "query_knowledge_graph" in app.registered, (
        "graph tools skipped — graph_builder never reached register_akosha_tools"
    )


@pytest.mark.asyncio
async def test_try_create_hot_store_returns_initialized_store() -> None:
    """_try_create_hot_store must call ``.initialize()`` on the returned store.

    The pre-fix version returned a freshly-constructed ``HotStore`` whose
    ``conn`` attribute was still ``None`` — causing every dependent tool
    (``search_code_patterns``, ``find_function_usage``, etc.) to raise
    ``RuntimeError("Hot store not initialized")`` at runtime."""
    with patch("akosha.storage.create_hot_store") as create_mock:
        store_instance = MagicMock()
        store_instance.initialize = AsyncMock()
        create_mock.return_value = store_instance

        result = await _try_create_hot_store()

    assert result is store_instance
    store_instance.initialize.assert_awaited_once()


@pytest.mark.asyncio
async def test_hot_store_initialize_creates_code_graphs_table() -> None:
    """``HotStore.initialize()`` must also create the ``code_graphs`` table.

    ``initialize_code_graphs_table`` exists at ``hot_store.py:325`` but was
    never called anywhere in the codebase — so ``search_code_patterns``
    failed with ``table not found`` even when the store itself was
    initialized. Calling ``list_code_graphs`` post-initialize must succeed."""
    from akosha.storage.hot_store import HotStore

    store = HotStore(database_path=":memory:")
    await store.initialize()

    # Should not raise — previously raised RuntimeError or duckdb "table not found".
    result = await store.list_code_graphs(limit=10)
    assert isinstance(result, list)
