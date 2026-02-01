# Akosha Phase 2: Advanced Features

**Status**: Ready to Implement
**Duration**: Weeks 5-8 (4 weeks)
**Focus**: Vector indexing, time-series analytics, advanced graph operations

______________________________________________________________________

## Overview

Phase 1 established the foundation (storage, ingestion, basic knowledge graph). Phase 2 adds advanced analytics capabilities:

1. **Production-Grade Vector Search** (with real embeddings)
1. **Time-Series Analytics** (trends, anomalies, correlations)
1. **Advanced Knowledge Graph** (community detection, centrality)
1. **Real-Time Ingestion** (from S3/R2 event triggers)

______________________________________________________________________

## Week 5: Vector Search with Embeddings

### Task 5.1: Embedding Service

**File**: `akosha/processing/embeddings.py`

**Purpose**: Generate embeddings for search using local ONNX model

```python
"""Embedding generation service."""

from __future__ = annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class EmbeddingService:
    """Generate embeddings using local ONNX all-MiniLM-L6-v2.

    Benefits:
    - No external API calls (privacy-first)
    - Fast inference (< 50ms per text)
    - 384-dimensional vectors (good quality/speed tradeoff)
    """

    def __init__(self) -> None:
        """Initialize embedding service."""
        self._model = None
        self._tokenizer = None

    async def initialize(self) -> None:
        """Load ONNX model and tokenizer."""
        loop = asyncio.get_event_loop()

        # Load in executor to avoid blocking
        self._model, self._tokenizer = await loop.run_in_executor(
            None,
            self._load_model,
        )
        logger.info("Embedding service initialized")

    def _load_model(self) -> tuple:
        """Load ONNX model (blocking, run in executor).

        Returns:
            Tuple of (model, tokenizer)
        """
        try:
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer('all-MiniLM-L6-v2')
            return model, model.tokenizer
        except ImportError:
            logger.warning("sentence-transformers not available, using fallback")
            return None, None

    async def generate_embedding(
        self,
        text: str,
    ) -> np.ndarray | None:
        """Generate embedding for text.

        Args:
            text: Text to embed

        Returns:
            Embedding vector (FLOAT[384]) or None if unavailable
        """
        if not self._model:
            return None

        loop = asyncio.get_event_loop()
        embedding = await loop.run_in_executor(
            None,
            self._model.encode,
            text,
        )

        return embedding

    async def generate_batch_embeddings(
        self,
        texts: list[str],
    ) -> list[np.ndarray]:
        """Generate embeddings for multiple texts (batch processing).

        Args:
            texts: List of texts to embed

        Returns:
            List of embedding vectors
        """
        loop = asyncio.get_event_loop()
        embeddings = await loop.run_in_executor(
            None,
            lambda: self._model.encode(texts),
        )

        return embeddings
```

### Task 5.2: Enhanced Vector Search

**Update**: `akosha/storage/hot_store.py`

**Add**: Cosine similarity without HNSW (fallback)

```python
async def search_similar_fallback(
    self,
    query_embedding: list[float],
    system_id: str | None = None,
    limit: int = 10,
    threshold: float = 0.7,
) -> list[dict[str, Any]]:
    """Vector search with fallback for when HNSW is unavailable.

    Uses SQL cosine similarity when HNSW index not available.
    """
    async with self._lock:
        if not self.conn:
            raise RuntimeError("Hot store not initialized")

        # Build query
        where_clause = f"WHERE system_id = '{system_id}'" if system_id else ""
        query = f"""
            SELECT
                system_id,
                conversation_id,
                content,
                timestamp,
                metadata,
                array_cosine_similarity(embedding, ?::FLOAT[384]) as similarity
            FROM conversations
            {where_clause}
            ORDER BY similarity DESC
            LIMIT ?
        """

        results = self.conn.execute(
            query,
            [query_embedding, limit * 10]  # Get extra for filtering
        ).fetchall()

        # Filter by threshold
        return [
            {
                "system_id": r[0],
                "conversation_id": r[1],
                "content": r[2],
                "timestamp": r[3],
                "metadata": r[4],
                "similarity": r[5],
            }
            for r in results
            if r[5] >= threshold
        ][:limit]
```

______________________________________________________________________

## Week 6: Time-Series Analytics

### Task 6.1: Metrics Storage

**File**: `akosha/processing/time_series.py`

```python
"""Time-series analytics and trend detection."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class TimeSeriesAggregator:
    """Aggregate metrics across systems over time.

    Features:
    - Hourly/daily/monthly rollups
    - Trend detection (up/down/flat)
    - Anomaly detection (statistical outliers)
    - Cross-system correlation
    """

    def __init__(self) -> None:
        """Initialize time-series aggregator."""
        self._metrics: dict[str, list[tuple[datetime, float]]] = defaultdict(list)

    async def record_metric(
        self,
        system_id: str,
        metric_name: str,
        value: float,
        timestamp: datetime | None = None,
    ) -> None:
        """Record a metric value.

        Args:
            system_id: System identifier
            metric_name: Metric name (e.g., 'conversation_count', 'quality_score')
            value: Metric value
            timestamp: When the metric was recorded
        """
        if timestamp is None:
            timestamp = datetime.now(UTC)

        key = f"{system_id}:{metric_name}"
        self._metrics[key].append((timestamp, value))

    async def aggregate(
        self,
        metric_name: str,
        interval: str = "1d",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[str, Any]:
        """Aggregate metric by time interval.

        Args:
            metric_name: Metric to aggregate
            interval: Interval ('1h', '1d', '7d', '30d')
            start: Start time
            end: End time

        Returns:
            Aggregated time-series data
        """
        if end is None:
            end = datetime.now(UTC)
        if start is None:
            start = end - timedelta(days=30)

        # TODO: Implement actual aggregation logic
        return {
            "metric_name": metric_name,
            "interval": interval,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "data_points": [],
        }

    async def detect_trends(
        self,
        metric_name: str,
        window_days: int = 7,
    ) -> list[dict[str, Any]]:
        """Detect trends in time-series data.

        Args:
            metric_name: Metric to analyze
            window_days: Rolling window size

        Returns:
            List of trend observations
        """
        trends = []

        # TODO: Implement trend detection
        # - Linear regression slope
        # - Moving average comparison
        # - Mann-Kendall test

        return trends

    async def detect_anomalies(
        self,
        metric_name: str,
        threshold_std: float = 2.0,
    ) -> list[dict[str, Any]]:
        """Detect statistical anomalies.

        Args:
            metric_name: Metric to analyze
            threshold_std: Standard deviations threshold

        Returns:
            List of anomaly detections
        """
        anomalies = []

        # TODO: Implement anomaly detection
        # - Z-score test
        # - IQR (interquartile range)
        # - Seasonal decomposition

        return anomalies

    async def cross_system_correlation(
        self,
        metric_name: str,
        system_ids: list[str] | None = None,
    ) -> dict[str, float]:
        """Calculate cross-system correlation matrix.

        Args:
            metric_name: Metric to correlate
            system_ids: Systems to include

        Returns:
            Correlation matrix
        """
        # TODO: Implement correlation calculation
        return {}
```

______________________________________________________________________

## Week 7: Advanced Knowledge Graph

### Task 7.1: Graph Algorithms

**File**: `akosha/processing/graph_algorithms.py`

```python
"""Advanced knowledge graph algorithms."""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from typing import TYPE_CHECKING, Any

import networkx as nx

if TYPE_CHECKING:
    from akosha.processing.knowledge_graph import KnowledgeGraphBuilder

logger = logging.getLogger(__name__)


class GraphAnalyzer:
    """Advanced graph analysis algorithms.

    Features:
    - Community detection (find clusters)
    - Centrality metrics (importance ranking)
    - Connected components
    - PageRank for entity importance
    """

    def __init__(self, builder: KnowledgeGraphBuilder) -> None:
        """Initialize graph analyzer.

        Args:
            builder: Knowledge graph builder
        """
        self.builder = builder

    def to_networkx(self) -> nx.Graph:
        """Convert to NetworkX graph for analysis.

        Returns:
            NetworkX graph
        """
        G = nx.Graph()

        # Add nodes
        for entity_id, entity in self.builder.entities.items():
            G.add_node(entity_id, **entity.properties)

        # Add edges
        for edge in self.builder.edges:
            G.add_edge(
                edge.source_id,
                edge.target_id,
                weight=edge.weight,
                edge_type=edge.edge_type,
            )

        return G

    def detect_communities(
        self,
        method: str = "louvain",
    ) -> dict[str, list[str]]:
        """Detect communities in the graph.

        Args:
            method: Community detection algorithm ('louvain', 'label_propagation')

        Returns:
            Dictionary mapping community_id to list of entity_ids
        """
        G = self.to_networkx()

        if method == "louvain":
            try:
                import networkx.algorithms.community as nx_community
                communities = nx_community.louvain_communities(G)
            except ImportError:
                # Fallback to label propagation
                communities = self._label_propagation(G)
        else:
            communities = self._label_propagation(G)

        # Convert to dict
        community_dict: dict[int, list[str]] = defaultdict(list)
        for idx, community_id in enumerate(communities):
            community_dict[community_id].append(idx)

        return community_dict

    def calculate_centrality(
        self,
        method: str = "betweenness",
    ) -> dict[str, float]:
        """Calculate centrality metrics for all entities.

        Args:
            method: Centrality metric ('betweenness', 'closeness', 'pagerank', 'degree')

        Returns:
            Dictionary mapping entity_id to centrality score
        """
        G = self.to_networkx()

        if method == "betweenness":
            centrality = nx.betweenness_centrality(G)
        elif method == "closeness":
            centrality = nx.closeness_centrality(G)
        elif method == "pagerank":
            centrality = nx.pagerank(G)
        else:  # degree
            centrality = dict(G.degree(weighted=True))

        return centrality

    def find_connected_components(self) -> list[list[str]]:
        """Find all connected components.

        Returns:
            List of components, each a list of entity_ids
        """
        G = self.to_networkx()
        return [list(c) for c in nx.connected_components(G)]

    @staticmethod
    def _label_propagation(G: nx.Graph) -> list[list[Any]]:
        """Label propagation algorithm."""
        return nx.algorithms.community.label_propagation_communities(G)
```

### Task 7.2: Add MCP Tools for Advanced Graph

**Update**: `akosha/mcp/tools/akosha_tools.py`

```python
@registry.register(
    registry.ToolMetadata(
        name="detect_communities",
        description="Detect communities/clusters in knowledge graph",
        category=ToolCategory.GRAPH,
    )
)
async def detect_communities(
    method: str = "louvain",
) -> dict[str, Any]:
    """Detect communities in the graph.

    Args:
        method: Community detection algorithm

    Returns:
        Community assignments
    """
    from akosha.processing.graph_algorithms import GraphAnalyzer

    analyzer = GraphAnalyzer(graph_builder)
    communities = analyzer.detect_communities(method=method)

    return {
        "method": method,
        "num_communities": len(communities),
        "communities": communities,
    }

@registry.register(
    registry.ToolMetadata(
        name="calculate_centrality",
        description="Calculate centrality metrics for graph entities",
        category=ToolCategory.GRAPH,
    )
)
async def calculate_centrality(
    method: str = "betweenness",
    top_k: int = 20,
) -> dict[str, Any]:
    """Calculate centrality.

    Args:
        method: Centrality metric
        top_k: Return top K entities

    Returns:
        Centrality rankings
    """
    from akosha.processing.graph_algorithms import GraphAnalyzer

    analyzer = GraphAnalyzer(graph_builder)
    centrality = analyzer.calculate_centrality(method=method)

    # Sort and return top K
    sorted_centralities = sorted(
        centrality.items(),
        key=lambda x: x[1],
        reverse=True,
    )[:top_k]

    return {
        "method": method,
        "top_k": top_k,
        "rankings": sorted_centralities,
    }
```

______________________________________________________________________

## Week 8: Real-Time Event-Driven Ingestion

### Task 8.1: Cloudflare R2 Event Notifications

**File**: `akosha/ingestion/event_handler.py`

```python
"""Event-driven ingestion from Cloudflare R2."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from oneiric.adapters.storage.s3 import S3StorageAdapter, S3StorageSettings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class R2EventHandler:
    """Handle Cloudflare R2 object creation events.

    Uses SQS or SNS for event notifications from R2.
    """

    def __init__(
        self,
        queue_url: str,
        hot_store,
        ingestion_processor,
    ):
        """Initialize event handler.

        Args:
            queue_url: SQS queue URL for R2 events
            hot_store: Hot store for insertion
            ingestion_processor: Processing service
        """
        self.queue_url = queue_url
        self.hot_store = hot_store
        self.processor = ingestion_processor
        self._running = False

    async def start(self) -> None:
        """Start event listener."""
        self._running = True
        logger.info(f"R2 event handler started (queue: {self.queue_url})")

        while self._running:
            try:
                # Poll for events (SQS or SNS)
                events = await self._poll_events()

                for event in events:
                    await self._process_event(event)

            except Exception as e:
                logger.error(f"Event processing error: {e}")
                await asyncio.sleep(5)

    async def _poll_events(self) -> list[dict]:
        """Poll for new R2 events.

        Returns:
            List of event notifications
        """
        # TODO: Implement SQS/SNS polling
        # For now, return empty list
        return []

    async def _process_event(self, event: dict) -> None:
        """Process R2 object creation event.

        Args:
            event: Event notification
        """
        # Parse event
        bucket = event.get("bucket")
        key = event.get("key")

        logger.info(f"Processing R2 event: {key}")

        # Extract system_id and upload_id from key
        # Pattern: system_id=<system>/upload_id=<upload>/...
        parts = key.split("/")
        system_id = None
        upload_id = None

        for part in parts:
            if part.startswith("system_id="):
                system_id = part.split("=")[1]
            elif part.startswith("upload_id="):
                upload_id = part.split("=")[1]

        if system_id and upload_id:
            # Trigger ingestion
            await self.processor.process_upload(system_id, upload_id)

    def stop(self) -> None:
        """Stop event handler."""
        self._running = False
        logger.info("R2 event handler stopped")
```

______________________________________________________________________

## Implementation Checklist

### Week 5

- [ ] Embedding service with ONNX all-MiniLM-L6-v2
- [ ] Enhanced vector search with cosine similarity
- [ ] Batch embedding generation
- [ ] Fallback for when embeddings unavailable

### Week 6

- [ ] Time-series aggregator
- [ ] Trend detection (linear regression)
- [ ] Anomaly detection (z-score, IQR)
- [ ] Cross-system correlation matrix

### Week 7

- [ ] Graph analyzer with NetworkX
- [ ] Community detection (Louvain, label propagation)
- [ ] Centrality metrics (betweenness, PageRank)
- [ ] Connected components detection
- [ ] MCP tools: detect_communities, calculate_centrality

### Week 8

- [ ] Cloudflare R2 event handler (SQS/SNS)
- [ ] Event-driven ingestion pipeline
- [ ] Backpressure handling
- [ ] Dead letter queue for failed events

______________________________________________________________________

## Dependencies Added

```toml
[project.optional-dependencies]
# New dependencies for Phase 2
advanced = [
    "sentence-transformers>=2.2.0",
    "onnxruntime>=1.17.0",
    "transformers>=4.21.0",
    "networkx>=3.0",
    "scipy>=1.10.0",
    "boto3>=12.0.0",  # For SQS/SNS event handling
]
```

______________________________________________________________________

## Success Criteria

Phase 2 is complete when:

- [ ] Embeddings are generated for all conversations
- [ ] Vector search returns semantically similar results
- [ ] Time-series trends are detected across systems
- [ ] Knowledge graph communities are identified
- [ ] Event-driven ingestion processes uploads in < 5 minutes
- [ ] All features have >85% test coverage
- [ ] MCP tools are functional and documented

______________________________________________________________________

## Next: Phase 3 (Production Hardening)

After Phase 2, Phase 3 adds:

- Circuit breakers and retry logic
- Comprehensive monitoring with OpenTelemetry
- Load testing and performance optimization
- Kubernetes deployment manifests
- Disaster recovery procedures

See: `docs/PHASE_3_PRODUCTION_HARDENING.md` (to be created)
