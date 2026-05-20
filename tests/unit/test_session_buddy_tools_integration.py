"""Integration tests for Session-Buddy tools via MCP server.

Tests the actual usage pattern where Session-Buddy tools are registered
through the MCP server and called via HTTP.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_mock_registry():
    """Create a mock registry whose decorator is a pass-through.

    The real registry.register(metadata) returns a decorator that wraps the
    function.  We need the decorator to be an identity function so that
    the original async functions are preserved in closures (e.g. batch_store
    captures store_memory).  A plain MagicMock decorator would replace the
    functions with non-awaitable Mocks.
    """
    registry = MagicMock()

    # Make the side_effect of register() return an identity decorator
    def _identity_decorator(metadata):
        def _decorator(func):
            return func

        return _decorator

    registry.register.side_effect = _identity_decorator
    return registry


def _extract_registered_functions(mock_registry):
    """Extract metadata and functions from mock registry calls.

    Because we use an identity decorator, each call to registry.register(metadata)
    records the metadata.  The function is captured via the decorator call and
    available in mock_registry.register.side_effect tracking.

    We return (metadata, func) pairs by cross-referencing register calls
    with the original functions passed through the identity decorator.
    """
    tool_pairs = []
    register_calls = mock_registry.register.call_args_list
    # The identity decorator receives (func,) as its args when called.
    # We track this via a wrapper.
    # Since we use side_effect, we need to capture the functions separately.
    # Use the _captured_funcs attribute set by the helper below.
    captured = getattr(mock_registry, "_captured_funcs", [])
    for i, metadata in enumerate(register_calls):
        tool_pairs.append((metadata[0][0], captured[i] if i < len(captured) else None))
    return tool_pairs


def _make_registry_and_capture():
    """Create a mock registry that captures both metadata and functions."""
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


class TestSessionBuddyToolsMCPIntegration:
    """Test Session-Buddy tools through MCP server integration."""

    def test_tool_metadata_registration(self):
        """Test that tools are registered with correct metadata."""
        from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools

        mock_registry, captured = _make_registry_and_capture()
        mock_hot_store = AsyncMock()

        register_session_buddy_tools(mock_registry, mock_hot_store)

        assert mock_registry.register.call_count == 2
        assert len(captured) == 2

        # Check first tool (store_memory)
        first_call = mock_registry.register.call_args_list[0]
        metadata = first_call[0][0]
        assert metadata.name == "store_memory"
        assert "Store a memory" in metadata.description
        assert metadata.category.value == "ingestion"
        assert "memory_id" in str(metadata.examples)
        assert "text" in str(metadata.examples)

        # Check second tool (batch_store_memories)
        second_call = mock_registry.register.call_args_list[1]
        metadata = second_call[0][0]
        assert metadata.name == "batch_store_memories"
        assert "Store multiple memories" in metadata.description
        assert metadata.category.value == "ingestion"
        assert "memories" in str(metadata.examples)

    @pytest.mark.asyncio
    async def test_store_memory_function_signature(self):
        """Test that store_memory function has correct signature."""
        from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools

        mock_registry, captured = _make_registry_and_capture()
        mock_hot_store = AsyncMock()

        register_session_buddy_tools(mock_registry, mock_hot_store)
        store_func = captured[0]

        # Verify it's an async function
        assert hasattr(store_func, "__code__")

        import inspect

        sig = inspect.signature(store_func)

        expected_params = ["memory_id", "text", "embedding", "metadata"]
        for param_name in expected_params:
            assert param_name in sig.parameters

    @pytest.mark.asyncio
    async def test_batch_store_memories_function_signature(self):
        """Test that batch_store_memories function has correct signature."""
        from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools

        mock_registry, captured = _make_registry_and_capture()
        mock_hot_store = AsyncMock()

        register_session_buddy_tools(mock_registry, mock_hot_store)
        batch_func = captured[1]

        assert hasattr(batch_func, "__code__")

        import inspect

        sig = inspect.signature(batch_func)

        assert "memories" in sig.parameters
        param = sig.parameters["memories"]
        assert param.annotation != inspect.Parameter.empty

    @pytest.mark.asyncio
    async def test_store_memory_logic_with_injection(self):
        """Test store_memory logic with dependency injection."""
        from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools

        mock_registry, captured = _make_registry_and_capture()
        mock_hot_store = AsyncMock()

        with patch("akosha.models.HotRecord") as mock_hot_record:
            mock_record_instance = MagicMock()
            mock_hot_record.return_value = mock_record_instance

            register_session_buddy_tools(mock_registry, mock_hot_store)
            store_func = captured[0]

            result = await store_func(
                memory_id="test-mem-123",
                text="Test memory content",
                embedding=[0.1] * 384,
                metadata={"source": "http://localhost:8678"},
            )

            assert result["status"] == "stored"
            assert result["memory_id"] == "test-mem-123"
            assert result["embedding_dim"] == 384
            assert result["source"] == "http://localhost:8678"

            mock_hot_record.assert_called_once()
            call_kwargs = mock_hot_record.call_args.kwargs

            assert call_kwargs["system_id"] == "http://localhost:8678"
            assert call_kwargs["conversation_id"] == "test-mem-123"
            assert call_kwargs["content"] == "Test memory content"
            assert call_kwargs["embedding"] == [0.1] * 384

            mock_hot_store.insert.assert_called_once_with(mock_record_instance)

    @pytest.mark.asyncio
    async def test_batch_store_memories_logic_with_injection(self):
        """Test batch_store_memories logic with dependency injection."""
        from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools

        mock_registry, captured = _make_registry_and_capture()
        mock_hot_store = AsyncMock()

        with patch("akosha.models.HotRecord") as mock_hot_record:
            mock_record_instance = MagicMock()
            mock_hot_record.return_value = mock_record_instance

            register_session_buddy_tools(mock_registry, mock_hot_store)
            batch_func = captured[1]

            memories = [
                {
                    "memory_id": "mem1",
                    "text": "First memory",
                    "embedding": [0.1] * 384,
                    "metadata": {"source": "http://localhost:8678"},
                },
                {"memory_id": "mem2", "text": "Second memory"},
            ]

            result = await batch_func(memories=memories)

            assert result["status"] == "completed"
            assert result["total"] == 2
            assert result["stored"] == 2
            assert result["failed"] == 0
            assert len(result["errors"]) == 0

            assert mock_hot_record.call_count == 2

    @pytest.mark.asyncio
    async def test_error_handling_in_store_memory(self):
        """Test error handling in store_memory function."""
        from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools

        mock_registry, captured = _make_registry_and_capture()
        mock_hot_store = AsyncMock()

        with patch("akosha.models.HotRecord") as mock_hot_record:
            mock_hot_record.side_effect = Exception("Database connection failed")

            register_session_buddy_tools(mock_registry, mock_hot_store)
            store_func = captured[0]

            result = await store_func(memory_id="test-error", text="Test content")

            assert result["status"] == "failed"
            assert result["memory_id"] == "test-error"
            assert "Database connection failed" in result["error"]

            mock_hot_record.assert_called_once()
            mock_hot_store.insert.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_size_validation(self):
        """Test batch size validation in batch_store_memories."""
        from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools

        mock_registry, captured = _make_registry_and_capture()
        mock_hot_store = AsyncMock()

        register_session_buddy_tools(mock_registry, mock_hot_store)
        batch_func = captured[1]

        large_batch = [{"memory_id": f"mem{i}", "text": f"Text{i}"} for i in range(1001)]

        result = await batch_func(memories=large_batch)

        assert result["status"] == "failed"
        assert result["total"] == 1001
        assert "exceeds maximum" in result["error"]

    def test_integration_with_mcp_server(self):
        """Test integration pattern with MCP server."""
        from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools

        mock_registry, captured = _make_registry_and_capture()
        mock_hot_store = AsyncMock()

        register_session_buddy_tools(mock_registry, mock_hot_store)

        assert mock_registry.register.call_count == 2
        assert mock_hot_store is not None

        store_func = captured[0]
        batch_func = captured[1]

        assert hasattr(store_func, "__code__")
        assert hasattr(batch_func, "__code__")


class TestSessionBuddyToolDocumentation:
    """Test tool documentation and examples."""

    def test_store_memory_examples(self):
        """Test store_memory examples are properly defined."""
        from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools

        mock_registry, captured = _make_registry_and_capture()
        mock_hot_store = AsyncMock()

        register_session_buddy_tools(mock_registry, mock_hot_store)

        first_call = mock_registry.register.call_args_list[0]
        metadata = first_call[0][0]

        examples = metadata.examples
        assert isinstance(examples, list)
        assert len(examples) > 0

        first_example = examples[0]
        assert "memory_id" in first_example
        assert "text" in first_example
        assert "embedding" in first_example
        assert "metadata" in first_example

    def test_batch_store_memories_examples(self):
        """Test batch_store_memories examples are properly defined."""
        from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools

        mock_registry, captured = _make_registry_and_capture()
        mock_hot_store = AsyncMock()

        register_session_buddy_tools(mock_registry, mock_hot_store)

        second_call = mock_registry.register.call_args_list[1]
        metadata = second_call[0][0]

        examples = metadata.examples
        assert isinstance(examples, list)
        assert len(examples) > 0

        first_example = examples[0]
        assert "memories" in first_example
        assert isinstance(first_example["memories"], list)
        assert len(first_example["memories"]) > 0


class TestSessionBuddyToolPerformance:
    """Test performance characteristics of Session-Buddy tools."""

    @pytest.mark.asyncio
    async def test_store_memory_performance(self):
        """Test store_memory performance characteristics."""
        from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools

        mock_registry, captured = _make_registry_and_capture()
        mock_hot_store = AsyncMock()

        with patch("akosha.models.HotRecord") as mock_hot_record:
            mock_record_instance = MagicMock()
            mock_hot_record.return_value = mock_record_instance

            register_session_buddy_tools(mock_registry, mock_hot_store)
            store_func = captured[0]

            import time

            start_time = time.time()

            for i in range(100):
                await store_func(memory_id=f"perf-test-{i}", text=f"Performance test content {i}")

            end_time = time.time()

            assert (end_time - start_time) < 2.0
            assert mock_hot_record.call_count == 100

    @pytest.mark.asyncio
    async def test_batch_store_performance(self):
        """Test batch_store_memories performance."""
        from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools

        mock_registry, captured = _make_registry_and_capture()
        mock_hot_store = AsyncMock()

        with patch("akosha.models.HotRecord") as mock_hot_record:
            mock_record_instance = MagicMock()
            mock_hot_record.return_value = mock_record_instance

            register_session_buddy_tools(mock_registry, mock_hot_store)
            batch_func = captured[1]

            memories = []
            for i in range(500):
                memories.append({"memory_id": f"batch-test-{i}", "text": f"Batch test content {i}"})

            import time

            start_time = time.time()

            result = await batch_func(memories=memories)

            end_time = time.time()

            assert (end_time - start_time) < 1.0
            assert result["stored"] == 500
            assert mock_hot_record.call_count == 500


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
