"""Tests for Session-Buddy integration tools.

Tests HTTP-based memory ingestion from Session-Buddy instances via MCP tools.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import UTC, datetime

# Import the tools directly
from akosha.mcp.tools.session_buddy_tools import (
    store_memory,
    batch_store_memories,
)


class TestStoreMemory:
    """Test individual memory storage functionality."""

    @pytest.mark.asyncio
    async def test_store_memory_basic(self):
        """Test basic memory storage."""
        mock_hot_store = AsyncMock()
        mock_hot_store.insert = AsyncMock()

        # Mock HotRecord
        with patch('akosha.mcp.tools.session_buddy_tools.HotRecord') as mock_record_class:
            mock_record = MagicMock()
            mock_record_class.return_value = mock_record

            memory_id = "test-mem-123"
            text = "This is a test memory about JWT authentication"
            embedding = [0.1] * 384
            metadata = {"source": "http://localhost:8678", "type": "session_memory"}

            result = await store_memory(
                memory_id=memory_id,
                text=text,
                embedding=embedding,
                metadata=metadata
            )

            # Should succeed
            assert result["status"] == "stored"
            assert result["memory_id"] == memory_id
            assert result["embedding_dim"] == 384
            assert result["source"] == "http://localhost:8678"
            assert "stored_at" in result

            # Verify HotRecord creation
            mock_record_class.assert_called_once()
            expected_timestamp = metadata["created_at"]
            mock_record_class.assert_called_with(
                system_id="http://localhost:8678",
                conversation_id="test-mem-123",
                content="This is a test memory about JWT authentication",
                embedding=[0.1] * 384,
                timestamp=expected_timestamp,
                metadata={
                    "source": "http://localhost:8678",
                    "original_id": None,
                    "type": "session_memory",
                    "ingestion_method": "http_push",
                },
            )

            # Verify insertion
            mock_hot_store.insert.assert_called_once_with(mock_record)

    @pytest.mark.asyncio
    async def test_store_memory_no_embedding(self):
        """Test memory storage without embedding."""
        mock_hot_store = AsyncMock()
        mock_hot_store.insert = AsyncMock()

        with patch('akosha.mcp.tools.session_buddy_tools.HotRecord') as mock_record_class:
            mock_record = MagicMock()
            mock_record_class.return_value = mock_record

            memory_id = "test-mem-no-embedding"
            text = "Memory without embedding"
            metadata = {"source": "http://localhost:8678"}

            result = await store_memory(
                memory_id=memory_id,
                text=text,
                metadata=metadata
            )

            # Should succeed
            assert result["status"] == "stored"
            assert result["memory_id"] == memory_id
            assert result["embedding_dim"] is None
            assert result["source"] == "http://localhost:8678"

            # Verify HotRecord creation without embedding
            mock_record_class.assert_called_once()
            mock_record_class.assert_called_with(
                system_id="http://localhost:8678",
                conversation_id="test-mem-no-embedding",
                content="Memory without embedding",
                embedding=None,
                timestamp=pytest.approx(datetime.now(UTC).isoformat()),
                metadata={
                    "source": "http://localhost:8678",
                    "original_id": None,
                    "type": "session_memory",
                    "ingestion_method": "http_push",
                },
            )

    @pytest.mark.asyncio
    async def test_store_memory_invalid_id(self):
        """Test memory storage with invalid memory_id."""
        result = await store_memory(
            memory_id="",
            text="test content"
        )

        # Should fail
        assert result["status"] == "failed"
        assert result["memory_id"] == ""
        assert "Invalid memory_id" in result["error"]

    @pytest.mark.asyncio
    async def test_store_memory_no_text(self):
        """Test memory storage without text content."""
        result = await store_memory(
            memory_id="test-id",
            text=""
        )

        # Should fail
        assert result["status"] == "failed"
        assert result["memory_id"] == "test-id"
        assert "Invalid text" in result["error"]

    @pytest.mark.asyncio
    async def test_store_memory_wrong_embedding_dim(self):
        """Test memory storage with wrong embedding dimension."""
        mock_hot_store = AsyncMock()
        mock_hot_store.insert = AsyncMock()

        with patch('akosha.mcp.tools.session_buddy_tools.HotRecord') as mock_record_class:
            mock_record = MagicMock()
            mock_record_class.return_value = mock_record

            memory_id = "test-wrong-dim"
            text = "Test content"
            embedding = [0.1] * 256  # Wrong dimension (should be 384)

            result = await store_memory(
                memory_id=memory_id,
                text=text,
                embedding=embedding
            )

            # Should succeed but warn
            assert result["status"] == "stored"
            assert result["memory_id"] == memory_id
            assert result["embedding_dim"] == 256  # Actual dimension stored

    @pytest.mark.asyncio
    async def test_store_memory_with_metadata(self):
        """Test memory storage with full metadata."""
        mock_hot_store = AsyncMock()
        mock_hot_store.insert = AsyncMock()

        with patch('akosha.mcp.tools.session_buddy_tools.HotRecord') as mock_record_class:
            mock_record = MagicMock()
            mock_record_class.return_value = mock_record

            memory_id = "test-full-metadata"
            text = "Memory with metadata"
            embedding = [0.1] * 384
            metadata = {
                "source": "http://localhost:8678",
                "original_id": "original-123",
                "created_at": "2026-02-08T12:00:00Z",
                "type": "insight"
            }

            result = await store_memory(
                memory_id=memory_id,
                text=text,
                embedding=embedding,
                metadata=metadata
            )

            # Should succeed
            assert result["status"] == "stored"
            assert result["memory_id"] == memory_id

            # Verify metadata handling
            mock_record_class.assert_called_once()
            call_args = mock_record_class.call_args
            assert call_args.kwargs["original_id"] == "original-123"
            assert call_args.kwargs["type"] == "insight"
            assert call_args.kwargs["timestamp"] == "2026-02-08T12:00:00Z"

    @pytest.mark.asyncio
    async def test_store_memory_insert_error(self):
        """Test memory storage when insert fails."""
        mock_hot_store = AsyncMock()
        mock_hot_store.insert = AsyncMock(side_effect=Exception("Database connection failed"))

        memory_id = "test-error"
        text = "Test content"

        result = await store_memory(
            memory_id=memory_id,
            text=text
        )

        # Should fail
        assert result["status"] == "failed"
        assert result["memory_id"] == memory_id
        assert "Database connection failed" in result["error"]


class TestBatchStoreMemories:
    """Test batch memory storage functionality."""

    @pytest.mark.asyncio
    async def test_batch_store_memories_basic(self):
        """Test basic batch memory storage."""
        mock_hot_store = AsyncMock()

        memories = [
            {
                "memory_id": "mem1",
                "text": "First memory",
                "embedding": [0.1] * 384,
                "metadata": {"source": "http://localhost:8678"}
            },
            {
                "memory_id": "mem2",
                "text": "Second memory",
                "embedding": [0.2] * 384,
            }
        ]

        with patch('akosha.mcp.tools.session_buddy_tools.store_memory') as mock_store:
            mock_store.side_effect = [
                {"status": "stored", "memory_id": "mem1", "embedding_dim": 384},
                {"status": "stored", "memory_id": "mem2", "embedding_dim": 384},
            ]

            result = await batch_store_memories(memories)

            # Should succeed completely
            assert result["status"] == "completed"
            assert result["total"] == 2
            assert result["stored"] == 2
            assert result["failed"] == 0
            assert result["errors"] == []
            assert "stored_at" in result

            # Verify store_memory was called twice
            assert mock_store.call_count == 2

    @pytest.mark.asyncio
    async def test_batch_store_memories_partial_success(self):
        """Test batch memory storage with partial success."""
        mock_hot_store = AsyncMock()

        memories = [
            {
                "memory_id": "mem1",
                "text": "Valid memory",
                "metadata": {"source": "http://localhost:8678"}
            },
            {
                "memory_id": "",
                "text": "Invalid memory (no ID)"
            },
            {
                "memory_id": "mem2",
                "text": "",
                "metadata": {"source": "http://localhost:8678"}
            }
        ]

        with patch('akosha.mcp.tools.session_buddy_tools.store_memory') as mock_store:
            mock_store.side_effect = [
                {"status": "stored", "memory_id": "mem1", "embedding_dim": None},
                {"status": "failed", "memory_id": "", "error": "Invalid memory_id"},
                {"status": "failed", "memory_id": "mem2", "error": "Invalid text"},
            ]

            result = await batch_store_memories(memories)

            # Should be partial success
            assert result["status"] == "partial"
            assert result["total"] == 3
            assert result["stored"] == 1
            assert result["failed"] == 2
            assert len(result["errors"]) == 2

            # Check error details
            errors = result["errors"]
            assert any(e["memory_id"] == "" for e in errors)
            assert any(e["memory_id"] == "mem2" for e in errors)

    @pytest.mark.asyncio
    async def test_batch_store_memories_batch_too_large(self):
        """Test batch memory storage with oversized batch."""
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
    async def test_batch_store_memories_empty_batch(self):
        """Test batch memory storage with empty batch."""
        memories = []

        result = await batch_store_memories(memories)

        # Should succeed with zero items
        assert result["status"] == "completed"
        assert result["total"] == 0
        assert result["stored"] == 0
        assert result["failed"] == 0
        assert result["errors"] == []

    @pytest.mark.asyncio
    async def test_batch_store_memories_all_fail(self):
        """Test batch memory storage where all items fail."""
        memories = [
            {"memory_id": "", "text": "No ID"},
            {"memory_id": "mem2", "text": ""}
        ]

        with patch('akosha.mcp.tools.session_buddy_tools.store_memory') as mock_store:
            mock_store.side_effect = [
                {"status": "failed", "memory_id": "", "error": "No ID"},
                {"status": "failed", "memory_id": "mem2", "error": "No text"},
            ]

            result = await batch_store_memories(memories)

            # Should fail completely
            assert result["status"] == "failed"
            assert result["total"] == 2
            assert result["stored"] == 0
            assert result["failed"] == 2
            assert len(result["errors"]) == 2

    @pytest.mark.asyncio
    async def test_batch_store_memories_exception_handling(self):
        """Test batch memory storage with exceptions."""
        memories = [
            {
                "memory_id": "mem1",
                "text": "Valid memory",
                "metadata": {"source": "http://localhost:8678"}
            },
            {
                "memory_id": "mem2",
                "text": "Another memory",
                "metadata": {"source": "http://localhost:8678"}
            }
        ]

        with patch('akosha.mcp.tools.session_buddy_tools.store_memory') as mock_store:
            # First succeeds, second throws exception
            mock_store.side_effect = [
                {"status": "stored", "memory_id": "mem1", "embedding_dim": None},
                Exception("Unexpected error"),
            ]

            result = await batch_store_memories(memories)

            # Should be partial
            assert result["status"] == "partial"
            assert result["total"] == 2
            assert result["stored"] == 1
            assert result["failed"] == 1
            assert len(result["errors"]) == 1

            # Check error contains exception details
            error = result["errors"][0]
            assert error["memory_id"] == "mem2"
            assert "Unexpected error" in error["error"]

    @pytest.mark.asyncio
    async def test_batch_store_memories_metadata_variations(self):
        """Test batch memory storage with different metadata configurations."""
        memories = [
            {
                "memory_id": "mem1",
                "text": "Memory 1",
                "metadata": {"source": "source1", "type": "insight"}
            },
            {
                "memory_id": "mem2",
                "text": "Memory 2",
                "metadata": None  # No metadata
            },
            {
                "memory_id": "mem3",
                "text": "Memory 3",
                # Empty metadata dict
            }
        ]

        results = []
        with patch('akosha.mcp.tools.session_buddy_tools.store_memory') as mock_store:
            for mem in memories:
                result = await store_memory(
                    memory_id=mem["memory_id"],
                    text=mem["text"],
                    metadata=mem.get("metadata")
                )
                results.append(result)

            batch_result = await batch_store_memories(memories)

            # All should succeed
            assert batch_result["status"] == "completed"
            assert batch_result["stored"] == 3

            # Verify individual results
            for i, result in enumerate(results):
                assert result["status"] == "stored"


class TestSessionBuddyToolsIntegration:
    """Integration tests for Session-Buddy tools."""

    @pytest.mark.asyncio
    async def test_tools_register_correctly(self):
        """Test that tools can be registered with registry."""
        from unittest.mock import MagicMock

        mock_registry = MagicMock()
        mock_hot_store = AsyncMock()

        # Import the register function
        from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools

        # Should not raise exceptions
        register_session_buddy_tools(mock_registry, mock_hot_store)

        # Verify registration was called
        assert mock_registry.register.call_count == 2

    @pytest.mark.asyncio
    async def test_memory_flow_integration(self):
        """Test complete memory flow from input to storage."""
        mock_hot_store = AsyncMock()
        mock_hot_store.insert = AsyncMock()

        with patch('akosha.mcp.tools.session_buddy_tools.HotRecord') as mock_record_class:
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
                    "tags": ["python", "asyncio"]
                }
            }

            result = await store_memory(**memory_data)

            # Verify complete flow
            assert result["status"] == "stored"
            assert result["memory_id"] == "flow-test-123"
            assert result["embedding_dim"] == 384
            assert result["source"] == "https://session-buddy.example.com"

            # Verify HotRecord was created with correct metadata
            mock_record_class.assert_called_once()
            call_kwargs = mock_record_class.call_kwargs
            assert call_kwargs["system_id"] == "https://session-buddy.example.com"
            assert call_kwargs["conversation_id"] == "flow-test-123"
            assert call_kwargs["content"] == memory_data["text"]
            assert call_kwargs["embedding"] == memory_data["embedding"]
            assert call_kwargs["timestamp"] == memory_data["metadata"]["created_at"]
            assert call_kwargs["metadata"]["original_id"] == "sess-456"
            assert call_kwargs["metadata"]["type"] == "code_insight"
            assert call_kwargs["metadata"]["tags"] == ["python", "asyncio"]
            assert call_kwargs["metadata"]["ingestion_method"] == "http_push"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])