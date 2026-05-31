"""Tests for akosha/mcp/tools/session_buddy_tools.py — store_memory, batch_store_memories."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools
from akosha.mcp.tools.tool_registry import FastMCPToolRegistry


def _make_registry():
    """Create a real FastMCPToolRegistry with a mock app."""
    app = MagicMock()

    def tool_decorator(*args, **kwargs):
        def deco(func):
            return func

        return deco

    app.tool = tool_decorator
    registry = FastMCPToolRegistry(app)
    return registry


class TestStoreMemory:
    @pytest.mark.asyncio
    async def test_store_success(self):
        registry = _make_registry()
        hot_store = AsyncMock()

        with patch("akosha.models.HotRecord") as mock_record_class:
            register_session_buddy_tools(registry, hot_store)

            store_func = registry.tools["store_memory"].decorated
            result = await store_func(
                memory_id="mem_123",
                text="test content",
                embedding=[0.1] * 384,
                metadata={
                    "source": "http://localhost:8678",
                    "original_id": "orig_1",
                    "type": "session_memory",
                    "correlation_id": "corr-123",
                },
            )

        assert result["status"] == "stored"
        assert result["memory_id"] == "mem_123"
        assert result["embedding_dim"] == 384
        assert result["source"] == "http://localhost:8678"
        hot_store.insert.assert_called_once()
        assert mock_record_class.call_args is not None
        assert mock_record_class.call_args.kwargs["metadata"]["correlation_id"] == "corr-123"

    @pytest.mark.asyncio
    async def test_store_empty_memory_id(self):
        registry = _make_registry()
        hot_store = AsyncMock()
        register_session_buddy_tools(registry, hot_store)

        store_func = registry.tools["store_memory"].decorated
        result = await store_func(memory_id="", text="content")

        assert result["status"] == "failed"
        assert "memory_id" in result["error"]

    @pytest.mark.asyncio
    async def test_store_empty_text(self):
        registry = _make_registry()
        hot_store = AsyncMock()
        register_session_buddy_tools(registry, hot_store)

        store_func = registry.tools["store_memory"].decorated
        result = await store_func(memory_id="mem_1", text="")

        assert result["status"] == "failed"
        assert "text" in result["error"]

    @pytest.mark.asyncio
    async def test_store_wrong_embedding_dim(self):
        registry = _make_registry()
        hot_store = AsyncMock()

        with patch("akosha.models.HotRecord", MagicMock):
            register_session_buddy_tools(registry, hot_store)

        store_func = registry.tools["store_memory"].decorated
        result = await store_func(
            memory_id="mem_1",
            text="content",
            embedding=[0.1] * 128,
        )

        assert result["status"] == "stored"
        assert result["embedding_dim"] == 128

    @pytest.mark.asyncio
    async def test_store_no_embedding_fails_with_real_model(self):
        """Real HotRecord requires embedding, so None causes a Pydantic validation error."""
        registry = _make_registry()
        hot_store = AsyncMock()
        register_session_buddy_tools(registry, hot_store)

        store_func = registry.tools["store_memory"].decorated
        result = await store_func(memory_id="mem_1", text="content")

        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_store_no_metadata(self):
        """No metadata still works because the code constructs metadata internally."""
        registry = _make_registry()
        hot_store = AsyncMock()
        register_session_buddy_tools(registry, hot_store)

        store_func = registry.tools["store_memory"].decorated
        with patch("akosha.models.HotRecord", MagicMock):
            result = await store_func(memory_id="mem_1", text="content", embedding=[0.1] * 384, metadata=None)

        assert result["status"] == "stored"
        assert result["source"] == "unknown"

    @pytest.mark.asyncio
    async def test_store_exception(self):
        registry = _make_registry()
        hot_store = AsyncMock()
        hot_store.insert.side_effect = RuntimeError("db error")
        register_session_buddy_tools(registry, hot_store)

        store_func = registry.tools["store_memory"].decorated
        with patch("akosha.models.HotRecord", MagicMock):
            result = await store_func(memory_id="mem_1", text="content", embedding=[0.1] * 384)

        assert result["status"] == "failed"
        assert "db error" in result["error"]

    @pytest.mark.asyncio
    async def test_store_with_created_at_in_metadata(self):
        registry = _make_registry()
        hot_store = AsyncMock()
        register_session_buddy_tools(registry, hot_store)

        store_func = registry.tools["store_memory"].decorated
        with patch("akosha.models.HotRecord", MagicMock):
            result = await store_func(
                memory_id="mem_1",
                text="content",
                embedding=[0.1] * 384,
                metadata={"source": "http://test", "created_at": "2026-01-01T00:00:00Z"},
            )

        assert result["status"] == "stored"


class TestBatchStoreMemories:
    @pytest.mark.asyncio
    async def test_batch_all_success(self):
        registry = _make_registry()
        hot_store = AsyncMock()

        with patch("akosha.models.HotRecord", MagicMock):
            register_session_buddy_tools(registry, hot_store)

        batch_func = registry.tools["batch_store_memories"].decorated
        result = await batch_func(
            memories=[
                {"memory_id": "mem_1", "text": "First", "embedding": [0.1] * 384},
                {"memory_id": "mem_2", "text": "Second", "embedding": [0.1] * 384},
            ]
        )

        assert result["status"] == "completed"
        assert result["total"] == 2
        assert result["stored"] == 2
        assert result["failed"] == 0

    @pytest.mark.asyncio
    async def test_batch_too_large(self):
        registry = _make_registry()
        hot_store = AsyncMock()
        register_session_buddy_tools(registry, hot_store)

        batch_func = registry.tools["batch_store_memories"].decorated
        result = await batch_func(memories=[{"memory_id": f"m{i}"} for i in range(1001)])

        assert result["status"] == "failed"
        assert result["total"] == 1001
        assert "1000" in result["error"]

    @pytest.mark.asyncio
    async def test_batch_partial_success(self):
        registry = _make_registry()
        hot_store = AsyncMock()

        call_count = 0

        async def fake_insert(record):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first fails")

        hot_store.insert = fake_insert

        with patch("akosha.models.HotRecord", MagicMock):
            register_session_buddy_tools(registry, hot_store)

        batch_func = registry.tools["batch_store_memories"].decorated
        result = await batch_func(
            memories=[
                {"memory_id": "mem_1", "text": "First", "embedding": [0.1] * 384},
                {"memory_id": "mem_2", "text": "Second", "embedding": [0.1] * 384},
            ]
        )

        assert result["status"] == "partial"
        assert result["stored"] == 1
        assert result["failed"] == 1

    @pytest.mark.asyncio
    async def test_batch_missing_required_fields(self):
        registry = _make_registry()
        hot_store = AsyncMock()
        register_session_buddy_tools(registry, hot_store)

        batch_func = registry.tools["batch_store_memories"].decorated
        result = await batch_func(
            memories=[
                {"memory_id": "mem_1"},  # missing text
                {"text": "no id"},  # missing memory_id
                {"memory_id": "mem_3", "text": "ok", "embedding": [0.1] * 384},
            ]
        )

        assert result["status"] == "partial"
        assert result["stored"] == 1
        assert result["failed"] == 2

    @pytest.mark.asyncio
    async def test_batch_all_fail(self):
        registry = _make_registry()
        hot_store = AsyncMock()
        hot_store.insert.side_effect = RuntimeError("always fails")

        with patch("akosha.models.HotRecord", MagicMock):
            register_session_buddy_tools(registry, hot_store)

        batch_func = registry.tools["batch_store_memories"].decorated
        result = await batch_func(
            memories=[
                {"memory_id": "mem_1", "text": "First", "embedding": [0.1] * 384},
            ]
        )

        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_batch_exception(self):
        registry = _make_registry()
        hot_store = AsyncMock()

        with patch("akosha.models.HotRecord", MagicMock):
            register_session_buddy_tools(registry, hot_store)

        batch_func = registry.tools["batch_store_memories"].decorated
        result = await batch_func("not a list")

        assert result["status"] == "failed"


class TestRegisterSessionBuddyTools:
    def test_invalid_registry_raises(self):
        hot_store = MagicMock()
        with pytest.raises(AttributeError):
            register_session_buddy_tools("not a registry", hot_store)

    def test_valid_registry(self):
        registry = _make_registry()
        hot_store = MagicMock()
        register_session_buddy_tools(registry, hot_store)

        tools = registry.tools
        assert "store_memory" in tools
        assert "batch_store_memories" in tools
