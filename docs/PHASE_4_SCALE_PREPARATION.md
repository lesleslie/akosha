# Akasha Phase 4: Scale Preparation

**Status**: Planned
**Duration**: Weeks 11-12 (2 weeks)
**Focus**: Hyperscale architecture, 100M+ embeddings, distributed systems

---

## Overview

Phase 3 hardened the system for production. Phase 4 prepares for hyperscale growth (10,000-100,000 systems):

1. **Milvus Integration** (100M-1B embeddings)
2. **TimescaleDB** (advanced time-series analytics)
3. **Neo4j** (complex graph queries)
4. **Multi-Region Disaster Recovery**

---

## Week 11: Distributed Vector Database

### Task 11.1: Milvus Cluster Setup

**Purpose**: Handle 100M-1B vector embeddings with sub-millisecond search

**Architecture**:

```yaml
# Milvus cluster (Helm chart)
milvus:
  enabled: true
  mode: clustered
  
  etcd:
    # Replicated etcd for Milvus coordination
    replicaCount: 3
  
  milvus:
    image: milvusdb/milvus:v2.4.0
    replicaCount: 3
    
    config:
      # Disk-based indexing (vectors don't need to fit in RAM)
      indexType: "DiskANN"
      
      # S3-compatible storage for vectors
      minio:
        enabled: true
        accessKey: "${MINIO_ACCESS_KEY}"
        secretKey: "${MINIO_SECRET_KEY}"
  
  pulsar:
    # Message queue for Milvus
    enabled: true
    replicaCount: 3
  
  # Data persistence
  persistence:
    enabled: true
    storageClass: "fast-ssd"
```

**Integration with Akasha**:

```python
"""Milvus warm tier integration."""

from milvus import Milvus,connections

class MilvusWarmStore:
    """Milvus-backed warm tier for large-scale vector search."""
    
    def __init__(self):
        """Initialize Milvus client."""
        self.client = Milvus(
            host="milvus.akasha.svc.cluster.local",
            port=19530,
        )
        
        # Create collection for embeddings
        if not self.client.has_collection("embeddings"):
            self.client.create_collection(
                collection_name="embeddings",
                dimension=384,  # all-MiniLM-L6-v2
                index_type="DISKANN",
                metric_type="COSINE",
                index_param={
                    "M": 16,
                    "ef_construction": 200,
                },
            )
    
    async def search(
        self,
        query_vector: list[float],
        limit: int = 100,
    ) -> list[dict]:
        """Search Milvus collection.
        
        Args:
            query_vector: Query embedding
            limit: Max results
            
        Returns:
            Search results with metadata
        """
        results = self.client.search(
            collection_name="embeddings",
            data=[query_vector],
            limit=limit,
            output_fields=["conversation_id", "system_id", "content"],
        )
        
        return results
```

### Task 11.2: Hybrid Search Strategy

**File**: `akasha/query/hybrid_search.py`

```python
"""Hybrid search across hot and warm tiers."""

from akasha.storage.hot_store import HotStore
from akasha.query.milvus_store import MilvusWarmStore


class HybridVectorSearch:
    """Unified search across DuckDB hot and Milvus warm tiers.
    
    Routing strategy:
    - Recent data (< 7 days): Hot store (DuckDB in-memory)
    - Historical data (7-90 days): Warm store (Milvus cluster)
    - Cold archive (> 90 days): Skip (too slow)
    """
    
    def __init__(self, hot_store: HotStore, milvus_store: MilvusWarmStore):
        """Initialize hybrid search."""
        self.hot_store = hot_store
        self.milvus_store = milvus_store
    
    async def search(
        self,
        query_embedding: list[float],
        date_range_start: datetime,
        date_range_end: datetime,
        limit: int = 10,
    ) -> list[dict]:
        """Search across tiers based on date range.
        
        Args:
            query_embedding: Query vector
            date_range_start: Search range start
            date_range_end: Search range end
            limit: Max results
            
        Returns:
            Unified search results
        """
        now = datetime.now(UTC)
        days_ago = (now - date_range_end).days
        
        results = []
        
        # Hot tier: Recent 7 days
        if days_ago < 7:
            hot_results = await self.hot_store.search_similar(
                query_embedding=query_embedding,
                limit=limit,
            )
            results.extend(hot_results)
        
        # Warm tier: 7-90 days (use Milvus)
        if 7 <= days_ago < 90:
            warm_results = await self.milvus_store.search(
                query_vector=query_embedding,
                limit=limit,
            )
            results.extend(warm_results)
        
        # Merge and re-rank
        results.sort(key=lambda r: r["similarity"], reverse=True)
        return results[:limit]
```

---

## Week 12: Advanced Analytics & DR

### Task 12.1: TimescaleDB Integration

**Purpose**: Advanced time-series analytics with continuous aggregates

```sql
-- TimescaleDB hypertable for metrics
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

CREATE TABLE IF NOT EXISTS metrics (
    timestamp TIMESTAMP NOT NULL,
    system_id VARCHAR NOT NULL,
    metric_name VARCHAR NOT NULL,
    metric_value FLOAT NOT NULL,
    tags JSONB
);

SELECT create_hypertable('metrics', 'timestamp', 'chunk_time_interval => INTERVAL 1 day');

-- Continuous aggregates
CREATE MATERIALIZED VIEW metrics_5min WITH (timescaledb.continuous) AS
SELECT
    time_bucket('5 minutes', timestamp) AS bucket,
    system_id,
    metric_name,
    AVG(metric_value) AS avg_value,
    MIN(metric_value) AS min_value,
    MAX(metric_value) AS max_value,
    COUNT(*) AS count
FROM metrics
GROUP BY bucket, system_id, metric_name;

CREATE MATERIALIZED VIEW metrics_1hour WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', timestamp) AS bucket,
    system_id,
    metric_name,
    AVG(metric_value) AS avg_value,
    MIN(metric_value) AS min_value,
    MAX(metric_value) AS max_value,
    COUNT(*) AS count
FROM metrics
GROUP BY bucket, system_id, metric_name;

-- Refresh policy
SELECT add_continuous_aggregate_policy('metrics_5min',
    start_offset => INTERVAL '1 hour',
    end_offset => INTERVAL '10 minutes',
    schedule_interval => '5 minutes');

SELECT add_continuous_aggregate_policy('metrics_1hour',
    start_offset => '1 day',
    end_offset => '1 hour',
    schedule_interval => '1 hour');
```

### Task 12.2: Neo4j Knowledge Graph

**Purpose**: Complex graph queries with 100M+ edges

```python
"""Neo4j integration for large-scale knowledge graphs."""

from neo4j import AsyncGraphDriver

class Neo4jGraphStore:
    """Neo4j-backed knowledge graph for complex queries.
    
    Used when:
    - Graph has >100M edges
    - Complex multi-hop queries needed
    - Native graph algorithms required
    """
    
    def __init__(self, uri: str = "bolt://neo4j:7687"):
        """Initialize Neo4j driver.
        
        Args:
            uri: Neo4j Bolt protocol URI
        """
        self.driver = AsyncGraphDriver(uri)
    
    async def store_entity(self, entity: GraphEntity) -> None:
        """Store entity in Neo4j.
        
        Args:
            entity: Entity to store
        """
        async with self.driver.session() as session:
            await session.run(
                "MERGE (e:Entity {id: $id}) "
                "SET e.entity_type = $type, e.properties = $props",
                id=entity.entity_id,
                type=entity.entity_type,
                props=entity.properties,
            )
    
    async def find_shortest_path(
        self,
        source_id: str,
        target_id: str,
        max_hops: int = 5,
        relationship_types: list[str] | None = None,
    ) -> list[str] | None:
        """Find shortest path using Cypher.
        
        Args:
            source_id: Source entity
            target_id: Target entity
            max_hops: Maximum path length
            relationship_types: Optional filter
            
        Returns:
            Path as list of entity IDs, or None if no path
        """
        async with self.driver.session() as session:
            if relationship_types:
                rel_filter = ":" + "|:".join(relationship_types) + ":"
                query = f"""
                    MATCH path = shortestPath(
                        (a {{id: $source}})-{rel_filter}*1..{max_hops}-(b {{id: $target}})
                    RETURN [node.id(node) FOR node IN nodes(path)]
                """
            else:
                query = """
                    MATCH path = shortestPath(
                        (a {id: $source})-*1..$max_hops-(b {id: $target})
                    )
                    RETURN [node.id(node) FOR node IN nodes(path)]
                """
            
            result = await session.run(
                query,
                source=source_id,
                target=target_id,
                max_hops=max_hops,
            )
            
            record = await result.single()
            if record:
                return record[0]
            return None
```

### Task 12.3: Multi-Region Disaster Recovery

**Architecture**:

```
Region 1 (Primary)          Region 2 (Secondary)
┌─────────────────┐        ┌─────────────────┐
│  Akasha (Active) │────────>│  Akasha (Standby) │
│  - Hot Store      │         │  - Hot Store      │
│  - Milvus         │         │  - Milvus         │
│  - TimescaleDB    │         │  - TimescaleDB    │
└─────────────────┘        └─────────────────┘
        │                                │
        │ Cloudflare R2                │ Cloudflare R2
        │ (Primary Bucket)              │ (Replica Bucket)
        └────────────────────────────────┘
                    │
                    v
        ┌─────────────────┐
        │  Region 3       │
        │  (Backup Only)  │
        └─────────────────┘
```

**Implementation**:

```python
"""Multi-region disaster recovery."""

class MultiRegionCoordinator:
    """Coordinate cross-region replication."""
    
    def __init__(self, primary_region: str, secondary_regions: list[str]):
        """Initialize coordinator.
        
        Args:
            primary_region: Primary region (e.g., 'us-east-1')
            secondary_regions: List of secondary regions
        """
        self.primary_region = primary_region
        self.secondary_regions = secondary_regions
        self._current_region = primary_region
    
    async def replicate_to_secondary(self, data: dict) -> None:
        """Replicate data to secondary regions.
        
        Args:
            data: Data to replicate
        """
        for region in self.secondary_regions:
            try:
                await self._upload_to_region(region, data)
            except Exception as e:
                logger.error(f"Failed to replicate to {region}: {e}")
    
    async def promote_to_primary(self, new_primary: str) -> None:
        """Promote secondary region to primary.
        
        Args:
            new_primary: Region to promote
        """
        if new_primary not in self.secondary_regions:
            raise ValueError(f"Unknown region: {new_primary}")
        
        # Update routing
        self.secondary_regions.remove(new_primary)
        self.secondary_regions.append(self.primary_region)
        self.primary_region = new_primary
        self._current_region = new_primary
        
        logger.warning(f"Promoted {new_primary} to primary region")
```

---

## Scaling Triggers

| Scale | Embeddings | Vector DB | Time-Series | Graph DB | Trigger |
|-------|-----------|-----------|-------------|----------|---------|
| **100 systems** | 1M | DuckDB | DuckDB | DuckDB + Redis | ✅ Phase 1 |
| **1,000 systems** | 10M | DuckDB | TimescaleDB | DuckDB + Redis | Add Milvus |
| **10,000 systems** | 100M | Milvus | TimescaleDB + replicas | Neo4j | Add replicas |
| **100,000 systems** | 1B | Milvus cluster | TimescaleDB cluster | Neo4j cluster | Multi-region |

---

## Implementation Checklist

### Week 11
- [ ] Deploy Milvus cluster (3 replicas)
- [ ] Hybrid search (DuckDB + Milvus)
- [ ] Auto-tier migration based on query performance
- [ ] Milvus health monitoring

### Week 12
- [ ] Deploy TimescaleDB with continuous aggregates
- [ ] Deploy Neo4j for 100M+ edges
- [ ] Multi-region replication setup
- [ ] Disaster recovery testing
- [ ] Runbook documentation

---

## Success Criteria

Phase 4 is complete when:

- [ ] System can handle 100M+ embeddings
- [ ] Search latency < 100ms (p95) for hot tier
- [ ] Search latency < 500ms (p95) for warm tier
- [ ] Time-series queries run in <1 second
- [ ] Graph path finding <2 seconds for 5 hops
- [ ] RPO < 5 minutes, RTO < 1 hour for DR
- [ ] System tested at 10,000 systems scale

---

## Complete Roadmap Summary

| Phase | Duration | Focus | Status |
|-------|----------|-------|--------|
| **Phase 1** | Weeks 1-4 | Foundation (Storage, Ingestion, Basic Graph) | ✅ Complete |
| **Phase 2** | Weeks 5-8 | Advanced Features (Embeddings, Trends, Advanced Graph) | 📋 Planned |
| **Phase 3** | Weeks 9-10 | Production Hardening (Resilience, Observability, K8s) | 📋 Planned |
| **Phase 4** | Weeks 11-12 | Hyperscale (Milvus, TimescaleDB, Neo4j, Multi-Region) | 📋 Planned |

**Total Timeline**: 12 weeks to production at 10,000+ systems

---

## Dependencies

```toml
[project.optional-dependencies]
# Phase 4 dependencies
hyperscale = [
    "pymilvus>=2.4.0",
    "timescaledb>=0.15.0",
    "neo4j>=5.0.0",
    "asyncpg>=0.29.0",  # TimescaleDB driver
    "minio>=2024.0.0",  # For Milvus MinIO
]
```

---

## Production Readiness

After completing all 4 phases, Akasha will be:

- ✅ **Scalable**: 100 to 100,000 systems
- ✅ **Resilient**: Circuit breakers, retries, graceful degradation
- ✅ **Observable**: Full tracing, metrics, structured logging
- ✅ **Performant**: Sub-100ms search (hot), <500ms (warm)
- ✅ **Production-Ready**: Kubernetes deployment, DR tested

**The universal memory aggregation system will be ready to handle cross-system intelligence at massive scale!** 🚀
