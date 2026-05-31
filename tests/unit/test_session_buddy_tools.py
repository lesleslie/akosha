"""Tests for Session-Buddy integration tools.

Tests HTTP-based memory ingestion from Session-Buddy instances via MCP tools.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# store_memory and batch_store_memories are nested functions inside
# register_session_buddy_tools(), so we access them via the registry.
from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools


def _get_registered_tools(registry):
    """Extract store_memory and batch_store_memories from the registry."""
    store_fn = None
    batch_fn = None
    for name, func in registry._registered_tools.items():
        if name == "store_memory":
            store_fn = func
        elif name == "batch_store_memories":
            batch_fn = func
    return store_fn, batch_fn


class TestStoreMemory:
    """Test individual memory storage functionality."""

    @pytest.fixture
    def registry_and_store(self):
        """Create a mock registry and register tools, returning (registry, store_memory_fn)."""
        mock_registry = MagicMock()
        mock_hot_store = AsyncMock()

        # Track registered tools
        registered = {}

        def mock_register(metadata):
            """Capture registered tools."""

            def decorator(func):
                registered[metadata.name] = func
                return func

            return decorator

        mock_registry.register = mock_register
        register_session_buddy_tools(mock_registry, mock_hot_store)

        store_memory_fn = registered.get("store_memory")
        return mock_registry, mock_hot_store, store_memory_fn

    @pytest.mark.asyncio
    async def test_store_memory_basic(self, registry_and_store):
        """Test basic memory storage."""
        _, mock_hot_store, store_memory = registry_and_store
        mock_hot_store.insert = AsyncMock()

        # Mock HotRecord
        with patch("akosha.models.HotRecord") as mock_record_class:
            mock_record = MagicMock()
            mock_record_class.return_value = mock_record

            memory_id = "test-mem-with-embedding"
            text = "Memory with embedding"
            metadata = {
                "source": "http://localhost:8678",
                "correlation_id": "corr-123",
            }

            result = await store_memory(memory_id=memory_id, text=text, embedding=[0.1] * 384, metadata=metadata)

            # Should succeed
            assert result["status"] == "stored"
            assert result["memory_id"] == memory_id
            assert result["embedding_dim"] == 384
            assert result["source"] == "http://localhost:8678"
            assert mock_record_class.call_args is not None
            assert mock_record_class.call_args.kwargs["metadata"]["correlation_id"] == "corr-123"

    @pytest.mark.asyncio
    async def test_store_memory_invalid_id(self, registry_and_store):
        """Test memory storage with invalid memory_id."""
        _, _, store_memory = registry_and_store

        result = await store_memory(memory_id="", text="test content")

        # Should fail
        assert result["status"] == "failed"
        assert result["memory_id"] == ""
        assert "Invalid memory_id" in result["error"]

    @pytest.mark.asyncio
    async def test_store_memory_no_text(self, registry_and_store):
        """Test memory storage without text content."""
        _, _, store_memory = registry_and_store

        result = await store_memory(memory_id="test-id", text="")

        # Should fail
        assert result["status"] == "failed"
        assert result["memory_id"] == "test-id"
        assert "Invalid text" in result["error"]

    @pytest.mark.asyncio
    async def test_store_memory_wrong_embedding_dim(self, registry_and_store):
        """Test memory storage with wrong embedding dimension."""
        _, mock_hot_store, store_memory = registry_and_store
        mock_hot_store.insert = AsyncMock()

        with patch("akosha.models.HotRecord") as mock_record_class:
            mock_record = MagicMock()
            mock_record_class.return_value = mock_record

            memory_id = "test-wrong-dim"
            text = "Test content"
            embedding = [0.1] * 256  # Wrong dimension (should be 384)

            result = await store_memory(memory_id=memory_id, text=text, embedding=embedding)

            # Should succeed but warn
            assert result["status"] == "stored"
            assert result["memory_id"] == memory_id
            assert result["embedding_dim"] == 256  # Actual dimension stored

    @pytest.mark.asyncio
    async def test_store_memory_with_metadata(self, registry_and_store):
        """Test memory storage with full metadata."""
        _, mock_hot_store, store_memory = registry_and_store
        mock_hot_store.insert = AsyncMock()

        with patch("akosha.models.HotRecord") as mock_record_class:
            mock_record = MagicMock()
            mock_record_class.return_value = mock_record

            memory_id = "test-full-metadata"
            text = "Memory with metadata"
            embedding = [0.1] * 384
            metadata = {
                "source": "http://localhost:8678",
                "original_id": "original-123",
                "created_at": "2026-02-08T12:00:00Z",
                "type": "insight",
            }

            result = await store_memory(
                memory_id=memory_id, text=text, embedding=embedding, metadata=metadata
            )

            # Should succeed
            assert result["status"] == "stored"
            assert result["memory_id"] == memory_id

            # Verify metadata handling
            mock_record_class.assert_called_once()

    @pytest.mark.asyncio
    async def test_store_memory_insert_error(self, registry_and_store):
        """Test memory storage when insert fails."""
        _, mock_hot_store, store_memory = registry_and_store
        mock_hot_store.insert = AsyncMock(side_effect=Exception("Database connection failed"))

        memory_id = "test-error"
        text = "Test content"
        embedding = [0.1] * 384

        with patch("akosha.models.HotRecord") as mock_record_class:
            mock_record = MagicMock()
            mock_record_class.return_value = mock_record

            result = await store_memory(memory_id=memory_id, text=text, embedding=embedding)

            # Should fail
            assert result["status"] == "failed"
            assert result["memory_id"] == memory_id
            assert "Database connection failed" in result["error"]


class TestBatchStoreMemories:
    """Test batch memory storage functionality."""

    @pytest.fixture
    def registry_and_batch(self):
        """Create a mock registry and register tools, returning (registry, batch_store_fn)."""
        mock_registry = MagicMock()
        mock_hot_store = AsyncMock()

        registered = {}

        def mock_register(metadata):
            def decorator(func):
                registered[metadata.name] = func
                return func

            return decorator

        mock_registry.register = mock_register
        register_session_buddy_tools(mock_registry, mock_hot_store)

        batch_fn = registered.get("batch_store_memories")
        return mock_registry, mock_hot_store, batch_fn

    @pytest.mark.asyncio
    async def test_batch_store_memories_basic(self, registry_and_batch):
        """Test basic batch memory storage."""
        _, mock_hot_store, batch_store_memories = registry_and_batch
        mock_hot_store.insert = AsyncMock()

        memories = [
            {
                "memory_id": "mem1",
                "text": "First memory",
                "embedding": [0.1] * 384,
                "metadata": {"source": "http://localhost:8678"},
            },
            {
                "memory_id": "mem2",
                "text": "Second memory",
                "embedding": [0.2] * 384,
            },
        ]

        with patch("akosha.models.HotRecord") as mock_record_class:
            mock_record = MagicMock()
            mock_record_class.return_value = mock_record

            result = await batch_store_memories(memories)

            # Should succeed completely
            assert result["status"] == "completed"
            assert result["total"] == 2
            assert result["stored"] == 2
            assert result["failed"] == 0
            assert result["errors"] == []
            assert "stored_at" in result

    @pytest.mark.asyncio
    async def test_batch_store_memories_partial_success(self, registry_and_batch):
        """Test batch memory storage with partial success."""
        _, mock_hot_store, batch_store_memories = registry_and_batch
        mock_hot_store.insert = AsyncMock()

        memories = [
            {
                "memory_id": "mem1",
                "text": "Valid memory",
                "embedding": [0.1] * 384,
                "metadata": {"source": "http://localhost:8678"},
            },
            {"memory_id": "", "text": "Invalid memory (no ID)"},
            {"memory_id": "mem2", "text": "", "metadata": {"source": "http://localhost:8678"}},
        ]

        with patch("akosha.models.HotRecord") as mock_record_class:
            mock_record = MagicMock()
            mock_record_class.return_value = mock_record

            result = await batch_store_memories(memories)

            # Should be partial success
            assert result["status"] == "partial"
            assert result["total"] == 3
            assert result["stored"] == 1
            assert result["failed"] == 2
            assert len(result["errors"]) == 2

    @pytest.mark.asyncio
    async def test_batch_store_memories_batch_too_large(self, registry_and_batch):
        """Test batch memory storage with oversized batch."""
        _, _, batch_store_memories = registry_and_batch

        # Create batch of 1001 memories (exceeds limit)
        memories = [{"memory_id": f"mem{i}", "text": f"Memory {i}"} for i in range(1001)]

        result = await batch_store_memories(memories)

        # Should fail
        assert result["status"] == "failed"
        assert result["total"] == 1001
        assert result["stored"] == 0
        assert result["failed"] == 1001
        assert "exceeds maximum" in result["error"]

    @pytest.mark.asyncio
    async def test_batch_store_memories_empty_batch(self, registry_and_batch):
        """Test batch memory storage with empty batch."""
        _, _, batch_store_memories = registry_and_batch

        memories = []

        result = await batch_store_memories(memories)

        # Should succeed with zero items
        assert result["status"] == "completed"
        assert result["total"] == 0
        assert result["stored"] == 0
        assert result["failed"] == 0
        assert result["errors"] == []

    @pytest.mark.asyncio
    async def test_batch_store_memories_all_fail(self, registry_and_batch):
        """Test batch memory storage where all items fail."""
        _, _, batch_store_memories = registry_and_batch

        memories = [{"memory_id": "", "text": "No ID"}, {"memory_id": "mem2", "text": ""}]

        result = await batch_store_memories(memories)

        # Should fail completely
        assert result["status"] == "failed"
        assert result["total"] == 2
        assert result["stored"] == 0
        assert result["failed"] == 2
        assert len(result["errors"]) == 2


class TestSessionBuddyToolsIntegration:
    """Integration tests for Session-Buddy tools."""

    @pytest.mark.asyncio
    async def test_tools_register_correctly(self):
        """Test that tools can be registered with registry."""
        mock_registry = MagicMock()
        mock_hot_store = AsyncMock()

        # Should not raise exceptions
        register_session_buddy_tools(mock_registry, mock_hot_store)

        # Verify registration was called
        assert mock_registry.register.call_count == 2

    @pytest.mark.asyncio
    async def test_memory_flow_integration(self):
        """Test complete memory flow from input to storage."""
        mock_registry = MagicMock()
        mock_hot_store = AsyncMock()
        mock_hot_store.insert = AsyncMock()

        registered = {}

        def mock_register(metadata):
            def decorator(func):
                registered[metadata.name] = func
                return func

            return decorator

        mock_registry.register = mock_register
        register_session_buddy_tools(mock_registry, mock_hot_store)

        store_memory = registered["store_memory"]

        with patch("akosha.models.HotRecord") as mock_record_class:
            mock_record = MagicMock()
            mock_record_class.return_value = mock_record

            # Simulate receiving memory from Session-Buddy
            memory_data = {
                "memory_id": "flow-test-123",
                "text": "This is a test of the complete memory flow",
                "embedding": [0.5] * 384,
                "metadata": {
                    "source": "https://session-buddy.example.com",
                    "original_id": "sess-456",
                    "created_at": "2026-02-08T12:00:00Z",
                    "type": "code_insight",
                    "tags": ["python", "asyncio"],
                },
            }

            result = await store_memory(**memory_data)

            # Verify complete flow
            assert result["status"] == "stored"
            assert result["memory_id"] == "flow-test-123"
            assert result["embedding_dim"] == 384
            assert result["source"] == "https://session-buddy.example.com"

            # Verify HotRecord was created
            mock_record_class.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
