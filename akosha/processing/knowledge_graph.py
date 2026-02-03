"""Knowledge graph construction for cross-system relationships."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from akosha.observability import add_span_attributes, record_counter, record_histogram, traced

logger = logging.getLogger(__name__)


@dataclass
class GraphEntity:
    """Entity in the knowledge graph."""

    entity_id: str
    entity_type: str  # user, project, concept, error, system
    properties: dict[str, Any] = field(default_factory=dict)
    source_system: str = "unknown"


@dataclass
class GraphEdge:
    """Relationship between entities."""

    source_id: str
    target_id: str
    edge_type: str  # worked_on, fixed, related_to, similar_to, mentioned
    weight: float = 1.0
    properties: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    source_system: str = "unknown"


class KnowledgeGraphBuilder:
    """Build distributed knowledge graph from conversations.

    Extracts entities and relationships from cross-system conversations
    to enable:
    - Cross-system pattern discovery
    - Expert identification
    - Related concept finding
    - Error tracking and resolution
    """

    def __init__(self) -> None:
        """Initialize knowledge graph builder."""
        self.entities: dict[str, GraphEntity] = {}
        self.edges: list[GraphEdge] = []

    @traced("knowledge_graph_extract_entities")
    async def extract_entities(
        self,
        conversation: dict[str, Any],
    ) -> list[GraphEntity]:
        """Extract entities from conversation.

        Args:
            conversation: Conversation with content and metadata

        Returns:
            List of extracted entities
        """
        add_span_attributes(
            {
                "kg.system_id": conversation.get("system_id", "unknown"),
            }
        )

        entities = []

        # Extract system entity
        system_id = conversation.get("system_id")
        if system_id:
            entities.append(
                GraphEntity(
                    entity_id=f"system:{system_id}",
                    entity_type="system",
                    properties={"name": system_id},
                    source_system=system_id,
                )
            )

        # Extract user entities from metadata
        metadata = conversation.get("metadata", {})
        user_id = metadata.get("user_id")
        if user_id:
            entities.append(
                GraphEntity(
                    entity_id=f"user:{user_id}",
                    entity_type="user",
                    properties={"user_id": user_id},
                    source_system=system_id or "unknown",
                )
            )

        # Extract project/entity
        project = metadata.get("project")
        if project:
            entities.append(
                GraphEntity(
                    entity_id=f"project:{project}",
                    entity_type="project",
                    properties={"name": project},
                    source_system=system_id or "unknown",
                )
            )

        record_histogram("kg.entities.extracted", len(entities))
        record_counter("kg.extract_entities.calls", 1)

        return entities

    @traced("knowledge_graph_extract_relationships")
    async def extract_relationships(
        self,
        conversation: dict[str, Any],
        entities: list[GraphEntity],
    ) -> list[GraphEdge]:
        """Extract relationships between entities.

        Args:
            conversation: Conversation data
            entities: Extracted entities

        Returns:
            List of relationships
        """
        edges = []
        system_id = conversation.get("system_id", "unknown")

        add_span_attributes(
            {
                "kg.entity_count": str(len(entities)),
            }
        )

        # User worked on project
        user_entities = [e for e in entities if e.entity_type == "user"]
        project_entities = [e for e in entities if e.entity_type == "project"]

        for user in user_entities:
            for project in project_entities:
                edges.append(
                    GraphEdge(
                        source_id=user.entity_id,
                        target_id=project.entity_id,
                        edge_type="worked_on",
                        weight=1.0,
                        source_system=system_id,
                    )
                )

        # System contains project
        for project in project_entities:
            edges.append(
                GraphEdge(
                    source_id=f"system:{system_id}",
                    target_id=project.entity_id,
                    edge_type="contains",
                    weight=1.0,
                    source_system=system_id,
                )
            )

        record_histogram("kg.relationships.extracted", len(edges))
        record_counter("kg.extract_relationships.calls", 1)

        return edges

    @traced("knowledge_graph_add_to_graph")
    async def add_to_graph(
        self,
        entities: list[GraphEntity],
        edges: list[GraphEdge],
    ) -> None:
        """Add entities and edges to knowledge graph.

        Args:
            entities: Entities to add
            edges: Edges to add
        """
        add_span_attributes(
            {
                "kg.entities_to_add": str(len(entities)),
                "kg.edges_to_add": str(len(edges)),
            }
        )

        # Add entities
        new_entities = 0
        for entity in entities:
            if entity.entity_id not in self.entities:
                self.entities[entity.entity_id] = entity
                new_entities += 1

        # Add edges
        self.edges.extend(edges)

        record_histogram("kg.entities.total", len(self.entities))
        record_histogram("kg.edges.total", len(self.edges))
        record_counter("kg.entities.added", new_entities)
        record_counter("kg.edges.added", len(edges))

        logger.debug(f"Added {len(entities)} entities and {len(edges)} edges to graph")

    def get_neighbors(
        self,
        entity_id: str,
        edge_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get neighbors of an entity in the graph.

        Args:
            entity_id: Entity ID
            edge_type: Optional edge type filter
            limit: Maximum neighbors to return

        Returns:
            List of neighboring entities with relationship info
        """
        add_span_attributes(
            {
                "kg.entity_id": entity_id,
                "kg.edge_type": edge_type or "all",
                "kg.limit": str(limit),
            }
        )

        neighbors = []

        for edge in self.edges:
            if edge.source_id == entity_id and (edge_type is None or edge.edge_type == edge_type):
                target_entity = self.entities.get(edge.target_id)
                if target_entity:
                    neighbors.append(
                        {
                            "entity_id": edge.target_id,
                            "entity_type": target_entity.entity_type,
                            "edge_type": edge.edge_type,
                            "weight": edge.weight,
                            "properties": target_entity.properties,
                        }
                    )

            if edge.target_id == entity_id and (edge_type is None or edge.edge_type == edge_type):
                source_entity = self.entities.get(edge.source_id)
                if source_entity:
                    neighbors.append(
                        {
                            "entity_id": edge.source_id,
                            "entity_type": source_entity.entity_type,
                            "edge_type": edge.edge_type,
                            "weight": edge.weight,
                            "properties": source_entity.properties,
                        }
                    )

        result = neighbors[:limit]

        record_histogram("kg.neighbors.found", len(result))
        record_counter("kg.get_neighbors.calls", 1)

        return result

    def find_shortest_path(
        self,
        source_id: str,
        target_id: str,
        max_hops: int = 3,
    ) -> list[str] | None:
        """Find shortest path between two entities using bidirectional BFS.

        Bidirectional BFS provides O(b^(d/2)) complexity vs O(b^d) for unidirectional,
        yielding 1000-10000x speedup for large graphs with branching factor b and depth d.

        Args:
            source_id: Source entity ID
            target_id: Target entity ID
            max_hops: Maximum path length

        Returns:
            List of entity IDs in path, or None if no path found
        """
        add_span_attributes(
            {
                "kg.source_id": source_id,
                "kg.target_id": target_id,
                "kg.max_hops": str(max_hops),
                "kg.algorithm": "bidirectional_bfs",
            }
        )

        if source_id not in self.entities or target_id not in self.entities:
            record_counter("kg.shortest_path.failed", 1, {"reason": "entity_not_found"})
            return None

        # Handle trivial case
        if source_id == target_id:
            record_histogram("kg.shortest_path.length", 1)
            record_counter("kg.shortest_path.found", 1)
            return [source_id]

        from collections import deque

        # Forward queue from source
        forward_queue = deque([source_id])
        forward_visited: dict[str, str | None] = {source_id: None}  # Maps node to parent

        # Backward queue from target
        backward_queue = deque([target_id])
        backward_visited: dict[str, str | None] = {target_id: None}  # Maps node to parent

        # Bidirectional BFS: expand both frontiers alternately
        while forward_queue and backward_queue:
            # Expand forward frontier
            for _ in range(len(forward_queue)):
                current = forward_queue.popleft()

                # Check if meeting point reached
                if current in backward_visited:
                    path = self._reconstruct_path(
                        source_id, target_id, current, forward_visited, backward_visited
                    )
                    if path and len(path) <= max_hops:
                        record_histogram("kg.shortest_path.length", len(path))
                        record_counter("kg.shortest_path.found", 1)
                        return path

                # Get neighbors and expand
                neighbors = self._get_neighbors(current)
                for neighbor in neighbors:
                    if neighbor not in forward_visited:
                        forward_visited[neighbor] = current
                        forward_queue.append(neighbor)

            # Expand backward frontier
            for _ in range(len(backward_queue)):
                current = backward_queue.popleft()

                # Check if meeting point reached
                if current in forward_visited:
                    path = self._reconstruct_path(
                        source_id, target_id, current, forward_visited, backward_visited
                    )
                    if path and len(path) <= max_hops:
                        record_histogram("kg.shortest_path.length", len(path))
                        record_counter("kg.shortest_path.found", 1)
                        return path

                # Get neighbors and expand (reverse direction)
                neighbors = self._get_neighbors(current)
                for neighbor in neighbors:
                    if neighbor not in backward_visited:
                        backward_visited[neighbor] = current
                        backward_queue.append(neighbor)

        record_counter("kg.shortest_path.not_found", 1)
        return None

    def _get_neighbors(self, entity_id: str) -> list[str]:
        """Get all neighbor entity IDs for a given entity.

        Args:
            entity_id: Entity ID to get neighbors for

        Returns:
            List of neighbor entity IDs
        """
        neighbors = []

        for edge in self.edges:
            if edge.source_id == entity_id:
                neighbors.append(edge.target_id)
            elif edge.target_id == entity_id:
                neighbors.append(edge.source_id)

        return neighbors

    def _reconstruct_path(
        self,
        source_id: str,
        target_id: str,
        meeting_point: str,
        forward_visited: dict[str, str | None],
        backward_visited: dict[str, str | None],
    ) -> list[str] | None:
        """Reconstruct path from source to target through meeting point.

        Args:
            source_id: Source entity ID
            target_id: Target entity ID
            meeting_point: Entity where forward and backward searches met
            forward_visited: Forward BFS parent mappings
            backward_visited: Backward BFS parent mappings

        Returns:
            List of entity IDs forming the path, or None if reconstruction fails
        """
        # Reconstruct forward path (source -> meeting_point)
        forward_path = []
        current = meeting_point
        while current is not None:
            forward_path.append(current)
            current = forward_visited.get(current)
        forward_path.reverse()

        # Reconstruct backward path (meeting_point -> target)
        backward_path = []
        current = backward_visited.get(meeting_point)
        while current is not None:
            backward_path.append(current)
            current = backward_visited.get(current)

        # Combine paths (excluding duplicate meeting_point)
        full_path = forward_path + backward_path

        # Validate path
        if not full_path or full_path[0] != source_id or full_path[-1] != target_id:
            return None

        return full_path

    def get_statistics(self) -> dict[str, Any]:
        """Get graph statistics.

        Returns:
            Dictionary with graph metrics
        """
        entity_types: dict[str, int] = {}
        for entity in self.entities.values():
            entity_types[entity.entity_type] = entity_types.get(entity.entity_type, 0) + 1

        edge_types: dict[str, int] = {}
        for edge in self.edges:
            edge_types[edge.edge_type] = edge_types.get(edge.edge_type, 0) + 1

        return {
            "total_entities": len(self.entities),
            "total_edges": len(self.edges),
            "entity_types": entity_types,
            "edge_types": edge_types,
        }
