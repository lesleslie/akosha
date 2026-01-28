"""Full integration test for Akasha system."""

from datetime import UTC, datetime

import pytest
from pathlib import Path

from akasha.config import config
from akasha.storage.hot_store import HotStore
from akasha.storage.warm_store import WarmStore
from akasha.models import HotRecord
from akasha.processing.knowledge_graph import KnowledgeGraphBuilder


@pytest.mark.asyncio
async def test_storage_layers():
    """Test hot and warm storage initialization."""
    # Test hot store
    hot_store = HotStore(database_path=":memory:")
    await hot_store.initialize()
    
    # Test warm store
    warm_path = Path("/tmp/test_akasha_warm.duckdb")
    warm_store = WarmStore(database_path=warm_path)
    await warm_store.initialize()
    
    # Insert test data
    test_record = HotRecord(
        system_id="test-system",
        conversation_id="test-conv-1",
        content="Test conversation about implementing JWT authentication",
        embedding=[0.1] * 384,  # Use non-zero embedding for similarity
        timestamp=datetime.now(UTC),
        metadata={"user_id": "alice", "project": "auth-system"},
    )

    await hot_store.insert(test_record)

    # Search
    results = await hot_store.search_similar(
        query_embedding=[0.1] * 384,  # Matching query
        system_id="test-system",
        limit=10,
        threshold=0.5,
    )
    
    assert len(results) >= 1
    assert results[0]["conversation_id"] == "test-conv-1"
    
    # Cleanup
    await hot_store.close()
    await warm_store.close()
    if warm_path.exists():
        warm_path.unlink()
    
    print("✅ Storage layers test passed")


@pytest.mark.asyncio
async def test_knowledge_graph():
    """Test knowledge graph construction."""
    builder = KnowledgeGraphBuilder()
    
    # Test entities
    test_conversation = {
        "system_id": "system-001",
        "metadata": {
            "user_id": "alice",
            "project": "auth-system",
        }
    }
    
    entities = await builder.extract_entities(test_conversation)
    edges = await builder.extract_relationships(test_conversation, entities)
    
    await builder.add_to_graph(entities, edges)
    
    # Verify entities added
    assert len(builder.entities) >= 3  # system, user, project
    
    # Test neighbor query
    neighbors = builder.get_neighbors(entity_id="user:alice")
    assert len(neighbors) >= 1
    
    # Test shortest path
    path = builder.find_shortest_path("user:alice", "project:auth-system")
    assert path is not None
    
    # Test statistics
    stats = builder.get_statistics()
    assert stats["total_entities"] >= 3
    assert stats["total_edges"] >= 2
    
    print("✅ Knowledge graph test passed")


if __name__ == "__main__":
    asyncio.run(test_storage_layers())
    asyncio.run(test_knowledge_graph())
    print("\n✅ All integration tests passed!")
