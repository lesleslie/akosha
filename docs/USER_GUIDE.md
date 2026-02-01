# Akosha User Guide

Complete guide for using Akosha to aggregate, search, and analyze memories across Session-Buddy instances.

______________________________________________________________________

## Table of Contents

1. [Getting Started](#getting-started)
1. [Embedding Generation](#embedding-generation)
1. [Time-Series Analytics](#time-series-analytics)
1. [Knowledge Graph Queries](#knowledge-graph-queries)
1. [MCP Tool Usage](#mcp-tool-usage)
1. [Advanced Patterns](#advanced-patterns)
1. [Best Practices](#best-practices)
1. [Troubleshooting](#troubleshooting)

______________________________________________________________________

## Getting Started

### Installation Verification

```python
# Test your installation
from akosha.processing.embeddings import get_embedding_service
from akosha.processing.analytics import TimeSeriesAnalytics

# Initialize services
embedding_service = get_embedding_service()
await embedding_service.initialize()

analytics = TimeSeriesAnalytics()

print("✅ Akosha ready!")
print(f"Embedding mode: {'real' if embedding_service.is_available() else 'fallback'}")
```

### First Embedding

```python
# Generate your first semantic embedding
text = "How to implement JWT authentication in FastAPI"
embedding = await embedding_service.generate_embedding(text)

print(f"Generated {len(embedding)}-dimensional embedding")
print(f"First 5 values: {embedding[:5]}")
```

______________________________________________________________________

## Embedding Generation

### Single Text Embedding

```python
from akosha.processing.embeddings import get_embedding_service

service = get_embedding_service()
await service.initialize()

# Generate embedding for text
text = "FastAPI is a modern, fast web framework for building APIs with Python 3.7+"
embedding = await service.generate_embedding(text)

# Returns: numpy array of shape (384,)
print(f"Embedding shape: {embedding.shape}")  # (384,)
print(f"Embedding dtype: {embedding.dtype}")   # float32
```

### Batch Embedding

```python
# Generate embeddings for multiple texts efficiently
texts = [
    "Python is a high-level programming language",
    "JavaScript is used for web development",
    "Rust provides memory safety without garbage collection",
]

embeddings = await service.generate_batch_embeddings(
    texts=texts,
    batch_size=32,  # Process 32 texts at a time
)

print(f"Generated {len(embeddings)} embeddings")
for i, emb in enumerate(embeddings):
    print(f"  Text {i}: shape={emb.shape}")
```

### Similarity Computation

```python
# Compute similarity between two embeddings
text1 = "Machine learning is a subset of AI"
text2 = "ML and AI are related fields"

emb1 = await service.generate_embedding(text1)
emb2 = await service.generate_embedding(text2)

similarity = await service.compute_similarity(emb1, emb2)

print(f"Similarity: {similarity:.3f}")  # 0.85 (high similarity)
```

### Ranking by Similarity

```python
# Rank candidates by similarity to a query
query = "database optimization techniques"
candidates = [
    "How to optimize SQL queries",
    "Introduction to machine learning",
    "Database indexing strategies",
    "Python web development",
]

query_emb = await service.generate_embedding(query)
candidate_embs = await service.generate_batch_embeddings(candidates)

rankings = await service.rank_by_similarity(
    query_embedding=query_emb,
    candidate_embeddings=candidate_embs,
    limit=3,
)

for idx, similarity in rankings:
    print(f"{similarity:.3f}: {candidates[idx]}")
```

______________________________________________________________________

## Time-Series Analytics

### Tracking Metrics

```python
from akosha.processing.analytics import TimeSeriesAnalytics
from datetime import datetime, timedelta, UTC

analytics = TimeSeriesAnalytics()
now = datetime.now(UTC)

# Track conversation count over time
for hours_ago in range(24, 0, -1):
    timestamp = now - timedelta(hours=hours_ago)
    count = 100 + (24 - hours_ago) * 5  # Increasing trend

    await analytics.add_metric(
        metric_name="conversation_count",
        value=count,
        system_id="session-buddy-001",
        timestamp=timestamp,
        metadata={"time_slot": "hourly"},
    )
```

### Analyzing Trends

```python
# Detect trends in your metrics
trend = await analytics.analyze_trend(
    metric_name="conversation_count",
    system_id="session-buddy-001",
    time_window=timedelta(days=7),
)

if trend:
    print(f"Direction: {trend.trend_direction}")     # "increasing"
    print(f"Strength: {trend.trend_strength:.2f}")    # 0.85+
    print(f"Change: {trend.percent_change:.1f}%")      # +45%
    print(f"Confidence: {trend.confidence:.2f}")        # 0.70
```

### Detecting Anomalies

```python
# Find unusual patterns in metrics
await analytics.add_metric("error_rate", 5.2, "system-1")
await analytics.add_metric("error_rate", 4.8, "system-1")
await analytics.add_metric("error_rate", 95.0, "system-1")  # Spike!
await analytics.add_metric("error_rate", 5.1, "system-1")

anomalies = await analytics.detect_anomalies(
    metric_name="error_rate",
    system_id="system-1",
    time_window=timedelta(days=1),
    threshold_std=2.5,  # 2.5 standard deviations
)

if anomalies:
    print(f"Found {anomalies.anomaly_count} anomalies")
    print(f"Anomaly rate: {anomalies.anomaly_rate:.1%}")

    for anomaly in anomalies.anomalies:
        print(f"  Value: {anomaly['value']:.2f}")
        print(f"  Z-score: {anomaly['z_score']:.2f}")
        print(f"  Time: {anomaly['timestamp']}")
```

### Cross-System Correlation

```python
# Compare metrics across multiple systems
for i in range(20):
    base_value = 50.0 + i

    # System 1
    await analytics.add_metric(
        "quality_score",
        base_value,
        "session-buddy-001",
        timestamp=now - timedelta(hours=20-i),
    )

    # System 2 (correlated)
    await analytics.add_metric(
        "quality_score",
        base_value + 3,
        "session-buddy-002",
        timestamp=now - timedelta(hours=20-i),
    )

# Analyze correlations
correlation = await analytics.correlate_systems(
    metric_name="quality_score",
    time_window=timedelta(days=7),
)

print(f"Systems analyzed: {len(correlation.systems)}")
print(f"Significant correlations: {len(correlation.system_pairs)}")

for pair in correlation.system_pairs:
    print(f"{pair['system_1']} ↔ {pair['system_2']}")
    print(f"  Correlation: {pair['correlation']:.3f}")
    print(f"  Strength: {pair['strength']}")
```

______________________________________________________________________

## Knowledge Graph Queries

### Building the Graph

```python
from akosha.processing.knowledge_graph import KnowledgeGraphBuilder

builder = KnowledgeGraphBuilder()

# Build graph from conversation
conversation = {
    "system_id": "session-buddy-001",
    "conversation_id": "conv-123",
    "content": """
    Alice worked on the FastAPI authentication system.
    She fixed JWT token validation and improved error handling.
    The project is called 'auth-service' and uses Python 3.13.
    """,
    "timestamp": datetime.now(UTC),
    "metadata": {},
}

await builder.build_from_conversation(conversation)
```

### Querying Entity Relationships

```python
# Find what entities are related to
neighbors = builder.get_neighbors(
    entity_id="user:alice",
    edge_type="worked_on",
    limit=10,
)

print(f"Alice worked on {len(neighbors)} projects:")
for neighbor in neighbors:
    print(f"  - {neighbor['entity_id']} ({neighbor['entity_type']})")
    print(f"    Edge: {neighbor['edge_type']}")
```

### Finding Paths Between Entities

```python
# Find shortest path between two entities
path = builder.find_shortest_path(
    source_id="user:alice",
    target_id="project:auth-service",
    max_hops=3,
)

if path:
    print(f"Path found ({len(path)-1} hops):")
    for i, entity_id in enumerate(path):
        print(f"  {i}. {entity_id}")
```

### Graph Statistics

```python
# Get graph insights
stats = builder.get_statistics()

print(f"Total entities: {stats['total_entities']}")
print(f"Total edges: {stats['total_edges']}")
print(f"Entity types: {stats['entity_type_counts']}")
print(f"Edge types: {stats['edge_type_counts']}")
```

______________________________________________________________________

## MCP Tool Usage

### Using Akosha MCP Tools

Once configured (see README), tools are available in Claude Code:

#### Search Tools

```
# Generate embeddings
User: Generate an embedding for "How to implement caching in FastAPI"

# Batch embeddings
User: Generate embeddings for these 5 texts:
- "Python type hints"
- "Async/await patterns"
- "Docker containers"
- "Kubernetes deployment"
- "CI/CD pipelines"

# Semantic search
User: Search across all systems for "database connection pooling"
User: Search system "session-buddy-001" for "JWT authentication"
```

#### Analytics Tools

```
# Get system metrics
User: Get metrics for the last 30 days

# Analyze trends
User: Analyze trend for "conversation_count" on "session-buddy-001"
User: Is "error_rate" trending up or down?

# Detect anomalies
User: Find anomalies in "response_time" with 3.0 sigma threshold
User: Any unusual patterns in "memory_usage"?

# Cross-system correlation
User: Which systems have similar "quality_score" patterns?
User: Correlate "commit_frequency" across all systems
```

#### Graph Tools

```
# Query knowledge graph
User: What has "user:bob" worked on?
User: Find all entities related to "project:auth-service"

# Find paths
User: What's the shortest path from "user:alice" to "concept:authentication"?
User: How is "FastAPI" connected to "project:payment-service"?

# Graph statistics
User: How many entities are in the knowledge graph?
User: What are the most common relationship types?
```

______________________________________________________________________

## Advanced Patterns

### Pattern 1: Continuous Monitoring

```python
import asyncio
from datetime import datetime, UTC

async def monitor_system_metrics(analytics, system_id, duration_hours=24):
    """Continuously monitor and alert on anomalies."""
    print(f"🔍 Monitoring {system_id} for {duration_hours} hours...")

    while True:
        # Check for anomalies every hour
        anomalies = await analytics.detect_anomalies(
            metric_name="error_rate",
            system_id=system_id,
            time_window=timedelta(hours=1),
            threshold_std=2.5,
        )

        if anomalies and anomalies.anomaly_count > 0:
            print(f"⚠️  {anomalies.anomaly_count} anomalies detected!")
            for anomaly in anomalies.anomalies:
                print(f"  {anomaly['timestamp']}: {anomaly['value']:.2f}")

        await asyncio.sleep(3600)  # Wait 1 hour
```

### Pattern 2: Semantic Deduplication

```python
async def find_similar_conversations(embedding_service, conversations, threshold=0.9):
    """Find duplicate or very similar conversations."""
    seen_embeddings = []
    duplicates = []

    for conv in conversations:
        emb = await embedding_service.generate_embedding(conv['content'])

        # Check against existing embeddings
        for seen_emb, seen_conv in seen_embeddings:
            similarity = await embedding_service.compute_similarity(emb, seen_emb)
            if similarity >= threshold:
                duplicates.append({
                    'original': seen_conv,
                    'duplicate': conv,
                    'similarity': similarity,
                })
                break

        seen_embeddings.append((emb, conv))

    return duplicates
```

### Pattern 3: Cross-System Learning

```python
async def find_best_practices(analytics, metric_name):
    """Find systems with best performance for a metric."""
    # Analyze trends for all systems
    trends = []
    for system_id in ["system-1", "system-2", "system-3"]:
        trend = await analytics.analyze_trend(
            metric_name=metric_name,
            system_id=system_id,
            time_window=timedelta(days=30),
        )
        if trend and trend.trend_direction == "increasing":
            trends.append((system_id, trend))

    # Sort by trend strength
    trends.sort(key=lambda x: x[1].trend_strength, reverse=True)

    print(f"🏆 Best practices for {metric_name}:")
    for system_id, trend in trends[:3]:
        print(f"  {system_id}: {trend.trend_strength:.2f} strength")

    return trends
```

______________________________________________________________________

## Best Practices

### 1. Embedding Generation

✅ **DO:**

- Use batch embedding for multiple texts
- Cache embeddings for frequently searched content
- Normalize input text (lowercase, remove extra whitespace)
- Use fallback mode during development

❌ **DON'T:**

- Generate embeddings for very short texts (\<10 characters)
- Re-embed identical content without caching
- Use embeddings for non-text data without adaptation

### 2. Analytics Queries

✅ **DO:**

- Use appropriate time windows (7-30 days for trends)
- Set realistic anomaly thresholds (2.5-3.0 sigma)
- Filter by system_id when analyzing specific instances
- Analyze trends before and after deployments

❌ **DON'T:**

- Use very short time windows (\<1 hour) for trend analysis
- Set anomaly thresholds too low (\<2.0) - too many false positives
- Ignore metric metadata - it provides valuable context

### 3. Knowledge Graph

✅ **DO:**

- Build graph incrementally as conversations arrive
- Use descriptive entity IDs (e.g., "user:alice", "project:X")
- Query with appropriate limits (10-50 neighbors)
- Find paths with reasonable max_hops (3-5)

❌ **DON'T:**

- Create entities for every word (be selective)
- Use numeric IDs without meaning
- Query unlimited neighbors (performance issues)
- Set max_hops too high (>10)

### 4. Performance

✅ **DO:**

- Batch operations when possible
- Use async/await throughout
- Close database connections when done
- Monitor memory usage with large embeddings

❌ **DON'T:**

- Generate embeddings in tight loops without batching
- Block event loop with synchronous operations
- Leave connections open indefinitely
- Embed massive documents (>100K words) without summarization

______________________________________________________________________

## Troubleshooting

### Issue: Embedding generation is slow

**Solution:**

```python
# Use batch processing
texts = [text1, text2, text3, ...]
embeddings = await service.generate_batch_embeddings(texts, batch_size=32)
```

### Issue: Low similarity scores (\<0.5)

**Solution:**

```python
# Check if using real embeddings
if not service.is_available():
    print("⚠️ Using fallback mode - install sentence-transformers")
    print("  uv add --optional embeddings sentence-transformers")
```

### Issue: No anomalies detected

**Solution:**

```python
# Lower threshold
anomalies = await analytics.detect_anomalies(
    metric_name="error_rate",
    threshold_std=2.0,  # Try 2.0 instead of 3.0
)
```

### Issue: Trend analysis returns None

**Solution:**

```python
# Check data point count
# Need at least 10 data points for trend analysis
for i in range(10):
    await analytics.add_metric(metric_name, value, system_id)
```

### Issue: Graph queries return empty results

**Solution:**

```python
# Build graph first
await builder.build_from_conversation(conversation)

# Check entity exists
stats = builder.get_statistics()
print(f"Entities in graph: {stats['total_entities']}")

# Use exact entity ID
neighbors = builder.get_neighbors(entity_id="user:alice")
```

______________________________________________________________________

## Additional Resources

- **GitHub Repository**: [https://github.com/yourusername/akosha](https://github.com/yourusername/akosha)
- **Architecture**: [ADR_001_ARCHITECTURE_DECISIONS.md](ADR_001_ARCHITECTURE_DECISIONS.md)
- **Roadmap**: [ROADMAP.md](ROADMAP.md)
- **Issues**: [GitHub Issues](https://github.com/yourusername/akosha/issues)

______________________________________________________________________

**Happy aggregating!** 🚀

*आकाश (Akosha) - The sky has no limits*
