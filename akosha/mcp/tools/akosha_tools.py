"""Akosha universal memory aggregation MCP tools.

This module provides MCP tool implementations for cross-system memory intelligence,
including semantic search, analytics, anomaly detection, and knowledge graph queries.

All tools are organized into categories and registered with the FastMCP registry.
Tools use validation schemas to ensure input safety and provide clear error messages.

Example:
    >>> from akosha.mcp.tools import register_all_tools
    >>> from fastmcp import FastMCP
    >>> app = FastMCP("my-app")
    >>> register_all_tools(app, embedding_service, analytics_service, graph_builder)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from akosha.mcp.tools.tool_registry import FastMCPToolRegistry
    from akosha.processing.analytics import TimeSeriesAnalytics
    from akosha.processing.embeddings import EmbeddingService
    from akosha.processing.knowledge_graph import KnowledgeGraphBuilder

from akosha.mcp.validation import (
    AnalyzeTrendsRequest,
    CorrelateSystemsRequest,
    DetectAnomaliesRequest,
    FindPathRequest,
    GenerateBatchEmbeddingsRequest,
    GenerateEmbeddingRequest,
    GetSystemMetricsRequest,
    QueryKnowledgeGraphRequest,
    SearchAllSystemsRequest,
    validate_request,
)
from akosha.security import require_auth


def register_akosha_tools(
    registry: FastMCPToolRegistry,
    embedding_service: EmbeddingService,
    analytics_service: TimeSeriesAnalytics,
    graph_builder: KnowledgeGraphBuilder,
) -> None:
    """Register all Akosha MCP tools.

    This is the main entry point for tool registration, orchestrating the
    registration of all tool categories (embedding, search, analytics, graph, system).

    Args:
        registry: FastMCP tool registry instance for tool registration
        embedding_service: Embedding generation service for semantic search
        analytics_service: Time-series analytics service for trend analysis
        graph_builder: Knowledge graph builder for relationship queries

    Example:
        >>> from fastmcp import FastMCP
        >>> from akosha.processing.embeddings import get_embedding_service
        >>> from akosha.processing.analytics import TimeSeriesAnalytics
        >>> from akosha.processing.knowledge_graph import KnowledgeGraphBuilder
        >>>
        >>> app = FastMCP("akosha")
        >>> register_akosha_tools(
        ...     app,
        ...     get_embedding_service(),
        ...     TimeSeriesAnalytics(),
        ...     KnowledgeGraphBuilder()
        ... )
    """
    register_embedding_tools(registry, embedding_service)
    register_search_tools(registry, embedding_service)
    register_analytics_tools(registry, analytics_service)
    register_graph_tools(registry, graph_builder)
    register_system_tools(registry)


def register_embedding_tools(
    registry: FastMCPToolRegistry,
    embedding_service: EmbeddingService,
) -> None:
    """Register embedding generation tools.

    Registers tools for generating semantic embeddings from text using
    the local AI model (all-MiniLM-L6-v2). Supports both single text
    and batch processing.

    Args:
        registry: FastMCP tool registry instance
        embedding_service: Embedding generation service

    Tools registered:
        - generate_embedding: Generate embedding for single text
        - generate_batch_embeddings: Generate embeddings for multiple texts
    """
    from akosha.mcp.tools.tool_registry import ToolCategory, ToolMetadata

    logger = logging.getLogger(__name__)

    @registry.register(
        ToolMetadata(
            name="generate_embedding",
            description="Generate semantic embedding for text using local AI model",
            category=ToolCategory.SEARCH,
            examples=[
                {
                    "text": "how to implement JWT authentication in FastAPI",
                    "description": "Generate 384-dimensional vector embedding",
                }
            ],
        )
    )
    async def generate_embedding(
        text: str,
    ) -> dict[str, Any]:
        """Generate semantic embedding for text.

        Creates a 384-dimensional vector embedding using the all-MiniLM-L6-v2
        model running locally via ONNX runtime. The embedding captures semantic
        meaning of the text for similarity search and retrieval.

        The service gracefully degrades to fallback mode if the model is
        unavailable, returning deterministic mock embeddings.

        Args:
            text: Input text to embed. Should be a meaningful phrase or sentence.
                Longer texts may be truncated internally. Empty strings will
                produce zero embeddings.

        Returns:
            dict[str, Any]: Embedding result containing:
                - text (str): Original input text
                - embedding_dim (int): Dimension of embedding vector (384)
                - embedding (list[float]): Embedding vector as list
                - mode (str): "real" if using actual model, "fallback" if using mock

        Raises:
            ValueError: If text is empty or validation fails. This is handled
                by the validate_request wrapper.

        Example:
            >>> result = await generate_embedding(
            ...     "how to implement JWT authentication in FastAPI"
            ... )
            >>> embedding = result["embedding"]
            >>> len(embedding)
            384
            >>> result["mode"]
            'real'
        """
        # Validate input
        params = validate_request(GenerateEmbeddingRequest, text=text)
        text = params.text

        logger.info(f"Generating embedding for text: {text[:50]}...")

        embedding = await embedding_service.generate_embedding(text)

        return {
            "text": text,
            "embedding_dim": len(embedding),
            "embedding": embedding.tolist(),
            "mode": "real" if embedding_service.is_available() else "fallback",
        }

    @registry.register(
        ToolMetadata(
            name="generate_batch_embeddings",
            description="Generate embeddings for multiple texts at once",
            category=ToolCategory.SEARCH,
        )
    )
    async def generate_batch_embeddings(
        texts: list[str],
        batch_size: int = 32,
    ) -> dict[str, Any]:
        """Generate embeddings for multiple texts.

        Efficiently processes multiple texts in batches for better throughput.
        Uses the same all-MiniLM-L6-v2 model with vectorized operations
        for improved performance.

        Args:
            texts: List of input texts to embed. Each text will be processed
                independently. Empty list returns empty results.
            batch_size: Number of texts to process in each batch. Default is 32.
                Larger batches may be more efficient but use more memory.
                Recommended range: 8-128.

        Returns:
            dict[str, Any]: Batch embedding result containing:
                - count (int): Number of embeddings generated
                - embedding_dim (int): Dimension of each embedding (384)
                - embeddings (list[list[float]]): List of embedding vectors
                - mode (str): "real" or "fallback" mode indicator

        Raises:
            ValueError: If texts is empty or batch_size is invalid.

        Example:
            >>> result = await generate_batch_embeddings(
            ...     texts=["hello world", "goodbye moon"],
            ...     batch_size=16
            ... )
            >>> result["count"]
            2
            >>> len(result["embeddings"][0])
            384
        """
        # Validate input
        params = validate_request(
            GenerateBatchEmbeddingsRequest, texts=texts, batch_size=batch_size
        )
        texts = params.texts
        batch_size = params.batch_size

        logger.info(f"Generating batch embeddings for {len(texts)} texts")

        embeddings = await embedding_service.generate_batch_embeddings(
            texts=texts,
            batch_size=batch_size,
        )

        return {
            "count": len(embeddings),
            "embedding_dim": len(embeddings[0]) if embeddings else 0,
            "embeddings": [emb.tolist() for emb in embeddings],
            "mode": "real" if embedding_service.is_available() else "fallback",
        }


def register_search_tools(
    registry: FastMCPToolRegistry,
    embedding_service: EmbeddingService,
) -> None:
    """Register cross-system search tools.

    Registers tools for searching across all system memories using semantic
    similarity. Search functionality will be enhanced once the hot store
    is fully implemented.

    Args:
        registry: FastMCP tool registry instance
        embedding_service: Embedding generation service for query encoding

    Tools registered:
        - search_all_systems: Semantic search across all system memories
    """
    from akosha.mcp.tools.tool_registry import ToolCategory, ToolMetadata

    logger = logging.getLogger(__name__)

    @registry.register(
        ToolMetadata(
            name="search_all_systems",
            description="Search across all system memories using semantic similarity",
            category=ToolCategory.SEARCH,
            examples=[
                {
                    "query": "how to implement JWT authentication",
                    "limit": 10,
                    "description": "Find conversations about JWT auth across all systems",
                }
            ],
        )
    )
    @require_auth
    async def search_all_systems(
        query: str,
        limit: int = 10,
        threshold: float = 0.7,
        system_id: str | None = None,
    ) -> dict[str, Any]:
        """Search across all system memories.

        Performs semantic search across all ingested system memories to find
        conversations similar to the query. Uses vector embeddings to match
        meaning rather than just keywords.

        Note: Currently returns mock results until the hot store is fully
        implemented. The query embedding is generated but not yet used
        for actual retrieval.

        Args:
            query: Search query text. Natural language queries work best.
                Example: "how to implement JWT authentication"
            limit: Maximum number of results to return. Default is 10.
                Higher values may impact performance. Recommended range: 1-100.
            threshold: Minimum similarity score for results (0-1). Default is 0.7.
                Higher thresholds return only more similar results. Not yet used.
            system_id: Optional filter to search only a specific system.
                If None, searches across all systems. Not yet used.

        Returns:
            dict[str, Any]: Search results containing:
                - query (str): Original search query
                - total_results (int): Number of results found
                - results (list[dict]): List of result objects, each containing:
                    - system_id (str): Source system identifier
                    - conversation_id (str): Conversation identifier
                    - content (str): Relevant content snippet
                    - similarity (float): Semantic similarity score (0-1)
                    - timestamp (str): ISO timestamp of conversation
                - mode (str): "real" or "fallback" embedding mode

        Raises:
            ValueError: If query is empty or validation fails.
            PermissionError: If authentication fails (via @require_auth).

        Example:
            >>> result = await search_all_systems(
            ...     query="JWT authentication best practices",
            ...     limit=5,
            ...     threshold=0.8
            ... )
            >>> result["total_results"]
            5
            >>> top_result = result["results"][0]
            >>> top_result["similarity"]
            0.92
        """
        # Validate input
        params = validate_request(
            SearchAllSystemsRequest,
            query=query,
            limit=limit,
            threshold=threshold,
            system_id=system_id,
        )
        query = params.query
        limit = params.limit
        threshold = params.threshold
        system_id = params.system_id

        logger.info(f"Searching all systems: query='{query}', limit={limit}")

        # Generate query embedding (not used until hot store is implemented)
        _ = await embedding_service.generate_embedding(query)

        # TODO: Search hot store when implemented
        # For now, return mock results
        results = [
            {
                "system_id": system_id or "system-1",
                "conversation_id": "conv-1",
                "content": f"Mock result for: {query}",
                "similarity": 0.85,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        ]

        return {
            "query": query,
            "total_results": len(results),
            "results": results,
            "mode": "real" if embedding_service.is_available() else "fallback",
        }


def register_analytics_tools(
    registry: FastMCPToolRegistry,
    analytics_service: TimeSeriesAnalytics,
) -> None:
    """Register analytics tools.

    Registers tools for time-series analytics including trend analysis,
    anomaly detection, and cross-system correlation analysis.

    Args:
        registry: FastMCP tool registry instance
        analytics_service: Time-series analytics service

    Tools registered:
        - get_system_metrics: Get available metrics and statistics
        - analyze_trends: Analyze trends for a metric over time
        - detect_anomalies: Detect statistical anomalies in metrics
        - correlate_systems: Analyze correlations between systems
    """
    from akosha.mcp.tools.tool_registry import ToolCategory, ToolMetadata

    logger = logging.getLogger(__name__)

    @registry.register(
        ToolMetadata(
            name="get_system_metrics",
            description="Get metrics and statistics for all systems",
            category=ToolCategory.ANALYTICS,
        )
    )
    @require_auth
    async def get_system_metrics(
        time_range_days: int = 30,
    ) -> dict[str, Any]:
        """Get system metrics.

        Retrieves a list of all available metrics being tracked across systems,
        along with summary statistics for the specified time range.

        Args:
            time_range_days: Time range in days to look back for metrics.
                Default is 30 days. Determines which metrics are considered
                "active" based on recent data.

        Returns:
            dict[str, Any]: System metrics containing:
                - time_range_days (int): Requested time range
                - total_metrics (int): Number of available metrics
                - metric_names (list[str]): List of metric names being tracked

        Raises:
            ValueError: If time_range_days is invalid.

        Example:
            >>> metrics = await get_system_metrics(time_range_days=7)
            >>> metrics["total_metrics"]
            15
            >>> metrics["metric_names"][:3]
            ['conversation_count', 'quality_score', 'error_rate']
        """
        # Validate input
        params = validate_request(GetSystemMetricsRequest, time_range_days=time_range_days)
        time_range_days = params.time_range_days

        logger.info(f"Getting system metrics: range={time_range_days} days")

        metric_names = analytics_service.get_metric_names()

        return {
            "time_range_days": time_range_days,
            "total_metrics": len(metric_names),
            "metric_names": metric_names,
        }

    @registry.register(
        ToolMetadata(
            name="analyze_trends",
            description="Analyze trends across systems over time",
            category=ToolCategory.ANALYTICS,
            examples=[
                {
                    "metric_name": "conversation_count",
                    "system_id": "system-1",
                    "time_window_days": 7,
                    "description": "Check if conversation count is increasing",
                }
            ],
        )
    )
    @require_auth
    async def analyze_trends(
        metric_name: str,
        system_id: str | None = None,
        time_window_days: int = 7,
    ) -> dict[str, Any]:
        """Analyze trends for a metric.

        Performs linear regression analysis on metric data to detect trends
        (increasing, decreasing, or stable) over the specified time window.
        Provides statistical measures of trend strength and confidence.

        Args:
            metric_name: Name of the metric to analyze. Must be a metric
                that has been tracked via add_metric. Examples include
                "conversation_count", "quality_score", "error_rate".
            system_id: Optional filter to analyze only a specific system.
                If None, aggregates data across all systems.
            time_window_days: Time window for analysis in days. Default is 7.
                Larger windows provide more confidence but may smooth out
                recent changes. Recommended range: 3-90 days.

        Returns:
            dict[str, Any]: Trend analysis results containing:
                - metric_name (str): Analyzed metric name
                - trend_direction (str): "increasing", "decreasing", or "stable"
                - trend_strength (float): R-squared value (0-1), higher = stronger trend
                - percent_change (float): Percent change from start to end of window
                - confidence (float): Confidence score (0-1) based on data point count
                - time_range (tuple[str, str]): Start and end ISO timestamps
                - system_id (str | None): System filter used

            Or if insufficient data:
                - metric_name (str): Requested metric name
                - error (str): "Insufficient data for trend analysis"
                - system_id (str | None): System filter used
                - time_window_days (int): Requested time window

        Raises:
            ValueError: If metric_name is empty or parameters are invalid.
            PermissionError: If authentication fails.

        Example:
            >>> result = await analyze_trends(
            ...     metric_name="conversation_count",
            ...     system_id="system-1",
            ...     time_window_days=7
            ... )
            >>> result["trend_direction"]
            'increasing'
            >>> result["trend_strength"]
            0.87
            >>> result["percent_change"]
            23.5
        """
        # Validate input
        params = validate_request(
            AnalyzeTrendsRequest,
            metric_name=metric_name,
            system_id=system_id,
            time_window_days=time_window_days,
        )
        metric_name = params.metric_name
        system_id = params.system_id
        time_window_days = params.time_window_days

        logger.info(
            f"Analyzing trends: metric={metric_name}, "
            f"system={system_id}, window={time_window_days} days"
        )

        trend = await analytics_service.analyze_trend(
            metric_name=metric_name,
            system_id=system_id,
            time_window=timedelta(days=time_window_days),
        )

        if trend is None:
            return {
                "metric_name": metric_name,
                "error": "Insufficient data for trend analysis",
                "system_id": system_id,
                "time_window_days": time_window_days,
            }

        return {
            "metric_name": trend.metric_name,
            "trend_direction": trend.trend_direction,
            "trend_strength": trend.trend_strength,
            "percent_change": trend.percent_change,
            "confidence": trend.confidence,
            "time_range": (
                trend.time_range[0].isoformat(),
                trend.time_range[1].isoformat(),
            ),
            "system_id": system_id,
        }

    @registry.register(
        ToolMetadata(
            name="detect_anomalies",
            description="Detect statistical anomalies in system metrics",
            category=ToolCategory.ANALYTICS,
            examples=[
                {
                    "metric_name": "error_rate",
                    "threshold_std": 3.0,
                    "description": "Find unusual error rate spikes",
                }
            ],
        )
    )
    @require_auth
    async def detect_anomalies(
        metric_name: str,
        system_id: str | None = None,
        time_window_days: int = 7,
        threshold_std: float = 3.0,
    ) -> dict[str, Any]:
        """Detect anomalies in metrics.

        Uses statistical analysis (Z-score) to detect data points that deviate
        significantly from the norm. Anomalies are values that fall beyond
        the specified number of standard deviations from the mean.

        Args:
            metric_name: Name of the metric to analyze. Must be a tracked metric.
            system_id: Optional filter to analyze only a specific system.
                If None, analyzes data across all systems.
            time_window_days: Time window for analysis in days. Default is 7.
                Larger windows provide better baseline statistics but may
                miss short-term anomalies.
            threshold_std: Standard deviation threshold for anomaly detection.
                Default is 3.0 (covers 99.7% of normal distribution).
                Lower values detect more anomalies, higher values detect only
                extreme outliers. Recommended range: 2.0-4.0.

        Returns:
            dict[str, Any]: Anomaly detection results containing:
                - metric_name (str): Analyzed metric name
                - anomaly_count (int): Number of anomalies detected
                - total_points (int): Total data points analyzed
                - anomaly_rate (float): Percentage of points that are anomalies
                - threshold (float): Z-score threshold used
                - anomalies (list[dict]): Up to 10 anomalies, each with:
                    - timestamp (str): ISO timestamp
                    - value (float): Anomalous value
                    - system_id (str): Source system
                    - z_score (float): Z-score (deviation in std)
                    - deviation (float): Absolute deviation from mean
                    - metadata (dict): Additional point metadata

            Or if insufficient data:
                - metric_name (str): Requested metric name
                - error (str): "Insufficient data for anomaly detection"
                - system_id (str | None): System filter used

        Raises:
            ValueError: If parameters are invalid.
            PermissionError: If authentication fails.

        Example:
            >>> result = await detect_anomalies(
            ...     metric_name="error_rate",
            ...     threshold_std=3.0
            ... )
            >>> result["anomaly_count"]
            2
            >>> result["anomalies"][0]["z_score"]
            4.2
        """
        # Validate input
        params = validate_request(
            DetectAnomaliesRequest,
            metric_name=metric_name,
            system_id=system_id,
            time_window_days=time_window_days,
            threshold_std=threshold_std,
        )
        metric_name = params.metric_name
        system_id = params.system_id
        time_window_days = params.time_window_days
        threshold_std = params.threshold_std

        logger.info(f"Detecting anomalies: metric={metric_name}, threshold={threshold_std} std")

        anomalies = await analytics_service.detect_anomalies(
            metric_name=metric_name,
            system_id=system_id,
            time_window=timedelta(days=time_window_days),
            threshold_std=threshold_std,
        )

        if anomalies is None:
            return {
                "metric_name": metric_name,
                "error": "Insufficient data for anomaly detection",
                "system_id": system_id,
            }

        return {
            "metric_name": anomalies.metric_name,
            "anomaly_count": anomalies.anomaly_count,
            "total_points": anomalies.total_points,
            "anomaly_rate": anomalies.anomaly_rate,
            "threshold": anomalies.threshold,
            "anomalies": anomalies.anomalies[:10],  # Limit to first 10
        }

    @registry.register(
        ToolMetadata(
            name="correlate_systems",
            description="Analyze correlations between systems for a metric",
            category=ToolCategory.ANALYTICS,
            examples=[
                {
                    "metric_name": "quality_score",
                    "time_window_days": 7,
                    "description": "Find systems with similar quality patterns",
                }
            ],
        )
    )
    @require_auth
    async def correlate_systems(
        metric_name: str,
        time_window_days: int = 7,
    ) -> dict[str, Any]:
        """Analyze cross-system correlations.

        Computes Pearson correlation coefficients between systems for a given
        metric to identify systems with similar patterns. Useful for finding
        systems that behave similarly or are affected by common factors.

        Args:
            metric_name: Name of the metric to correlate. Must be tracked by
                at least 2 systems with sufficient data.
            time_window_days: Time window for analysis in days. Default is 7.
                Longer windows provide more stable correlation estimates.

        Returns:
            dict[str, Any]: Correlation analysis results containing:
                - metric_name (str): Analyzed metric name
                - total_systems (int): Number of systems analyzed
                - significant_correlations (int): Number of strong correlations
                    (correlation coefficient > 0.5)
                - correlations (list[dict]): List of significant system pairs:
                    - system_1 (str): First system ID
                    - system_2 (str): Second system ID
                    - correlation (float): Pearson correlation (-1 to 1)
                    - strength (str): "strong" if |correlation| > 0.7,
                        "moderate" if > 0.5
                - systems (list[str]): List of all system IDs analyzed
                - time_range (tuple[str, str]): Start and end ISO timestamps

            Or if insufficient data:
                - metric_name (str): Requested metric name
                - error (str): "Insufficient data for correlation analysis"

        Raises:
            ValueError: If metric_name is invalid.
            PermissionError: If authentication fails.

        Example:
            >>> result = await correlate_systems(
            ...     metric_name="quality_score",
            ...     time_window_days=7
            ... )
            >>> result["significant_correlations"]
            3
            >>> result["correlations"][0]
            {'system_1': 'system-1', 'system_2': 'system-2',
             'correlation': 0.87, 'strength': 'strong'}
        """
        # Validate input
        params = validate_request(
            CorrelateSystemsRequest,
            metric_name=metric_name,
            time_window_days=time_window_days,
        )
        metric_name = params.metric_name
        time_window_days = params.time_window_days

        logger.info(f"Analyzing correlations: metric={metric_name}")

        correlation = await analytics_service.correlate_systems(
            metric_name=metric_name,
            time_window=timedelta(days=time_window_days),
        )

        if correlation is None:
            return {
                "metric_name": metric_name,
                "error": "Insufficient data for correlation analysis",
            }

        return {
            "metric_name": correlation.metric_name,
            "total_systems": len(correlation.systems),
            "significant_correlations": len(correlation.system_pairs),
            "correlations": correlation.system_pairs,
            "systems": correlation.systems,
            "time_range": (
                correlation.time_range[0].isoformat(),
                correlation.time_range[1].isoformat(),
            ),
        }


def register_graph_tools(
    registry: FastMCPToolRegistry,
    graph_builder: KnowledgeGraphBuilder,
) -> None:
    """Register knowledge graph tools.

    Registers tools for querying the cross-system knowledge graph to find
    entities, relationships, and connections between systems, users, projects,
    and concepts.

    Args:
        registry: FastMCP tool registry instance
        graph_builder: Knowledge graph builder service

    Tools registered:
        - query_knowledge_graph: Query entities and relationships
        - find_path: Find shortest path between entities
        - get_graph_statistics: Get graph metadata and statistics
    """
    from akosha.mcp.tools.tool_registry import ToolCategory, ToolMetadata

    logger = logging.getLogger(__name__)

    @registry.register(
        ToolMetadata(
            name="query_knowledge_graph",
            description="Query the cross-system knowledge graph",
            category=ToolCategory.GRAPH,
            examples=[
                {
                    "entity_id": "user:alice",
                    "description": "Find what Alice worked on",
                }
            ],
        )
    )
    @require_auth
    async def query_knowledge_graph(
        entity_id: str,
        edge_type: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Query knowledge graph.

        Retrieves neighbors of a given entity in the knowledge graph, showing
        related entities and the types of relationships. Useful for exploring
        connections between users, projects, systems, and concepts.

        Args:
            entity_id: Entity ID to query. Must be a fully qualified ID
                with type prefix, e.g., "user:alice", "project:myapp",
                "system:prod-1". The entity must exist in the graph.
            edge_type: Optional edge type filter to limit results to specific
                relationship types. Examples: "worked_on", "contains", "related_to".
                If None, returns all neighbor types.
            limit: Maximum number of neighbors to return. Default is 50.
                Higher values may impact performance for highly connected entities.

        Returns:
            dict[str, Any]: Query results containing:
                - entity_id (str): Queried entity ID
                - total_neighbors (int): Number of neighbors found
                - neighbors (list[dict]): List of neighboring entities:
                    - entity_id (str): Neighbor entity ID
                    - entity_type (str): Type of entity (user, project, system, etc.)
                    - edge_type (str): Relationship type
                    - weight (float): Relationship strength (0-1)
                    - properties (dict): Additional entity properties

        Raises:
            ValueError: If entity_id is empty or invalid.
            PermissionError: If authentication fails.

        Example:
            >>> result = await query_knowledge_graph(
            ...     entity_id="user:alice",
            ...     edge_type="worked_on",
            ...     limit=10
            ... )
            >>> result["total_neighbors"]
            5
            >>> result["neighbors"][0]
            {'entity_id': 'project:myapp', 'entity_type': 'project',
             'edge_type': 'worked_on', 'weight': 1.0}
        """
        # Validate input
        params = validate_request(
            QueryKnowledgeGraphRequest,
            entity_id=entity_id,
            edge_type=edge_type,
            limit=limit,
        )
        entity_id = params.entity_id
        edge_type = params.edge_type
        limit = params.limit

        logger.info(f"Querying knowledge graph: entity={entity_id}")

        neighbors = graph_builder.get_neighbors(
            entity_id=entity_id,
            edge_type=edge_type,
            limit=limit,
        )

        return {
            "entity_id": entity_id,
            "total_neighbors": len(neighbors),
            "neighbors": neighbors,
        }

    @registry.register(
        ToolMetadata(
            name="find_path",
            description="Find shortest path between entities in knowledge graph",
            category=ToolCategory.GRAPH,
        )
    )
    @require_auth
    async def find_path(
        source_id: str,
        target_id: str,
        max_hops: int = 3,
    ) -> dict[str, Any]:
        """Find shortest path between entities.

        Uses bidirectional BFS to efficiently find the shortest path between
        two entities in the knowledge graph. Useful for discovering indirect
        relationships and connections.

        Args:
            source_id: Source entity ID to start from. Must exist in graph.
                Format: "type:id" (e.g., "user:alice").
            target_id: Target entity ID to find path to. Must exist in graph.
                Format: "type:id" (e.g., "project:myapp").
            max_hops: Maximum path length to search. Default is 3.
                Shorter values are faster but may miss longer connections.
                Recommended range: 2-5 for most use cases.

        Returns:
            dict[str, Any]: Path information containing:
                - source_id (str): Source entity ID
                - target_id (str): Target entity ID
                - path_found (bool): True if path was found
                - path (list[str] | None): List of entity IDs forming the path
                    from source to target (inclusive). Only present if path_found.
                - hops (int | None): Number of hops (edges) in path.
                    Only present if path_found.

        Raises:
            ValueError: If entity IDs are invalid or max_hops is too small.
            PermissionError: If authentication fails.

        Example:
            >>> result = await find_path(
            ...     source_id="user:alice",
            ...     target_id="project:myapp",
            ...     max_hops=3
            ... )
            >>> result["path_found"]
            True
            >>> result["path"]
            ['user:alice', 'project:myapp']
            >>> result["hops"]
            1
        """
        # Validate input
        params = validate_request(
            FindPathRequest,
            source_id=source_id,
            target_id=target_id,
            max_hops=max_hops,
        )
        source_id = params.source_id
        target_id = params.target_id
        max_hops = params.max_hops

        logger.info(f"Finding path: {source_id} -> {target_id}")

        path = graph_builder.find_shortest_path(
            source_id=source_id,
            target_id=target_id,
            max_hops=max_hops,
        )

        if path is None:
            return {
                "source_id": source_id,
                "target_id": target_id,
                "path_found": False,
            }

        return {
            "source_id": source_id,
            "target_id": target_id,
            "path_found": True,
            "path": path,
            "hops": len(path) - 1,
        }

    @registry.register(
        ToolMetadata(
            name="get_graph_statistics",
            description="Get knowledge graph statistics",
            category=ToolCategory.GRAPH,
        )
    )
    @require_auth
    async def get_graph_statistics() -> dict[str, Any]:
        """Get graph statistics.

        Returns metadata about the knowledge graph including entity counts,
        edge counts, and type distributions. Useful for monitoring graph
        growth and composition.

        Returns:
            dict[str, Any]: Graph statistics containing:
                - total_entities (int): Total number of unique entities
                - total_edges (int): Total number of relationships
                - entity_types (dict[str, int]): Count of entities by type
                    (e.g., {"user": 10, "project": 5, "system": 3})
                - edge_types (dict[str, int]): Count of edges by type
                    (e.g., {"worked_on": 15, "contains": 8})

        Raises:
            PermissionError: If authentication fails.

        Example:
            >>> stats = await get_graph_statistics()
            >>> stats["total_entities"]
            42
            >>> stats["entity_types"]
            {'user': 15, 'project': 12, 'system': 10, 'concept': 5}
        """
        logger.info("Getting graph statistics")

        return graph_builder.get_statistics()


def register_system_tools(registry: FastMCPToolRegistry) -> None:
    """Register system tools.

    Registers tools for system status and health checks. These tools provide
    visibility into the state of Akosha's storage tiers and services.

    Args:
        registry: FastMCP tool registry instance

    Tools registered:
        - get_storage_status: Get status of all storage tiers
    """
    from akosha.mcp.tools.tool_registry import ToolCategory, ToolMetadata

    logger = logging.getLogger(__name__)

    @registry.register(
        ToolMetadata(
            name="get_storage_status",
            description="Get status of Akosha storage tiers",
            category=ToolCategory.SYSTEM,
        )
    )
    async def get_storage_status() -> dict[str, Any]:
        """Get storage tier status.

        Returns the current status of all three storage tiers (hot, warm, cold)
        including conversation counts and health indicators. Useful for
        monitoring and capacity planning.

        Note: Currently returns placeholder status information. Full
        implementation will connect to actual storage backends.

        Returns:
            dict[str, Any]: Storage status containing:
                - hot_store (dict): Hot tier status (0-7 days):
                    - status (str): Health status ("unknown", "healthy", "degraded")
                    - conversation_count (int): Number of conversations
                - warm_store (dict): Warm tier status (7-90 days):
                    - status (str): Health status
                    - conversation_count (int): Number of conversations
                - cold_store (dict): Cold tier status (90+ days):
                    - status (str): Health status
                    - conversation_count (int): Number of conversations

        Example:
            >>> status = await get_storage_status()
            >>> status["hot_store"]["conversation_count"]
            1250
            >>> status["warm_store"]["status"]
            'healthy'
        """
        logger.info("Getting storage status")

        # TODO: Implement actual status checks
        return {
            "hot_store": {"status": "unknown", "conversation_count": 0},
            "warm_store": {"status": "unknown", "conversation_count": 0},
            "cold_store": {"status": "unknown", "conversation_count": 0},
        }


def register_code_graph_tools(
    registry: FastMCPToolRegistry,
    hot_store: Any,
) -> None:
    """Register code graph analysis tools.

    Registers tools for analyzing indexed code graphs across repositories,
    including finding similar implementations and cross-repo function usage.

    Args:
        registry: FastMCP tool registry instance
        hot_store: HotStore instance for data access

    Tools registered:
        - list_ingested_code_graphs: List all ingested code graphs
        - get_code_graph_details: Get full code graph data
        - find_similar_repositories: Find structurally similar repos
        - get_cross_repo_function_usage: Find function usage across repos
    """
    from .code_graph_tools import register_code_graph_analysis_tools

    tool_logger = logging.getLogger(__name__)
    register_code_graph_analysis_tools(registry, hot_store)
    tool_logger.info("Registered code graph analysis tools")
