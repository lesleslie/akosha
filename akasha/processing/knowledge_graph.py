"""Knowledge graph construction for cross-system relationships."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

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
        entities = []

        # Extract system entity
        system_id = conversation.get("system_id")
        if system_id:
            entities.append(GraphEntity(
                entity_id=f"system:{system_id}",
                entity_type="system",
                properties={"name": system_id},
                source_system=system_id,
            ))

        # Extract user entities from metadata
        metadata = conversation.get("metadata", {})
        user_id = metadata.get("user_id")
        if user_id:
            entities.append(GraphEntity(
                entity_id=f"user:{user_id}",
                entity_type="user",
                properties={"user_id": user_id},
                source_system=system_id or "unknown",
            ))

        # Extract project/entity
        project = metadata.get("project")
        if project:
            entities.append(GraphEntity(
                entity_id=f"project:{project}",
                entity_type="project",
                properties={"name": project},
                source_system=system_id or "unknown",
            ))

        return entities

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

        # User worked on project
        user_entities = [e for e in entities if e.entity_type == "user"]
        project_entities = [e for e in entities if e.entity_type == "project"]

        for user in user_entities:
            for project in project_entities:
                edges.append(GraphEdge(
                    source_id=user.entity_id,
                    target_id=project.entity_id,
                    edge_type="worked_on",
                    weight=1.0,
                    source_system=system_id,
                ))

        # System contains project
        for project in project_entities:
            edges.append(GraphEdge(
                source_id=f"system:{system_id}",
                target_id=project.entity_id,
                edge_type="contains",
                weight=1.0,
                source_system=system_id,
            ))

        return edges

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
        # Add entities
        for entity in entities:
            if entity.entity_id not in self.entities:
                self.entities[entity.entity_id] = entity

        # Add edges
        self.edges.extend(edges)

        logger.debug(
            f"Added {len(entities)} entities and {len(edges)} edges to graph"
        )

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
        neighbors = []

        for edge in self.edges:
            if edge.source_id == entity_id:
                if edge_type is None or edge.edge_type == edge_type:
                    target_entity = self.entities.get(edge.target_id)
                    if target_entity:
                        neighbors.append({
                            "entity_id": edge.target_id,
                            "entity_type": target_entity.entity_type,
                            "edge_type": edge.edge_type,
                            "weight": edge.weight,
                            "properties": target_entity.properties,
                        })

            if edge.target_id == entity_id:
                if edge_type is None or edge.edge_type == edge_type:
                    source_entity = self.entities.get(edge.source_id)
                    if source_entity:
                        neighbors.append({
                            "entity_id": edge.source_id,
                            "entity_type": source_entity.entity_type,
                            "edge_type": edge.edge_type,
                            "weight": edge.weight,
                            "properties": source_entity.properties,
                        })

        return neighbors[:limit]

    def find_shortest_path(
        self,
        source_id: str,
        target_id: str,
        max_hops: int = 3,
    ) -> list[str] | None:
        """Find shortest path between two entities.

        Args:
            source_id: Source entity ID
            target_id: Target entity ID
            max_hops: Maximum path length

        Returns:
            List of entity IDs in path, or None if no path found
        """
        if source_id not in self.entities or target_id not in self.entities:
            return None

        # BFS for shortest path
        from collections import deque

        queue = deque([(source_id, [source_id])])
        visited = {source_id}

        while queue:
            current, path = queue.popleft()

            if current == target_id:
                return path

            if len(path) >= max_hops:
                continue

            # Get neighbors
            for edge in self.edges:
                neighbor = None
                if edge.source_id == current and edge.target_id not in visited:
                    neighbor = edge.target_id
                elif edge.target_id == current and edge.source_id not in visited:
                    neighbor = edge.source_id

                if neighbor:
                    visited.add(neighbor)
                    queue.append((neighbor, path + [neighbor]))

        return None

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
