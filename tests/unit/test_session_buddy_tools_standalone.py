"""Standalone tests for Session-Buddy integration tools.

Tests HTTP-based memory ingestion from Session-Buddy instances via MCP tools.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import UTC, datetime

# Test the registration function and the internal logic
from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools


class TestSessionBuddyToolsRegistration:
    """Test tool registration functionality."""

    @pytest.mark.asyncio
    async def test_register_tools_success(self):
        """Test successful tool registration."""
        mock_registry = MagicMock()
        mock_hot_store = AsyncMock()

        # Should not raise exceptions
        register_session_buddy_tools(mock_registry, mock_hot_store)

        # Verify register was called twice (for both tools)
        assert mock_registry.register.call_count == 2

        # Verify tool metadata
        calls = mock_registry.register.call_args_list
        assert len(calls) == 2

        # First tool should be store_memory
        first_call = calls[0]
        tool_info = first_call[0][0]  # First argument
        assert tool_info.name == "store_memory"
        assert "Store a memory" in tool_info.description
        assert hasattr(first_call[0][1], '__code__')  # Async function

        # Second tool should be batch_store_memories
        second_call = calls[1]
        tool_info = second_call[0][0]  # First argument
        assert tool_info.name == "batch_store_memories"
        assert "Store multiple memories" in tool_info.description
        assert hasattr(second_call[0][1], '__code__')  # Async function


class TestStoreMemoryLogic:
    """Test the store_memory function logic."""

    @pytest.mark.asyncio
    async def test_store_memory_validation(self):
        """Test memory validation logic."""
        # Test cases from original implementation
        test_cases = [
            # Valid cases
            {
                "memory_id": "valid-id",
                "text": "valid text",
                "embedding": [0.1] * 384,
                "metadata": {"source": "test"},
                "should_succeed": True
            },
            {
                "memory_id": "valid-id",
                "text": "valid text",
                "embedding": None,
                "metadata": None,
                "should_succeed": True
            },
            # Invalid cases
            {
                "memory_id": "",
                "text": "valid text",
                "should_succeed": False
            },
            {
                "memory_id": "valid-id",
                "text": "",
                "should_succeed": False
            },
        ]

        for case in test_cases:
            # Mock HotRecord and hot_store
            with patch('akosha.mcp.tools.session_buddy_tools.HotRecord') as mock_record_class:
                mock_record = MagicMock()
                mock_record_class.return_value = mock_record

                # Mock hot_store
                mock_hot_store = AsyncMock()
                mock_hot_store.insert = AsyncMock()

                # Extract parameters
                kwargs = {
                    "memory_id": case["memory_id"],
                    "text": case["text"]
                }
                if "embedding" in case:
                    kwargs["embedding"] = case["embedding"]
                if "metadata" in case:
                    kwargs["metadata"] = case["metadata"]

                # Import the function directly
                from akosha.mcp.tools.session_buddy_tools import store_memory
                result = await store_memory(**kwargs)

                if case["should_succeed"]:
                    assert result["status"] == "stored"
                    assert result["memory_id"] == case["memory_id"]
                    if "embedding" in case and case["embedding"] is not None:
                        assert result["embedding_dim"] == 384
                    # Verify database operations
                    mock_record_class.assert_called_once()
                    mock_hot_store.insert.assert_called_once()
                else:
                    assert result["status"] == "failed"
                    assert result["memory_id"] == case["memory_id"]
                    assert "error" in result
                    # Should not have called database operations
                    mock_record_class.assert_not_called()
                    mock_hot_store.insert.assert_not_called()

    @pytest.mark.asyncio
    async def test_store_memory_embedding_dimensions(self):
        """Test embedding dimension handling."""
        test_dims = [256, 384, 768]  # Common embedding dimensions

        for dim in test_dims:
            with patch('akosha.mcp.tools.session_buddy_tools.HotRecord') as mock_record_class:
                mock_record = MagicMock()
                mock_record_class.return_value = mock_record

                mock_hot_store = AsyncMock()
                mock_hot_store.insert = AsyncMock()

                from akosha.mcp.tools.session_buddy_tools import store_memory
                result = await store_memory(
                    memory_id=f"test-{dim}",
                    text=f"Test text with {dim} dimensions",
                    embedding=[0.1] * dim
                )

                # Should store successfully
                assert result["status"] == "stored"
                assert result["embedding_dim"] == dim

    @pytest.mark.asyncio
    async def test_store_memory_metadata_handling(self):
        """Test metadata extraction and storage."""
        test_metadata = [
            {
                "input": {"source": "http://localhost:8678", "original_id": "orig-123", "type": "insight"},
                "expected_source": "http://localhost:8678",
                "expected_original_id": "orig-123",
                "expected_type": "insight"
            },
            {
                "input": {"source": "http://localhost:8678"},
                "expected_source": "http://localhost:8678",
                "expected_original_id": None,
                "expected_type": "session_memory"
            },
            {
                "input": None,
                "expected_source": "unknown",
                "expected_original_id": None,
                "expected_type": "session_memory"
            }
        ]

        for case in test_metadata:
            with patch('akosha.mcp.tools.session_buddy_tools.HotRecord') as mock_record_class:
                mock_record = MagicMock()
                mock_record_class.return_value = mock_record

                mock_hot_store = AsyncMock()
                mock_hot_store.insert = AsyncMock()

                from akosha.mcp.tools.session_buddy_tools import store_memory
                result = await store_memory(
                    memory_id="test-metadata",
                    text="Test metadata handling",
                    metadata=case["input"]
                )

                assert result["status"] == "stored"
                assert result["source"] == case["expected_source"]

                # Check HotRecord metadata
                call_args = mock_record_class.call_args
                metadata = call_args.kwargs["metadata"]
                assert metadata["source"] == case["expected_source"]
                assert metadata["original_id"] == case["expected_original_id"]
                assert metadata["type"] == case["expected_type"]
                assert metadata["ingestion_method"] == "http_push"


class TestBatchStoreMemoriesLogic:
    """Test the batch_store_memories function logic."""

    @pytest.mark.asyncio
    async def test_batch_validation_logic(self):
        """Test batch size validation."""
        from akosha.mcp.tools.session_buddy_tools import batch_store_memories

        # Test oversized batch
        large_batch = [{"memory_id": f"mem{i}", "text": f"Text{i}"} for i in range(1001)]
        result = await batch_store_memories(large_batch)

        assert result["status"] == "failed"
        assert result["total"] == 1001
        assert "exceeds maximum" in result["error"]

        # Test valid batch size
        valid_batch = [{"memory_id": f"mem{i}", "text": f"Text{i}"} for i in range(1000)]
        with patch('akosha.mcp.tools.session_buddy_tools.store_memory') as mock_store:
            mock_store.return_value = {"status": "stored", "memory_id": "test"}
            result = await batch_store_memories(valid_batch)

            assert result["status"] == "completed"
            assert mock_store.call_count == 1000

    @pytest.mark.asyncio
    async def test_batch_processing_logic(self):
        """Test batch processing with different scenarios."""
        test_cases = [
            {
                "name": "all_success",
                "memories": [
                    {"memory_id": "mem1", "text": "Text1"},
                    {"memory_id": "mem2", "text": "Text2"}
                ],
                "store_results": [
                    {"status": "stored", "memory_id": "mem1"},
                    {"status": "stored", "memory_id": "mem2"}
                ],
                "expected_status": "completed",
                "expected_stored": 2
            },
            {
                "name": "partial_success",
                "memories": [
                    {"memory_id": "mem1", "text": "Text1"},
                    {"memory_id": "", "text": "Invalid"},
                    {"memory_id": "mem2", "text": ""}
                ],
                "store_results": [
                    {"status": "stored", "memory_id": "mem1"},
                    {"status": "failed", "memory_id": "", "error": "No ID"},
                    {"status": "failed", "memory_id": "mem2", "error": "No text"}
                ],
                "expected_status": "partial",
                "expected_stored": 1
            },
            {
                "name": "all_fail",
                "memories": [
                    {"memory_id": "", "text": "Invalid"},
                    {"memory_id": "mem2", "text": ""}
                ],
                "store_results": [
                    {"status": "failed", "memory_id": ""},
                    {"status": "failed", "memory_id": "mem2"}
                ],
                "expected_status": "failed",
                "expected_stored": 0
            }
        ]

        for case in test_cases:
            with patch('akosha.mcp.tools.session_buddy_tools.store_memory') as mock_store:
                mock_store.side_effect = case["store_results"]

                from akosha.mcp.tools.session_buddy_tools import batch_store_memories
                result = await batch_store_memories(case["memories"])

                assert result["status"] == case["expected_status"]
                assert result["stored"] == case["expected_stored"]
                assert result["total"] == len(case["memories"])
                assert result["failed"] == len(case["memories"]) - case["expected_stored"]

    @pytest.mark.asyncio
    async def test_batch_error_handling(self):
        """Test batch error collection."""
        from akosha.mcp.tools.session_buddy_tools import batch_store_memories

        memories = [
            {"memory_id": "mem1", "text": "Text1"},
            {"memory_id": "mem2", "text": "Text2"},
            {"memory_id": "mem3", "text": "Text3"}
        ]

        # Mix of successful and failed with exceptions
        with patch('akosha.mcp.tools.session_buddy_tools.store_memory') as mock_store:
            mock_store.side_effect = [
                {"status": "stored", "memory_id": "mem1"},
                Exception("Database error"),
                {"status": "stored", "memory_id": "mem3"},
            ]

            result = await batch_store_memories(memories)

            assert result["status"] == "partial"
            assert result["stored"] == 2
            assert result["failed"] == 1
            assert len(result["errors"]) == 1

            error = result["errors"][0]
            assert error["memory_id"] == "mem2"
            assert "Database error" in error["error"]


class TestMemoryPersistence:
    """Test memory persistence behavior."""

    @pytest.mark.asyncio
    async def test_memory_record_creation(self):
        """Test HotRecord creation details."""
        with patch('akosha.mcp.tools.session_buddy_tools.HotRecord') as mock_record_class:
            mock_record = MagicMock()
            mock_record_class.return_value = mock_record

            mock_hot_store = AsyncMock()
            mock_hot_store.insert = AsyncMock()

            from akosha.mcp.tools.session_buddy_tools import store_memory

            # Test with timestamp
            test_timestamp = "2026-02-08T12:00:00Z"
            result = await store_memory(
                memory_id="timestamp-test",
                text="Test with timestamp",
                metadata={"created_at": test_timestamp}
            )

            assert result["status"] == "stored"

            # Verify timestamp handling
            call_args = mock_record_class.call_args
            assert call_args.kwargs["timestamp"] == test_timestamp
            mock_hot_store.insert.assert_called_once_with(mock_record)

    @pytest.mark.asyncio
    async def test_default_timestamp(self):
        """Test default timestamp generation."""
        with patch('akosha.mcp.tools.session_buddy_tools.HotRecord') as mock_record_class:
            mock_record = MagicMock()
            mock_record_class.return_value = mock_record

            mock_hot_store = AsyncMock()
            mock_hot_store.insert = AsyncMock()

            from akosha.mcp.tools.session_buddy_tools import store_memory
            from datetime import datetime, UTC

            result = await store_memory(
                memory_id="default-time-test",
                text="Test with default timestamp"
            )

            assert result["status"] == "stored"

            # Verify default timestamp is recent
            call_args = mock_record_class.call_args
            stored_timestamp = call_args.kwargs["timestamp"]
            now = datetime.now(UTC).isoformat()

            # Should be approximately the same time (within reasonable tolerance)
            assert stored_timestamp[:13] == now[:13]  # Same minute


if __name__ == "__main__":
    pytest.main([__file__, "-v"])