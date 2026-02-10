"""Tests for knowledge graph builder."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from akosha.processing.knowledge_graph import (
    GraphEdge,
    GraphEntity,
    KnowledgeGraphBuilder,
)


class TestGraphEntity:
    """Test suite for GraphEntity dataclass."""

    def test_entity_creation(self) -> None:
        """Test creating a graph entity."""
        entity = GraphEntity(
            entity_id="user:123",
            entity_type="user",
            properties={"name": "Alice"},
            source_system="system-1",
        )

        assert entity.entity_id == "user:123"
        assert entity.entity_type == "user"
        assert entity.properties == {"name": "Alice"}
        assert entity.source_system == "system-1"

    def test_entity_default_properties(self) -> None:
        """Test entity with default properties."""
        entity = GraphEntity(
            entity_id="system:test",
            entity_type="system",
        )

        assert entity.properties == {}
        assert entity.source_system == "unknown"


class TestGraphEdge:
    """Test suite for GraphEdge dataclass."""

    def test_edge_creation(self) -> None:
        """Test creating a graph edge."""
        edge = GraphEdge(
            source_id="user:123",
            target_id="project:abc",
            edge_type="worked_on",
            weight=1.5,
            properties={"since": "2023"},
            source_system="system-1",
        )

        assert edge.source_id == "user:123"
        assert edge.target_id == "project:abc"
        assert edge.edge_type == "worked_on"
        assert edge.weight == 1.5
        assert edge.properties == {"since": "2023"}
        assert edge.source_system == "system-1"

    def test_edge_defaults(self) -> None:
        """Test edge with default values."""
        edge = GraphEdge(
            source_id="a",
            target_id="b",
            edge_type="related_to",
        )

        assert edge.weight == 1.0
        assert edge.properties == {}
        assert edge.source_system == "unknown"
        assert isinstance(edge.timestamp, datetime)


class TestKnowledgeGraphBuilder:
    """Test suite for KnowledgeGraphBuilder."""

    @pytest.fixture
    def graph(self) -> KnowledgeGraphBuilder:
        """Create fresh graph builder for each test."""
        return KnowledgeGraphBuilder()

    @pytest.mark.asyncio
    async def test_initialization(self, graph: KnowledgeGraphBuilder) -> None:
        """Test graph builder initialization."""
        assert graph.entities == {}
        assert graph.edges == []

    @pytest.mark.asyncio
    async def test_extract_system_entity(self, graph: KnowledgeGraphBuilder) -> None:
        """Test extracting system entity from conversation."""
        conversation = {
            "system_id": "session-buddy-1",
            "content": "Test conversation",
        }

        entities = await graph.extract_entities(conversation)

        assert len(entities) == 1
        assert entities[0].entity_id == "system:session-buddy-1"
        assert entities[0].entity_type == "system"
        assert entities[0].properties == {"name": "session-buddy-1"}
        assert entities[0].source_system == "session-buddy-1"

    @pytest.mark.asyncio
    async def test_extract_user_entity(self, graph: KnowledgeGraphBuilder) -> None:
        """Test extracting user entity from conversation."""
        conversation = {
            "system_id": "system-1",
            "content": "Test",
            "metadata": {"user_id": "alice"},
        }

        entities = await graph.extract_entities(conversation)

        assert len(entities) == 2  # system + user
        user_entity = next(e for e in entities if e.entity_type == "user")
        assert user_entity.entity_id == "user:alice"
        assert user_entity.properties == {"user_id": "alice"}

    @pytest.mark.asyncio
    async def test_extract_project_entity(self, graph: KnowledgeGraphBuilder) -> None:
        """Test extracting project entity from conversation."""
        conversation = {
            "system_id": "system-1",
            "content": "Test",
            "metadata": {"project": "mahavishnu"},
        }

        entities = await graph.extract_entities(conversation)

        assert len(entities) == 2  # system + project
        project_entity = next(e for e in entities if e.entity_type == "project")
        assert project_entity.entity_id == "project:mahavishnu"
        assert project_entity.properties == {"name": "mahavishnu"}

    @pytest.mark.asyncio
    async def test_extract_multiple_entities(self, graph: KnowledgeGraphBuilder) -> None:
        """Test extracting all entity types."""
        conversation = {
            "system_id": "system-1",
            "content": "Discussion about mahavishnu project",
            "metadata": {
                "user_id": "alice",
                "project": "mahavishnu",
            },
        }

        entities = await graph.extract_entities(conversation)

        assert len(entities) == 3
        entity_types = {e.entity_type for e in entities}
        assert entity_types == {"system", "user", "project"}

    @pytest.mark.asyncio
    async def test_extract_with_missing_metadata(self, graph: KnowledgeGraphBuilder) -> None:
        """Test extraction with missing metadata."""
        conversation = {
            "system_id": "system-1",
            "content": "Test",
        }

        entities = await graph.extract_entities(conversation)

        assert len(entities) == 1
        assert entities[0].entity_type == "system"

    @pytest.mark.asyncio
    async def test_extract_with_missing_system_id(self, graph: KnowledgeGraphBuilder) -> None:
        """Test extraction with missing system_id."""
        conversation = {
            "content": "Test",
            "metadata": {"user_id": "alice"},
        }

        entities = await graph.extract_entities(conversation)

        assert len(entities) == 1
        assert entities[0].entity_type == "user"
        assert entities[0].source_system == "unknown"

    @pytest.mark.asyncio
    async def test_extract_user_project_relationship(
        self, graph: KnowledgeGraphBuilder
    ) -> None:
        """Test extracting user-worked_on-project relationship."""
        conversation = {
            "system_id": "system-1",
            "content": "Test",
            "metadata": {
                "user_id": "alice",
                "project": "mahavishnu",
            },
        }

        entities = await graph.extract_entities(conversation)
        edges = await graph.extract_relationships(conversation, entities)

        assert len(edges) == 2  # user-worked_on-project, system-contains-project

        # Check user-worked_on-project edge
        user_worked_edge = next(
            (e for e in edges if e.edge_type == "worked_on"), None
        )
        assert user_worked_edge is not None
        assert user_worked_edge.source_id == "user:alice"
        assert user_worked_edge.target_id == "project:mahavishnu"

    @pytest.mark.asyncio
    async def test_extract_system_contains_relationship(
        self, graph: KnowledgeGraphBuilder
    ) -> None:
        """Test extracting system-contains-project relationship."""
        conversation = {
            "system_id": "system-1",
            "content": "Test",
            "metadata": {"project": "mahavishnu"},
        }

        entities = await graph.extract_entities(conversation)
        edges = await graph.extract_relationships(conversation, entities)

        assert len(edges) == 1

        contains_edge = edges[0]
        assert contains_edge.edge_type == "contains"
        assert contains_edge.source_id == "system:system-1"
        assert contains_edge.target_id == "project:mahavishnu"

    @pytest.mark.asyncio
    async def test_extract_multiple_users_multiple_projects(
        self, graph: KnowledgeGraphBuilder
    ) -> None:
        """Test extracting multiple users and projects."""
        entities = [
            GraphEntity(entity_id="user:alice", entity_type="user"),
            GraphEntity(entity_id="user:bob", entity_type="user"),
            GraphEntity(entity_id="project:A", entity_type="project"),
            GraphEntity(entity_id="project:B", entity_type="project"),
        ]

        conversation = {"system_id": "system-1", "content": "Test"}
        edges = await graph.extract_relationships(conversation, entities)

        # Should have 2 users × 2 projects = 4 worked_on edges
        # plus 2 system-contains edges = 6 total
        worked_on_edges = [e for e in edges if e.edge_type == "worked_on"]
        assert len(worked_on_edges) == 4

    @pytest.mark.asyncio
    async def test_add_to_graph_new_entities(self, graph: KnowledgeGraphBuilder) -> None:
        """Test adding new entities to graph."""
        entities = [
            GraphEntity(entity_id="user:alice", entity_type="user"),
            GraphEntity(entity_id="project:test", entity_type="project"),
        ]
        edges = [
            GraphEdge(
                source_id="user:alice",
                target_id="project:test",
                edge_type="worked_on",
            )
        ]

        await graph.add_to_graph(entities, edges)

        assert len(graph.entities) == 2
        assert len(graph.edges) == 1
        assert "user:alice" in graph.entities
        assert "project:test" in graph.entities

    @pytest.mark.asyncio
    async def test_add_to_graph_duplicate_entities(self, graph: KnowledgeGraphBuilder) -> None:
        """Test that duplicate entities are not added."""
        entity = GraphEntity(entity_id="user:alice", entity_type="user")

        await graph.add_to_graph([entity], [])
        await graph.add_to_graph([entity], [])

        assert len(graph.entities) == 1
        assert graph.entities["user:alice"] == entity

    @pytest.mark.asyncio
    async def test_add_to_graph_duplicate_edges(self, graph: KnowledgeGraphBuilder) -> None:
        """Test that duplicate edges are added."""
        edge = GraphEdge(
            source_id="a",
            target_id="b",
            edge_type="related",
        )

        await graph.add_to_graph([], [edge])
        await graph.add_to_graph([], [edge])

        assert len(graph.edges) == 2  # Both edges added

    @pytest.mark.asyncio
    async def test_get_neighbors_empty(self, graph: KnowledgeGraphBuilder) -> None:
        """Test getting neighbors from empty graph."""
        neighbors = graph.get_neighbors("nonexistent")

        assert neighbors == []

    @pytest.mark.asyncio
    async def test_get_neighbors_by_edge_type(self, graph: KnowledgeGraphBuilder) -> None:
        """Test getting neighbors filtered by edge type."""
        entities = [
            GraphEntity(entity_id="user:alice", entity_type="user"),
            GraphEntity(entity_id="project:A", entity_type="project"),
            GraphEntity(entity_id="project:B", entity_type="project"),
        ]
        edges = [
            GraphEdge(
                source_id="user:alice",
                target_id="project:A",
                edge_type="worked_on",
            ),
            GraphEdge(
                source_id="user:alice",
                target_id="project:B",
                edge_type="similar_to",
            ),
        ]

        await graph.add_to_graph(entities, edges)

        # Get only worked_on neighbors
        neighbors = graph.get_neighbors("user:alice", edge_type="worked_on")

        assert len(neighbors) == 1
        assert neighbors[0]["entity_id"] == "project:A"
        assert neighbors[0]["edge_type"] == "worked_on"

    @pytest.mark.asyncio
    async def test_get_neighbors_with_limit(self, graph: KnowledgeGraphBuilder) -> None:
        """Test getting neighbors with limit."""
        entities = [
            GraphEntity(entity_id="user:alice", entity_type="user"),
        ] + [
            GraphEntity(entity_id=f"project:{i}", entity_type="project") for i in range(5)
        ]
        edges = [
            GraphEdge(
                source_id="user:alice",
                target_id=f"project:{i}",
                edge_type="worked_on",
            )
            for i in range(5)
        ]

        await graph.add_to_graph(entities, edges)

        neighbors = graph.get_neighbors("user:alice", limit=3)

        assert len(neighbors) == 3

    @pytest.mark.asyncio
    async def test_get_neighbors_bidirectional(self, graph: KnowledgeGraphBuilder) -> None:
        """Test that neighbors are found in both directions."""
        entities = [
            GraphEntity(entity_id="a", entity_type="node"),
            GraphEntity(entity_id="b", entity_type="node"),
            GraphEntity(entity_id="c", entity_type="node"),
        ]
        edges = [
            GraphEdge(source_id="a", target_id="b", edge_type="connected"),
            GraphEdge(source_id="c", target_id="a", edge_type="connected"),
        ]

        await graph.add_to_graph(entities, edges)

        neighbors_a = graph.get_neighbors("a")
        assert len(neighbors_a) == 2  # Both b and c

        neighbors_b = graph.get_neighbors("b")
        assert len(neighbors_b) == 1  # Only a

    @pytest.mark.asyncio
    async def test_find_shortest_path_trivial(self, graph: KnowledgeGraphBuilder) -> None:
        """Test finding path from node to itself."""
        entities = [GraphEntity(entity_id="a", entity_type="node")]
        await graph.add_to_graph(entities, [])

        path = graph.find_shortest_path("a", "a")

        assert path == ["a"]

    @pytest.mark.asyncio
    async def test_find_shortest_path_direct_connection(
        self, graph: KnowledgeGraphBuilder
    ) -> None:
        """Test finding direct path between connected nodes."""
        entities = [
            GraphEntity(entity_id="a", entity_type="node"),
            GraphEntity(entity_id="b", entity_type="node"),
        ]
        edges = [GraphEdge(source_id="a", target_id="b", edge_type="connected")]

        await graph.add_to_graph(entities, edges)

        path = graph.find_shortest_path("a", "b")

        assert path == ["a", "b"]

    @pytest.mark.asyncio
    async def test_find_shortest_path_two_hops(self, graph: KnowledgeGraphBuilder) -> None:
        """Test finding path with two hops."""
        entities = [
            GraphEntity(entity_id="a", entity_type="node"),
            GraphEntity(entity_id="b", entity_type="node"),
            GraphEntity(entity_id="c", entity_type="node"),
        ]
        edges = [
            GraphEdge(source_id="a", target_id="b", edge_type="connected"),
            GraphEdge(source_id="b", target_id="c", edge_type="connected"),
        ]

        await graph.add_to_graph(entities, edges)

        path = graph.find_shortest_path("a", "c")

        assert path == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_find_shortest_path_not_found(self, graph: KnowledgeGraphBuilder) -> None:
        """Test path finding when no path exists."""
        entities = [
            GraphEntity(entity_id="a", entity_type="node"),
            GraphEntity(entity_id="b", entity_type="node"),
            GraphEntity(entity_id="c", entity_type="node"),
        ]
        edges = [GraphEdge(source_id="a", target_id="b", edge_type="connected")]

        await graph.add_to_graph(entities, edges)

        path = graph.find_shortest_path("a", "c")

        assert path is None

    @pytest.mark.asyncio
    async def test_find_shortest_path_nonexistent_entities(
        self, graph: KnowledgeGraphBuilder
    ) -> None:
        """Test path finding with nonexistent entities."""
        path = graph.find_shortest_path("nonexistent1", "nonexistent2")

        assert path is None

    @pytest.mark.asyncio
    async def test_find_shortest_path_max_hops(self, graph: KnowledgeGraphBuilder) -> None:
        """Test path finding with max_hops limit."""
        entities = [
            GraphEntity(entity_id=f"node{i}", entity_type="node") for i in range(5)
        ]
        edges = [
            GraphEdge(source_id=f"node{i}", target_id=f"node{i+1}", edge_type="connected")
            for i in range(4)
        ]

        await graph.add_to_graph(entities, edges)

        # Path exists but longer than max_hops
        path = graph.find_shortest_path("node0", "node4", max_hops=2)

        assert path is None

    @pytest.mark.asyncio
    async def test_find_shortest_path_complex_graph(self, graph: KnowledgeGraphBuilder) -> None:
        """Test path finding in complex graph with multiple paths."""
        # Create diamond graph:
        #   a
        #  / \
        # b   c
        #  \ /
        #   d
        entities = [
            GraphEntity(entity_id="a", entity_type="node"),
            GraphEntity(entity_id="b", entity_type="node"),
            GraphEntity(entity_id="c", entity_type="node"),
            GraphEntity(entity_id="d", entity_type="node"),
        ]
        edges = [
            GraphEdge(source_id="a", target_id="b", edge_type="connected"),
            GraphEdge(source_id="a", target_id="c", edge_type="connected"),
            GraphEdge(source_id="b", target_id="d", edge_type="connected"),
            GraphEdge(source_id="c", target_id="d", edge_type="connected"),
        ]

        await graph.add_to_graph(entities, edges)

        path = graph.find_shortest_path("a", "d")

        # Should find one of the two paths (both length 3)
        assert path is not None
        assert len(path) == 3
        assert path[0] == "a"
        assert path[-1] == "d"

    @pytest.mark.asyncio
    async def test_get_statistics_empty_graph(self, graph: KnowledgeGraphBuilder) -> None:
        """Test getting statistics from empty graph."""
        stats = graph.get_statistics()

        assert stats["total_entities"] == 0
        assert stats["total_edges"] == 0
        assert stats["entity_types"] == {}
        assert stats["edge_types"] == {}

    @pytest.mark.asyncio
    async def test_get_statistics_populated_graph(self, graph: KnowledgeGraphBuilder) -> None:
        """Test getting statistics from populated graph."""
        entities = [
            GraphEntity(entity_id="user:alice", entity_type="user"),
            GraphEntity(entity_id="user:bob", entity_type="user"),
            GraphEntity(entity_id="project:A", entity_type="project"),
        ]
        edges = [
            GraphEdge(source_id="user:alice", target_id="project:A", edge_type="worked_on"),
            GraphEdge(source_id="user:bob", target_id="project:A", edge_type="worked_on"),
            GraphEdge(source_id="user:alice", target_id="user:bob", edge_type="similar_to"),
        ]

        await graph.add_to_graph(entities, edges)

        stats = graph.get_statistics()

        assert stats["total_entities"] == 3
        assert stats["total_edges"] == 3
        assert stats["entity_types"] == {"user": 2, "project": 1}
        assert stats["edge_types"] == {"worked_on": 2, "similar_to": 1}

    @pytest.mark.asyncio
    async def test_end_to_end_workflow(self, graph: KnowledgeGraphBuilder) -> None:
        """Test complete workflow: extract, add, query."""
        conversation = {
            "system_id": "session-buddy-1",
            "content": "Alice worked on mahavishnu project",
            "metadata": {
                "user_id": "alice",
                "project": "mahavishnu",
            },
        }

        # Extract entities and relationships
        entities = await graph.extract_entities(conversation)
        edges = await graph.extract_relationships(conversation, entities)

        # Add to graph
        await graph.add_to_graph(entities, edges)

        # Verify entities
        assert len(graph.entities) == 3
        assert "user:alice" in graph.entities
        assert "project:mahavishnu" in graph.entities
        assert "system:session-buddy-1" in graph.entities

        # Verify edges
        assert len(graph.edges) == 2

        # Query neighbors
        neighbors = graph.get_neighbors("user:alice")
        assert len(neighbors) == 1
        assert neighbors[0]["entity_id"] == "project:mahavishnu"

        # Get statistics
        stats = graph.get_statistics()
        assert stats["total_entities"] == 3
        assert stats["total_edges"] == 2
