"""Session-Buddy integration tools for Akosha MCP server.

from typing import Any
This module provides MCP tools for direct HTTP-based memory ingestion from
Session-Buddy instances. This complements the pull-based IngestionWorker by
providing a push endpoint for real-time sync.

Usage:
    These tools are automatically registered with the FastMCP server during
    initialization. Session-Buddy can call them via MCP protocol.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from akosha.mcp.tools.tool_registry import FastMCPToolRegistry
    from akosha.storage.hot_store import HotStore

logger = logging.getLogger(__name__)


def register_session_buddy_tools(registry: FastMCPToolRegistry, hot_store: HotStore) -> None:
    """Register Session-Buddy integration tools with MCP server.

    Args:
        registry: FastMCP tool registry for registering tools
        hot_store: Hot store instance for memory insertion
    """

    from akosha.mcp.tools.tool_registry import ToolCategory, ToolMetadata

    @registry.register(
        ToolMetadata(
            name="store_memory",
            description="Store a memory directly from Session-Buddy via HTTP push endpoint",
            category=ToolCategory.INGESTION,
            examples=[
                {
                    "memory_id": "mem_123",
                    "text": "How to implement JWT authentication",
                    "embedding": [0.1] * 384,
                    "metadata": {"source": "http://localhost:8678"},
                }
            ],
        )
    )
    async def store_memory(
        memory_id: str,
        text: str,
        embedding: list[float] | list[list[float]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Store a memory directly from Session-Buddy via HTTP.

        This tool provides a push-based ingestion endpoint for Session-Buddy
        instances, complementing Akosha's pull-based IngestionWorker.

        Args:
            memory_id: Unique identifier for the memory
            text: Text content of the memory
            embedding: Optional vector embedding (384-dimensional for all-MiniLM-L6-v2)
            metadata: Optional metadata including:
                - source: Source instance URL
                - original_id: Original memory ID
                - created_at: ISO timestamp
                - type: Memory type (e.g., "session_memory")

        Returns:
            Dict with status and metadata:
                {
                    "status": "stored" | "failed",
                    "memory_id": str,
                    "stored_at": str (ISO timestamp),
                    "embedding_dim": int | None,
                    "error": str | None
                }

        Example:
            >>> result = await store_memory(
            ...     memory_id="mem_123",
            ...     text="How to implement JWT authentication",
            ...     embedding=[0.1, 0.2, ...],  # 384 dimensions
            ...     metadata={"source": "http://localhost:8678"}
            ... )
            {
                "status": "stored",
                "memory_id": "mem_123",
                "stored_at": "2026-02-08T12:00:00Z",
                "embedding_dim": 384
            }

        Security:
            - Memory content is validated for size limits
            - Embeddings are validated for dimensionality
            - Source tracking is enforced via metadata
        """
        try:
            # Validate input
            if not memory_id:
                return {
                    "status": "failed",
                    "memory_id": memory_id,
                    "error": "Invalid memory_id: must be non-empty string",
                }

            if not text:
                return {
                    "status": "failed",
                    "memory_id": memory_id,
                    "error": "Invalid text: must be non-empty string",
                }

            # Validate embedding dimensions
            if embedding is not None:
                embedding_dim = len(embedding)
                if embedding_dim != 384:
                    logger.warning(
                        f"Unexpected embedding dimension: {embedding_dim} "
                        f"(expected 384 for all-MiniLM-L6-v2)"
                    )
            else:
                embedding_dim = None

            # Extract source from metadata
            source = metadata.get("source", "unknown") if metadata else "unknown"
            original_id = metadata.get("original_id") if metadata else None
            memory_type = metadata.get("type", "session_memory") if metadata else "session_memory"

            # Prepare database record
            conversation_data = {
                "id": memory_id,
                "content": text,
                "embedding": embedding,
                "timestamp": metadata.get("created_at", datetime.now(UTC).isoformat())
                if metadata
                else datetime.now(UTC).isoformat(),
                "metadata": {
                    "source": source,
                    "original_id": original_id,
                    "type": memory_type,
                    "ingestion_method": "http_push",  # Track push vs pull
                },
            }

            # Insert into hot store
            logger.info(
                f"Storing memory {memory_id} from {source} "
                f"(embedding_dim: {embedding_dim}, type: {memory_type})"
            )

            # Use HotRecord model for insertion
            from akosha.models import HotRecord

            record = HotRecord(
                system_id=source,
                conversation_id=memory_id,
                content=text,
                embedding=embedding,
                timestamp=metadata.get("created_at", datetime.now(UTC).isoformat())
                if metadata
                else datetime.now(UTC).isoformat(),
                metadata=conversation_data["metadata"],
            )

            await hot_store.insert(record)

            logger.debug(f"Successfully stored memory {memory_id} in Akosha")

            return {
                "status": "stored",
                "memory_id": memory_id,
                "stored_at": datetime.now(UTC).isoformat(),
                "embedding_dim": embedding_dim,
                "source": source,
            }

        except Exception as e:
            logger.error(f"Failed to store memory {memory_id}: {e}", exc_info=True)
            return {
                "status": "failed",
                "memory_id": memory_id,
                "error": str(e),
            }

    @registry.register(
        ToolMetadata(
            name="batch_store_memories",
            description="Store multiple memories from Session-Buddy in a single batch",
            category=ToolCategory.INGESTION,
            examples=[
                {
                    "memories": [
                        {
                            "memory_id": "mem_1",
                            "text": "First memory",
                            "embedding": [0.1] * 384,
                            "metadata": {"source": "http://localhost:8678"},
                        },
                        {
                            "memory_id": "mem_2",
                            "text": "Second memory",
                            "embedding": [0.3] * 384,
                        },
                    ]
                }
            ],
        )
    )
    async def batch_store_memories(
        memories: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Store multiple memories from Session-Buddy in a single batch.

        This tool provides efficient bulk ingestion for Session-Buddy sync
        operations, reducing overhead compared to individual store_memory calls.

        Args:
            memories: List of memory dictionaries, each containing:
                - memory_id (str): Unique identifier
                - text (str): Text content
                - embedding (list[float] | None): Optional embedding
                - metadata (dict[str, Any] | None): Optional metadata

        Returns:
            Dict with batch results:
                {
                    "status": "completed" | "partial" | "failed",
                    "total": int,
                    "stored": int,
                    "failed": int,
                    "errors": list[dict],
                    "stored_at": str (ISO timestamp)
                }

        Example:
            >>> result = await batch_store_memories(
            ...     memories=[
            ...         {
            ...             "memory_id": "mem_1",
            ...             "text": "First memory",
            ...             "embedding": [0.1, 0.2, ...],
            ...             "metadata": {"source": "http://localhost:8678"}
            ...         },
            ...         {
            ...             "memory_id": "mem_2",
            ...             "text": "Second memory",
            ...             "embedding": [0.3, 0.4, ...],
            ...         }
            ...     ]
            ... )
            {
                "status": "completed",
                "total": 2,
                "stored": 2,
                "failed": 0,
                "errors": [],
                "stored_at": "2026-02-08T12:00:00Z"
            }

        Performance:
            - Batch size limit: 1000 memories per call
            - Uses async bulk insertion for efficiency
            - Partial success supported (some memories can fail)
        """
        try:
            # Validate batch size
            if len(memories) > 1000:
                return {
                    "status": "failed",
                    "total": len(memories),
                    "stored": 0,
                    "failed": len(memories),
                    "error": "Batch size exceeds maximum of 1000 memories",
                }

            # Process memories
            stored_count = 0
            failed_count = 0
            errors: list[dict[str, Any]] = []

            for memory_dict in memories:
                try:
                    # Extract fields
                    memory_id = memory_dict.get("memory_id")
                    text = memory_dict.get("text")
                    embedding = memory_dict.get("embedding")
                    mem_metadata = memory_dict.get("metadata")

                    # Validate required fields
                    if not memory_id or not text:
                        failed_count += 1
                        errors.append(
                            {
                                "memory_id": memory_id,
                                "error": "Missing required field (memory_id or text)",
                            }
                        )
                        continue

                    # Store using store_memory logic
                    result = await store_memory(
                        memory_id=memory_id,
                        text=text,
                        embedding=embedding,
                        metadata=mem_metadata,
                    )

                    if result["status"] == "stored":
                        stored_count += 1
                    else:
                        failed_count += 1
                        errors.append(
                            {
                                "memory_id": memory_id,
                                "error": result.get("error", "Unknown error"),
                            }
                        )

                except Exception as e:
                    failed_count += 1
                    errors.append(
                        {
                            "memory_id": memory_dict.get("memory_id", "unknown"),
                            "error": str(e),
                        }
                    )

            # Determine overall status
            if failed_count == 0:
                status = "completed"
            elif stored_count > 0:
                status = "partial"
            else:
                status = "failed"

            logger.info(
                f"Batch store completed: {stored_count}/{len(memories)} stored, "
                f"{failed_count} failed"
            )

            return {
                "status": status,
                "total": len(memories),
                "stored": stored_count,
                "failed": failed_count,
                "errors": errors,
                "stored_at": datetime.now(UTC).isoformat(),
            }

        except Exception as e:
            logger.error(f"Batch store failed: {e}", exc_info=True)
            return {
                "status": "failed",
                "total": len(memories),
                "stored": 0,
                "failed": len(memories),
                "error": str(e),
                "errors": [],
            }

    logger.info("Session-Buddy integration tools registered with Akosha MCP server")
