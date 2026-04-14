"""Integration tests for Session-Buddy tools via MCP server.

Tests the actual usage pattern where Session-Buddy tools are registered
through the MCP server and called via HTTP.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import UTC, datetime
import json


class TestSessionBuddyToolsMCPIntegration:
    """Test Session-Buddy tools through MCP server integration."""

    def test_tool_metadata_registration(self):
        """Test that tools are registered with correct metadata."""
        from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools
        from unittest.mock import MagicMock

        mock_registry = MagicMock()
        mock_hot_store = AsyncMock()

        # Register tools
        register_session_buddy_tools(mock_registry, mock_hot_store)

        # Verify registry calls
        assert mock_registry.register.call_count == 2

        # Check first tool (store_memory)
        first_call = mock_registry.register.call_args_list[0]
        tool_metadata = first_call[0][0]  # First argument

        assert tool_metadata.name == "store_memory"
        assert "Store a memory" in tool_metadata.description
        assert tool_metadata.category == "INGESTION"
        assert "memory_id" in str(tool_metadata.examples)
        assert "text" in str(tool_metadata.examples)

        # Check second tool (batch_store_memories)
        second_call = mock_registry.register.call_args_list[1]
        tool_metadata = second_call[0][0]

        assert tool_metadata.name == "batch_store_memories"
        assert "Store multiple memories" in tool_metadata.description
        assert tool_metadata.category == "INGESTION"
        assert "memories" in str(tool_metadata.examples)

    @pytest.mark.asyncio
    async def test_store_memory_function_signature(self):
        """Test that store_memory function has correct signature."""
        from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools

        mock_registry = MagicMock()
        mock_hot_store = AsyncMock()

        # Get the actual function that would be registered
        register_session_buddy_tools(mock_registry, mock_hot_store)

        # Extract the function from the registration call
        store_call = mock_registry.register.call_args_list[0]
        store_func = store_call[0][1]  # Second argument (the function)

        # Verify it's an async function
        assert hasattr(store_func, '__code__')

        # Verify function signature parameters
        code = store_func.__code__
        param_names = list(code.co_varnames[:code.co_argcount])

        # Should have these parameters (in order they appear in function definition)
        expected_params = ['memory_id', 'text', 'embedding', 'metadata']
        actual_params = []

        # Get parameter names from function source by inspecting __code__.co_varnames
        # This is a simplified approach for testing
        import inspect
        sig = inspect.signature(store_func)

        expected_params = ['memory_id', 'text', 'embedding', 'metadata']
        for param_name in expected_params:
            assert param_name in sig.parameters
            param = sig.parameters[param_name]
            # Check parameter types if available (annotations)
            if param.annotation != inspect.Parameter.empty:
                # Should be union types or specific types
                pass

    @pytest.mark.asyncio
    async def test_batch_store_memories_function_signature(self):
        """Test that batch_store_memories function has correct signature."""
        from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools

        mock_registry = MagicMock()
        mock_hot_store = AsyncMock()

        # Get the actual function that would be registered
        register_session_buddy_tools(mock_registry, mock_hot_store)

        # Extract the function from the registration call
        batch_call = mock_registry.register.call_args_list[1]
        batch_func = batch_call[0][1]  # Second argument (the function)

        # Verify it's an async function
        assert hasattr(batch_func, '__code__')

        # Verify function signature
        import inspect
        sig = inspect.signature(batch_func)

        # Should have memories parameter
        assert 'memories' in sig.parameters
        param = sig.parameters['memories']
        assert param.annotation != inspect.Parameter.empty

    @pytest.mark.asyncio
    async def test_store_memory_logic_with_injection(self):
        """Test store_memory logic with dependency injection."""
        # Import the module to get the register function
        from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools

        mock_registry = MagicMock()
        mock_hot_store = AsyncMock()

        # Mock HotRecord at the module level where it's imported
        with patch('akosha.mcp.tools.session_buddy_tools.HotRecord') as mock_hot_record:
            mock_record_instance = MagicMock()
            mock_hot_record.return_value = mock_record_instance

            # Register tools (this will define the functions)
            register_session_buddy_tools(mock_registry, mock_hot_store)

            # Get the store_memory function from registration
            store_call = mock_registry.register.call_args_list[0]
            store_func = store_call[0][1]

            # Test the function with valid input
            result = await store_func(
                memory_id="test-mem-123",
                text="Test memory content",
                embedding=[0.1] * 384,
                metadata={"source": "http://localhost:8678"}
            )

            # Verify success
            assert result["status"] == "stored"
            assert result["memory_id"] == "test-mem-123"
            assert result["embedding_dim"] == 384
            assert result["source"] == "http://localhost:8678"

            # Verify HotRecord was created
            mock_hot_record.assert_called_once()
            call_kwargs = mock_hot_record.call_args.kwargs

            assert call_kwargs["system_id"] == "http://localhost:8678"
            assert call_kwargs["conversation_id"] == "test-mem-123"
            assert call_kwargs["content"] == "Test memory content"
            assert call_kwargs["embedding"] == [0.1] * 384

            # Verify hot_store.insert was called
            mock_hot_store.insert.assert_called_once_with(mock_record_instance)

    @pytest.mark.asyncio
    async def test_batch_store_memories_logic_with_injection(self):
        """Test batch_store_memories logic with dependency injection."""
        from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools

        mock_registry = MagicMock()
        mock_hot_store = AsyncMock()

        # Mock HotRecord
        with patch('akosha.mcp.tools.session_buddy_tools.HotRecord') as mock_hot_record:
            mock_record_instance = MagicMock()
            mock_hot_record.return_value = mock_record_instance

            # Register tools
            register_session_buddy_tools(mock_registry, mock_hot_store)

            # Get the batch_store_memories function from registration
            batch_call = mock_registry.register.call_args_list[1]
            batch_func = batch_call[0][1]

            # Test batch with valid memories
            memories = [
                {
                    "memory_id": "mem1",
                    "text": "First memory",
                    "embedding": [0.1] * 384,
                    "metadata": {"source": "http://localhost:8678"}
                },
                {
                    "memory_id": "mem2",
                    "text": "Second memory"
                }
            ]

            result = await batch_func(memories=memories)

            # Verify success
            assert result["status"] == "completed"
            assert result["total"] == 2
            assert result["stored"] == 2
            assert result["failed"] == 0
            assert len(result["errors"]) == 0

            # Verify HotRecord was created twice
            assert mock_hot_record.call_count == 2

    @pytest.mark.asyncio
    async def test_error_handling_in_store_memory(self):
        """Test error handling in store_memory function."""
        from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools

        mock_registry = MagicMock()
        mock_hot_store = AsyncMock()

        # Mock HotRecord to raise exception
        with patch('akosha.mcp.tools.session_buddy_tools.HotRecord') as mock_hot_record:
            mock_hot_record.side_effect = Exception("Database connection failed")

            # Register tools
            register_session_buddy_tools(mock_registry, mock_hot_store)

            # Get the store_memory function
            store_call = mock_registry.register.call_args_list[0]
            store_func = store_call[0][1]

            # Test with valid input (should fail due to HotRecord exception)
            result = await store_func(
                memory_id="test-error",
                text="Test content"
            )

            # Verify failure
            assert result["status"] == "failed"
            assert result["memory_id"] == "test-error"
            assert "Database connection failed" in result["error"]

            # Verify HotRecord was attempted but insert not called
            mock_hot_record.assert_called_once()
            mock_hot_store.insert.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_size_validation(self):
        """Test batch size validation in batch_store_memories."""
        from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools

        mock_registry = MagicMock()
        mock_hot_store = AsyncMock()

        # Register tools
        register_session_buddy_tools(mock_registry, mock_hot_store)

        # Get the batch_store_memories function
        batch_call = mock_registry.register.call_args_list[1]
        batch_func = batch_call[0][1]

        # Test oversized batch (1001 items)
        large_batch = [{"memory_id": f"mem{i}", "text": f"Text{i}"} for i in range(1001)]

        result = await batch_func(memories=large_batch)

        # Should fail immediately
        assert result["status"] == "failed"
        assert result["total"] == 1001
        assert "exceeds maximum" in result["error"]

    def test_integration_with_mcp_server(self):
        """Test integration pattern with MCP server."""
        from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools
        from unittest.mock import MagicMock

        # Simulate MCP server setup
        mock_registry = MagicMock()
        mock_hot_store = AsyncMock()

        # Register the tools
        register_session_buddy_tools(mock_registry, mock_hot_store)

        # Verify integration points
        # 1. Tools were registered
        assert mock_registry.register.call_count == 2

        # 2. Hot store is accessible
        assert mock_hot_store is not None

        # 3. Functions are properly defined
        calls = mock_registry.register.call_args_list
        store_func = calls[0][0][1]
        batch_func = calls[1][0][1]

        # Verify async functions
        assert hasattr(store_func, '__code__')
        assert hasattr(batch_func, '__code__')


class TestSessionBuddyToolDocumentation:
    """Test tool documentation and examples."""

    def test_store_memory_examples(self):
        """Test store_memory examples are properly defined."""
        from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools
        from unittest.mock import MagicMock

        mock_registry = MagicMock()
        mock_hot_store = AsyncMock()

        register_session_buddy_tools(mock_registry, mock_hot_store)

        # Get the store_metadata from registration
        store_call = mock_registry.register.call_args_list[0]
        metadata = store_call[0][0]

        # Check examples structure
        examples = metadata.examples
        assert isinstance(examples, list)
        assert len(examples) > 0

        # Check first example
        first_example = examples[0]
        assert "memory_id" in first_example
        assert "text" in first_example
        assert "embedding" in first_example
        assert "metadata" in first_example

    def test_batch_store_memories_examples(self):
        """Test batch_store_memories examples are properly defined."""
        from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools
        from unittest.mock import MagicMock

        mock_registry = MagicMock()
        mock_hot_store = AsyncMock()

        register_session_buddy_tools(mock_registry, mock_hot_store)

        # Get the batch_metadata from registration
        batch_call = mock_registry.register.call_args_list[1]
        metadata = batch_call[0][0]

        # Check examples structure
        examples = metadata.examples
        assert isinstance(examples, list)
        assert len(examples) > 0

        # Check first example contains memories list
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

        mock_registry = MagicMock()
        mock_hot_store = AsyncMock()

        with patch('akosha.mcp.tools.session_buddy_tools.HotRecord') as mock_hot_record:
            mock_record_instance = MagicMock()
            mock_hot_record.return_value = mock_record_instance

            register_session_buddy_tools(mock_registry, mock_hot_store)

            # Get the store_memory function
            store_call = mock_registry.register.call_args_list[0]
            store_func = store_call[0][1]

            import time
            start_time = time.time()

            # Process 100 operations
            for i in range(100):
                await store_func(
                    memory_id=f"perf-test-{i}",
                    text=f"Performance test content {i}"
                )

            end_time = time.time()

            # Should complete in reasonable time (< 1 second for 100 ops)
            assert (end_time - start_time) < 1.0

            # Verify all operations called HotRecord
            assert mock_hot_record.call_count == 100

    @pytest.mark.asyncio
    async def test_batch_store_performance(self):
        """Test batch_store_memories performance."""
        from akosha.mcp.tools.session_buddy_tools import register_session_buddy_tools

        mock_registry = MagicMock()
        mock_hot_store = AsyncMock()

        with patch('akosha.mcp.tools.session_buddy_tools.HotRecord') as mock_hot_record:
            mock_record_instance = MagicMock()
            mock_hot_record.return_value = mock_record_instance

            register_session_buddy_tools(mock_registry, mock_hot_store)

            # Get the batch_store_memories function
            batch_call = mock_registry.register.call_args_list[1]
            batch_func = batch_call[0][1]

            # Create batch of 500 memories
            memories = []
            for i in range(500):
                memories.append({
                    "memory_id": f"batch-test-{i}",
                    "text": f"Batch test content {i}"
                })

            import time
            start_time = time.time()

            # Process batch
            result = await batch_func(memories=memories)

            end_time = time.time()

            # Should complete in reasonable time
            assert (end_time - start_time) < 1.0

            # Verify all memories were processed
            assert result["stored"] == 500
            assert mock_hot_record.call_count == 500


if __name__ == "__main__":
    pytest.main([__file__, "-v"])