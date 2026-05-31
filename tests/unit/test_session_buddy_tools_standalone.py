"""Standalone tests for Session-Buddy integration tools.

Tests HTTP-based memory ingestion from Session-Buddy instances via MCP tools.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools


def _make_registry_and_capture():
    """Create a mock registry that captures both metadata and functions.

    The real registry.register(metadata) returns a decorator. We need the
    decorator to be an identity function so that the original async functions
    are preserved in closures (e.g. batch_store captures store_memory).
    """
    registry = MagicMock()
    captured = []

    def _tracking_decorator(metadata):
        def _decorator(func):
            captured.append(func)
            return func

        return _decorator

    registry.register.side_effect = _tracking_decorator
    registry._captured_funcs = captured
    return registry, captured


class TestSessionBuddyToolsRegistration:
    """Test tool registration functionality."""

    @pytest.mark.asyncio
    async def test_register_tools_success(self):
        """Test successful tool registration."""
        mock_registry, captured = _make_registry_and_capture()
        mock_hot_store = AsyncMock()

        register_session_buddy_tools(mock_registry, mock_hot_store)

        assert mock_registry.register.call_count == 2
        assert len(captured) == 2

        # First tool should be store_memory
        first_call = mock_registry.register.call_args_list[0]
        metadata = first_call[0][0]
        store_func = captured[0]
        assert metadata.name == "store_memory"
        assert "Store a memory" in metadata.description
        assert hasattr(store_func, "__code__")

        # Second tool should be batch_store_memories
        second_call = mock_registry.register.call_args_list[1]
        metadata = second_call[0][0]
        batch_func = captured[1]
        assert metadata.name == "batch_store_memories"
        assert "Store multiple memories" in metadata.description
        assert hasattr(batch_func, "__code__")


class TestStoreMemoryLogic:
    """Test the store_memory function logic."""

    @pytest.mark.asyncio
    async def test_store_memory_validation(self):
        """Test memory validation logic."""
        test_cases = [
            {
                "memory_id": "valid-id",
                "text": "valid text",
                "embedding": [0.1] * 384,
                "metadata": {"source": "test"},
                "should_succeed": True,
            },
            {
                "memory_id": "valid-id",
                "text": "valid text",
                "embedding": None,
                "metadata": None,
                "should_succeed": False,  # embedding is required for HotRecord
            },
            {"memory_id": "", "text": "valid text", "should_succeed": False},
            {"memory_id": "valid-id", "text": "", "should_succeed": False},
        ]

        for case in test_cases:
            with patch("akosha.models.HotRecord") as mock_record_class:
                mock_record = MagicMock()
                mock_record_class.return_value = mock_record

                mock_registry, captured = _make_registry_and_capture()
                mock_hot_store = AsyncMock()
                mock_hot_store.insert = AsyncMock()

                register_session_buddy_tools(mock_registry, mock_hot_store)
                store_func = captured[0]

                kwargs = {"memory_id": case["memory_id"], "text": case["text"]}
                if "embedding" in case:
                    kwargs["embedding"] = case["embedding"]
                if "metadata" in case:
                    kwargs["metadata"] = case["metadata"]

                result = await store_func(**kwargs)

                if case["should_succeed"]:
                    assert result["status"] == "stored"
                    assert result["memory_id"] == case["memory_id"]
                    if "embedding" in case and case["embedding"] is not None:
                        assert result["embedding_dim"] == 384
                    mock_record_class.assert_called_once()
                    mock_hot_store.insert.assert_called_once()
                else:
                    assert result["status"] == "failed"
                    assert result["memory_id"] == case["memory_id"]
                    assert "error" in result
                    mock_record_class.assert_not_called()
                    mock_hot_store.insert.assert_not_called()

    @pytest.mark.asyncio
    async def test_store_memory_embedding_dimensions(self):
        """Test embedding dimension handling."""
        test_dims = [256, 384, 768]

        for dim in test_dims:
            with patch("akosha.models.HotRecord") as mock_record_class:
                mock_record = MagicMock()
                mock_record_class.return_value = mock_record

                mock_registry, captured = _make_registry_and_capture()
                mock_hot_store = AsyncMock()
                mock_hot_store.insert = AsyncMock()

                register_session_buddy_tools(mock_registry, mock_hot_store)
                store_func = captured[0]

                result = await store_func(
                    memory_id=f"test-{dim}",
                    text=f"Test text with {dim} dimensions",
                    embedding=[0.1] * dim,
                )

                assert result["status"] == "stored"
                assert result["embedding_dim"] == dim

    @pytest.mark.asyncio
    async def test_store_memory_metadata_handling(self):
        """Test metadata extraction and storage."""
        test_metadata = [
            {
                "input": {
                    "source": "http://localhost:8678",
                    "original_id": "orig-123",
                    "type": "insight",
                },
                "expected_source": "http://localhost:8678",
                "expected_original_id": "orig-123",
                "expected_type": "insight",
            },
            {
                "input": {"source": "http://localhost:8678"},
                "expected_source": "http://localhost:8678",
                "expected_original_id": None,
                "expected_type": "session_memory",
            },
            {
                "input": None,
                "expected_source": "unknown",
                "expected_original_id": None,
                "expected_type": "session_memory",
            },
        ]

        for case in test_metadata:
            with patch("akosha.models.HotRecord") as mock_record_class:
                mock_record = MagicMock()
                mock_record_class.return_value = mock_record

                mock_registry, captured = _make_registry_and_capture()
                mock_hot_store = AsyncMock()
                mock_hot_store.insert = AsyncMock()

                register_session_buddy_tools(mock_registry, mock_hot_store)
                store_func = captured[0]

                result = await store_func(
                    memory_id="test-metadata",
                    text="Test metadata handling",
                    embedding=[0.1] * 384,
                    metadata=case["input"],
                )

                assert result["status"] == "stored"
                assert result["source"] == case["expected_source"]

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
        mock_registry, captured = _make_registry_and_capture()
        mock_hot_store = AsyncMock()

        register_session_buddy_tools(mock_registry, mock_hot_store)
        batch_func = captured[1]

        # Test oversized batch
        large_batch = [{"memory_id": f"mem{i}", "text": f"Text{i}"} for i in range(1001)]
        result = await batch_func(large_batch)

        assert result["status"] == "failed"
        assert result["total"] == 1001
        assert "exceeds maximum" in result["error"]

        # Test valid batch size
        mock_registry2, captured2 = _make_registry_and_capture()
        mock_hot_store2 = AsyncMock()

        with patch("akosha.models.HotRecord") as mock_hot_record:
            mock_record_instance = MagicMock()
            mock_hot_record.return_value = mock_record_instance

            register_session_buddy_tools(mock_registry2, mock_hot_store2)
            batch_func2 = captured2[1]

            valid_batch = [{"memory_id": f"mem{i}", "text": f"Text{i}", "embedding": [0.1] * 384} for i in range(1000)]
            result = await batch_func2(valid_batch)

            assert result["status"] == "completed"
            assert result["stored"] == 1000

    @pytest.mark.asyncio
    async def test_batch_processing_logic(self):
        """Test batch processing with different scenarios."""
        test_cases = [
            {
                "name": "all_success",
                "memories": [
                    {"memory_id": "mem1", "text": "Text1", "embedding": [0.1] * 384},
                    {"memory_id": "mem2", "text": "Text2", "embedding": [0.1] * 384},
                ],
                "expected_status": "completed",
                "expected_stored": 2,
            },
            {
                "name": "partial_success",
                "memories": [
                    {"memory_id": "mem1", "text": "Text1", "embedding": [0.1] * 384},
                    {"memory_id": "", "text": "Invalid"},
                    {"memory_id": "mem2", "text": ""},
                ],
                "expected_status": "partial",
                "expected_stored": 1,
            },
            {
                "name": "all_fail",
                "memories": [
                    {"memory_id": "", "text": "Invalid"},
                    {"memory_id": "mem2", "text": ""},
                ],
                "expected_status": "failed",
                "expected_stored": 0,
            },
        ]

        for case in test_cases:
            mock_registry, captured = _make_registry_and_capture()
            mock_hot_store = AsyncMock()

            with patch("akosha.models.HotRecord") as mock_hot_record:
                mock_record_instance = MagicMock()
                mock_hot_record.return_value = mock_record_instance

                register_session_buddy_tools(mock_registry, mock_hot_store)
                batch_func = captured[1]

                result = await batch_func(case["memories"])

                assert result["status"] == case["expected_status"], (
                    f"Case '{case['name']}': expected {case['expected_status']}, got {result['status']}"
                )
                assert result["stored"] == case["expected_stored"]
                assert result["total"] == len(case["memories"])
                assert result["failed"] == len(case["memories"]) - case["expected_stored"]

    @pytest.mark.asyncio
    async def test_batch_error_handling(self):
        """Test batch error collection."""
        mock_registry, captured = _make_registry_and_capture()
        mock_hot_store = AsyncMock()

        call_count = 0

        def hot_record_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise Exception("Database error")
            return MagicMock()

        with patch("akosha.models.HotRecord", side_effect=hot_record_side_effect):
            register_session_buddy_tools(mock_registry, mock_hot_store)
            batch_func = captured[1]

            memories = [
                {"memory_id": "mem1", "text": "Text1", "embedding": [0.1] * 384},
                {"memory_id": "mem2", "text": "Text2", "embedding": [0.1] * 384},
                {"memory_id": "mem3", "text": "Text3", "embedding": [0.1] * 384},
            ]

            result = await batch_func(memories)

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
        with patch("akosha.models.HotRecord") as mock_record_class:
            mock_record = MagicMock()
            mock_record_class.return_value = mock_record

            mock_registry, captured = _make_registry_and_capture()
            mock_hot_store = AsyncMock()
            mock_hot_store.insert = AsyncMock()

            register_session_buddy_tools(mock_registry, mock_hot_store)
            store_func = captured[0]

            test_timestamp = "2026-02-08T12:00:00Z"
            result = await store_func(
                memory_id="timestamp-test",
                text="Test with timestamp",
                embedding=[0.1] * 384,
                metadata={"created_at": test_timestamp},
            )

            assert result["status"] == "stored"

            call_args = mock_record_class.call_args
            stored_ts = call_args.kwargs["timestamp"]
            # HotRecord stores datetime objects, not strings
            assert stored_ts.year == 2026
            assert stored_ts.month == 2
            assert stored_ts.day == 8
            assert stored_ts.hour == 12
            assert stored_ts.minute == 0
            mock_hot_store.insert.assert_called_once_with(mock_record)

    @pytest.mark.asyncio
    async def test_default_timestamp(self):
        """Test default timestamp generation."""
        with patch("akosha.models.HotRecord") as mock_record_class:
            mock_record = MagicMock()
            mock_record_class.return_value = mock_record

            mock_registry, captured = _make_registry_and_capture()
            mock_hot_store = AsyncMock()
            mock_hot_store.insert = AsyncMock()

            register_session_buddy_tools(mock_registry, mock_hot_store)
            store_func = captured[0]

            from datetime import UTC, datetime

            result = await store_func(
                memory_id="default-time-test",
                text="Test with default timestamp",
                embedding=[0.1] * 384,
            )

            assert result["status"] == "stored"

            call_args = mock_record_class.call_args
            stored_timestamp = call_args.kwargs["timestamp"]
            now = datetime.now(UTC)

            # Should be approximately the same time (within 5 seconds)
            diff = abs((stored_timestamp - now).total_seconds())
            assert diff < 5, f"Timestamp difference {diff}s exceeds 5s tolerance"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
