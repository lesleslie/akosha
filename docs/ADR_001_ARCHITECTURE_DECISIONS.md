# ADR-001: Akasha Architecture Decisions

**Date**: 2025-01-25
**Status**: Accepted
**Context**: Universal Memory Aggregation System Design
**Specialists Consulted**: Code-Architect, Oneiric-Specialist, Orchestration-Specialist, Database Expert

---

## Executive Summary

This document captures all architectural decisions for **Akasha** (आकाश), a universal memory aggregation system that ingests, stores, analyzes, and provides cross-system intelligence from 100-10,000 Session-Buddy instances.

**Key Decision**: Hybrid architecture with clear separation of concerns:
- **Session-Buddy**: User/system memory collection + cloud upload
- **Mahavishnu**: Workflow orchestration and job scheduling
- **Oneiric**: Universal storage abstraction layer
- **Akasha**: Ingestion, aggregation, analytics, and query serving

---

## Decision 1: Ingest-Only Architecture (Pull Model)

**Status**: ✅ Accepted

**Context**:
- Session-Buddy needs to upload system memories to cloud storage
- Akasha needs to process these uploads for cross-system analysis
- Question: Should Akasha be in the write path or use a pull model?

**Decision**:
Session-Buddy uploads directly to cloud storage (S3/Azure/GCS) via Oneiric adapters. Akasha runs ingestion workers that **pull** from cloud storage.

**Rationale**:
| Pro | Con |
|-----|-----|
| ✅ Session-Buddy remains operational even if Akasha is down | ❌ Slight delay (minutes) between upload and ingestion |
| ✅ No latency added to user workflows | ❌ Need discovery mechanism for new uploads |
| ✅ Akasha can scale/evolve independently | ❌ Duplicate storage during processing |
| ✅ Clear failure isolation boundaries | |
| ✅ Simplified error handling | |

**Implementation**:
```python
# Session-Buddy uploads to cloud (no Akasha dependency)
await oneiric_storage.upload(
    bucket="session-buddy-memories",
    path=f"systems/{system_id}/memory.db",
    data=memory_db
)

# Akasha pulls from cloud (independent service)
uploads = await oneiric_storage.list_prefixes(
    bucket="session-buddy-memories",
    prefix=f"systems/{system_id}/"
)
```

**Alternatives Considered**:
1. **Push Model**: Session-Buddy calls Akasha API directly ❌ (Creates tight coupling)
2. **Message Queue**: Session-Buddy publishes to queue, Akasha consumes ❌ (Adds infrastructure complexity)

**Consequences**:
- Session-Buddy and Akasha can be deployed independently
- Cloud storage becomes source of truth
- Akasha can replay/reprocess historical data
- Need S3 EventBridge or polling for new upload discovery

---

## Decision 2: Three-Tier Storage Architecture

**Status**: ✅ Accepted

**Context**:
- Hot data (recent) needs sub-100ms search
- Warm data (historical) needs analytics queries
- Cold data (archival) needs cost optimization
- Question: How to tier storage for optimal cost/performance?

**Decision**:
Three-tier storage with automatic aging based on data age and access patterns.

| Tier | Retention | Storage | Characteristics | Cost |
|------|-----------|---------|-----------------|------|
| **Hot** | 0-7 days | DuckDB in-memory + Redis cache | Full embeddings, sub-100ms search | $$$ |
| **Warm** | 7-90 days | DuckDB on-disk (NVMe) | Compressed embeddings (INT8), 100-500ms | $$ |
| **Cold** | 90+ days | Parquet in S3/Azure/GCS | Summaries only, no embeddings | $ |

**Rationale**:
- **80% cost reduction** for cold data (no embeddings, compressed summaries)
- **Sub-second search** for recent data (most queries target hot tier)
- **Automatic aging** reduces operational overhead
- **Oneiric storage adapters** enable multi-cloud deployment

**Implementation**:
```python
# Hot tier: DuckDB in-memory
hot_store = DuckDBAdapter(database_path=":memory:")

# Warm tier: DuckDB on NVMe SSD
warm_store = DuckDBAdapter(database_path="/data/akasha/warm.duckdb")

# Cold tier: Parquet via Oneiric
cold_storage = OneiricStorageAdapter(backend="s3", config={
    "bucket": "akasha-cold",
    "prefix": "conversations/",
    "format": "parquet"
})

# Automatic aging service
aging_service = AgingService()
await aging_service.migrate_hot_to_warm()
await aging_service.migrate_warm_to_cold()
```

**Alternatives Considered**:
1. **Single-tier storage**: ❌ (Too expensive for 1PB+ data)
2. **Two-tier storage (hot/cold)**: ⚠️ (Would lose analytics performance)

**Consequences**:
- Need aging service to manage tier transitions
- Queries must check multiple tiers and merge results
- Cold tier queries are slower (acceptable for archival)

---

## Decision 3: Database Technology Selection

**Status**: ✅ Accepted (with evolution path)

**Context**:
- Need to store 1B+ vector embeddings
- Need time-series aggregation
- Need knowledge graph for cross-system relationships
- Question: Which databases for each workload?

**Decision**:
Phase-based evolution starting with DuckDB, adding specialized databases at scale.

| Phase | Scale | Vector DB | Time-Series | Graph DB | Conversations |
|-------|-------|-----------|-------------|----------|---------------|
| **Phase 1** | 100 systems | DuckDB HNSW | DuckDB | DuckDB + Redis | Parquet |
| **Phase 2** | 1,000 systems | + Milvus | + TimescaleDB | DuckDB + Redis | Parquet |
| **Phase 3** | 10,000 systems | Milvus | TimescaleDB | + Neo4j | Parquet |
| **Phase 4** | 100,000+ systems | Milvus or cloud | + ClickHouse | Neo4j | Parquet |

**Rationale**:
- **Start simple**: DuckDB handles all workloads at small scale (<100M embeddings)
- **Add specialized databases**: When hitting scale limits (100M+ embeddings, 100M+ edges)
- **Avoid premature optimization**: Don't deploy Neo4j until you have 100M+ edges
- **Cloud-native fallback**: Managed services (AWS OpenSearch, Azure AI Search) at hyperscale

**Vector Database Evolution**:
```python
# Phase 1: DuckDB HNSW (< 100M embeddings)
vector_store = DuckDBVectorStore()

# Phase 2: Add Milvus (100M-1B embeddings)
vector_store = HybridVectorStore(
    hot=DuckDBVectorStore(),  # Recent 7 days
    warm=MilvusClient()       # Historical data
)

# Phase 3: Consider cloud-native (1B+ embeddings)
vector_store = CloudVectorService(
    provider="aws-opensearch-serverless"
)
```

**Time-Series Database**:
```python
# Phase 1+: TimescaleDB (PostgreSQL extension)
time_series = TimescaleDBAdapter()
# Automatic continuous aggregates: raw → 5min → 1hour
```

**Knowledge Graph**:
```python
# Phase 1-2: DuckDB + Redis (< 100M edges)
graph = HybridGraphStore(
    persistent=DuckDBAdapter(),  # Nodes and edges
    cache=RedisAdapter()          # Fast adjacency lists
)

# Phase 3+: Neo4j (100M+ edges)
graph = Neo4jAdapter()
```

**Alternatives Considered**:
1. **Start with Milvus**: ❌ (Premature optimization, higher ops cost)
2. **Use PostgreSQL + pgvector**: ⚠️ (Good, but TimescaleDB better for time-series)
3. **Use dedicated vector DB from day 1**: ❌ (Overkill for <100M embeddings)

**Consequences**:
- Need migration paths when adding specialized databases
- Must monitor scaling triggers (100M embeddings, 100M edges)
- Architecture must support dual writes during migration

---

## Decision 4: System-Based Sharding

**Status**: ✅ Accepted

**Context**:
- Need to horizontally scale to 10,000+ systems
- Each system has multiple users with memories
- Question: How to shard data across multiple nodes?

**Decision**:
Consistent hashing by `system_id` across 256 shards.

**Rationale**:
| Benefit | Explanation |
|---------|-------------|
| ✅ Predictable routing | Same system always goes to same shard |
| ✅ Easy rebalancing | Add/remove shards with minimal data movement |
| ✅ Per-system queries | No cross-shard joins for single-system queries |
| ✅ Isolation | One heavy system doesn't affect others |

**Implementation**:
```python
class ShardRouter:
    """Consistent hashing router for 256 shards."""

    def get_shard(self, system_id: str) -> int:
        hash_val = int(hashlib.sha256(system_id.encode()).hexdigest(), 16)
        return hash_val % 256

    def get_shard_path(self, system_id: str) -> Path:
        shard_id = self.get_shard(system_id)
        return Path(f"/data/shard_{shard_id:03d}/{system_id}.duckdb")
```

**Query Routing**:
```python
# Fan-out query across shards
tasks = [
    search_shard(shard_id, query_embedding)
    for shard_id in target_shards
]
results = await asyncio.gather(*tasks)
```

**Alternatives Considered**:
1. **Date-based sharding**: ❌ (Time queries scan all shards)
2. **Hash-based on conversation_id**: ❌ (Same system scattered across shards)
3. **Geographic sharding**: ⚠️ (Not needed initially)

**Consequences**:
- Hotspot risk if one system dominates (mitigate with monitoring)
- Need shard rebalancing strategy
- Queries across all systems = query all 256 shards

---

## Decision 5: Mahavishnu-Akasha Orchestration

**Status**: ✅ Accepted

**Context**:
- Mahavishnu orchestrates workflows across services
- Akasha needs scheduled jobs and triggers
- Question: Should Mahavishnu orchestrate Akasha, or should Akasha be autonomous?

**Decision**:
**Hybrid coordination model**: Mahavishnu orchestrates workflows, Akasha executes autonomously.

**Responsibilities**:

| **Mahavishnu** (Orchestrator) | **Akasha** (Executor) |
|------------------------------|----------------------|
| ✅ Schedule and trigger workflows | ✅ Execute ingestion logic |
| ✅ Monitor Akasha health and backlog | ✅ Store and process embeddings |
| ✅ Scale Akasha pods based on metrics | ✅ Manage storage tiers |
| ✅ Handle alerts and escalation | ✅ Serve queries |
| ✅ Coordinate maintenance windows | ✅ Report health metrics |
| ❌ Execute memory ingestion | ❌ Schedule its own workflows |
| ❌ Store or process embeddings | ❌ Scale its own pods |
| ❌ Manage storage tiers | ❌ Monitor other services |

**Communication Patterns**:

```text
Mahavishnu                      Akasha
    │                              │
    ├─ MCP (orchestration) ──────▶│  Trigger workflows
    │   ◀─ (status/metrics)        │  Report health
    │                              │
    └─ HTTP (data ops) ──────────▶│  Query embeddings
        ◀─ (search results)        │  Return results
```

**Trigger Patterns**:

| Trigger Type | Source | Action |
|--------------|--------|--------|
| **S3 Object Created** | AWS EventBridge | Ingest new memory upload |
| **Scheduled Daily** | Cron (Mahavishnu) | Batch ingestion job |
| **Scheduled Hourly** | Cron (Mahavishnu) | Tier transition evaluation |
| **Manual User** | MCP Tool | Force ingestion/analysis |
| **Health Check** | Mahavishnu (15min) | Verify Akasha availability |
| **Backlog Alert** | Akasha metrics | Scale up ingestion |

**Implementation**:
```python
# Mahavishnu triggers Akasha workflow
@mahavishnu_server.tool()
async def trigger_akasha_ingest(
    source_system: str,
    priority: str = "normal",
) -> dict:
    """Trigger Akasha to ingest memory data."""
    akasha_client = get_akasha_mcp_client()
    result = await akasha_client.call_tool(
        "akasha_start_ingestion",
        {"source_system": source_system, "priority": priority}
    )
    # Register workflow state
    await workflow_state_manager.create(
        workflow_id=f"akasha-ingest-{source_system}",
        task={"type": "akasha_ingest"},
    )
    return result
```

**Alternatives Considered**:
1. **Akasha fully autonomous**: ❌ (No centralized coordination)
2. **Mahavishnu micromanages**: ❌ (Tight coupling, Akasha can't evolve)

**Consequences**:
- Mahavishnu needs Akasha MCP client
- Akasha needs health check and metrics endpoints
- Both services need clear failure handling

---

## Decision 6: Oneiric Storage Integration

**Status**: ✅ Accepted

**Context**:
- Akasha needs multi-cloud storage support
- Oneiric provides universal storage adapters
- Question: How should Akasha integrate with Oneiric?

**Decision**:
Use Oneiric storage adapters for all cloud storage operations via `stack_level` for tier priority.

**Storage Adapter Configuration**:

```yaml
# config/akasha_storage.yaml
storage:
  hot:
    backend: "duckdb-memory"
    path: "/data/akasha/hot"
    write_ahead_log: true

  warm:
    backend: "duckdb-ssd"
    path: "/data/akasha/warm"

  cold:
    backend: "s3"  # or azure, gcs
    bucket: "akasha-cold-data"
    prefix: "conversations/"
    format: "parquet"

  cache:
    backend: "redis"  # Oneiric cache adapter
    host: "${REDIS_HOST:redis.cache.local}"
    db: 0
```

**Oneiric Integration Pattern**:

```python
from oneiric.core.resolution import Resolver
from oneiric.adapters.metadata import register_adapter_metadata

# Initialize Oneiric resolver
resolver = Resolver()

# Register storage adapters with stack levels
register_adapter_metadata(
    resolver,
    adapters=[
        AdapterMetadata(
            category="storage",
            provider="s3-hot",
            stack_level=100,  # Highest priority
            factory=lambda: OneiricStorageAdapter(backend="s3", config=hot_config),
            description="Hot tier storage (recent 7 days)",
        ),
        AdapterMetadata(
            category="storage",
            provider="s3-warm",
            stack_level=50,
            factory=lambda: OneiricStorageAdapter(backend="s3", config=warm_config),
            description="Warm tier storage (7-90 days)",
        ),
        AdapterMetadata(
            category="storage",
            provider="s3-cold",
            stack_level=10,
            factory=lambda: OneiricStorageAdapter(backend="s3", config=cold_config),
            description="Cold tier storage (90+ days)",
        ),
    ],
)
```

**Tier Promotion/Demotion**:

```python
async def promote_to_hot(data: bytes) -> None:
    """Promote data from warm to hot tier."""
    # Resolve hot storage (highest stack_level)
    hot_storage = await bridge.use("storage-s3-hot")

    # Upload to hot tier
    await hot_storage.instance.upload(
        bucket="akasha-hot",
        path=data.id,
        data=data
    )

async def demote_to_cold(data: bytes) -> None:
    """Demote data from warm to cold tier."""
    # Resolve cold storage (lowest stack_level)
    cold_storage = await bridge.use("storage-s3-cold")

    # Compress and upload to cold
    compressed = gzip.compress(data)
    await cold_storage.instance.upload(
        bucket="akasha-cold",
        path=data.id,
        data=compressed
    )
```

**Alternatives Considered**:
1. **Direct S3/Azure/GCS clients**: ❌ (No abstraction, vendor lock-in)
2. **Custom storage abstraction**: ❌ (Reinventing Oneiric)

**Consequences**:
- Akasha depends on Oneiric for storage
- Need to handle Oneiric adapter failures gracefully
- Must test across multiple cloud providers

---

## Decision 7: API Layer Design

**Status**: ✅ Accepted

**Context**:
- Akasha needs to serve queries to other services
- Multiple access patterns: search, analytics, trends
- Question: What API style and communication protocol?

**Decision**:
**Dual API layer**: MCP for orchestration, REST for data operations.

**API Structure**:

| API Type | Protocol | Use Case | Consumers |
|----------|----------|----------|-----------|
| **MCP** | MCP Protocol | Orchestration, health checks | Mahavishnu |
| **REST** | HTTP/JSON | Query serving, analytics | Session-Buddy, UI, scripts |
| **GraphQL** | HTTP/GraphQL | Complex queries (future) | UI dashboards |

**REST API Endpoints**:

```python
# Search
POST   /api/v1/search                          # Universal search
GET    /api/v1/search/{query_id}               # Get search results

# Analytics
GET    /api/v1/analytics/trends                # Trend analysis
GET    /api/v1/analytics/metrics               # System metrics

# Knowledge Graph
POST   /api/v1/graph/query                     # Graph queries
GET    /api/v1/graph/entities/{entity_id}      # Entity details

# Health
GET    /health                                 # Health check
GET    /metrics                                # Prometheus metrics
```

**MCP Tools**:

```python
@akasha_mcp.tool()
async def akasha_start_ingestion(
    source_system: str,
    priority: str = "normal",
) -> dict:
    """Start ingestion (called by Mahavishnu)."""

@akasha_mcp.tool()
async def akasha_health_check() -> dict:
    """Health check (called by Mahavishnu)."""

@akasha_mcp.tool()
async def akasha_get_metrics() -> dict:
    """Get metrics (called by Mahavishnu)."""
```

**Authentication & Authorization**:

```python
# JWT authentication for REST API
from fastapi.security import HTTPBearer

security = HTTPBearer()

async def verify_token(credentials: HTTPAuthorizationCredentials):
    claims = await jwt_service.verify(credentials.credentials)
    if "akasha:read" not in claims.get("scope", []):
        raise HTTPException(status_code=403, detail="Forbidden")
    return claims

# Usage
@app.post("/api/v1/search")
async def search(
    request: SearchRequest,
    claims: dict = Depends(verify_token),
):
    # Authenticated search
    ...
```

**Alternatives Considered**:
1. **MCP-only API**: ❌ (Not suitable for general HTTP clients)
2. **GraphQL-only**: ⚠️ (Overkill for initial release)

**Consequences**:
- Need to maintain two API surfaces
- REST API needs rate limiting and authentication
- MCP tools need proper error handling

---

## Decision 8: Failure Handling & Resilience

**Status**: ✅ Accepted

**Context**:
- Distributed system with multiple failure modes
- Need graceful degradation
- Question: How to handle failures at each layer?

**Decision**:
**Multi-layer resilience** with circuit breakers, retries, and graceful degradation.

**Failure Scenarios & Responses**:

| Failure | Detection | Response | Recovery |
|---------|-----------|----------|----------|
| **Akasha ingestion down** | Mahavishnu health check fails | Pause uploads, queue in S3 | Scale up pods |
| **Hot store corrupted** | Health check fails | Failover to warm tier | Restore from backup |
| **Cloud storage unavailable** | Oneiric circuit breaker | Local buffering + retry | Wait for recovery |
| **Backlog > 10,000 uploads** | Metrics alert | Scale up pods 2x | Process backlog |
| **Milvus down** | Health check fails | Fall back to DuckDB | Restart Milvus |

**Circuit Breaker Pattern**:

```python
from oneiric.core.circuit import CircuitBreaker

# Circuit breaker for S3 operations
s3_breaker = CircuitBreaker(
    failure_threshold=5,
    recovery_timeout=60,
    expected_exception=ConnectionError,
)

@s3_breaker
async def upload_to_s3(data: bytes) -> None:
    """Upload with circuit breaker protection."""
    await s3_storage.upload(data)
    # If 5 consecutive failures, circuit opens
    # Requests fail fast for 60 seconds
    # Then attempt recovery
```

**Retry with Exponential Backoff**:

```python
tenacity = __import__("tenacity")

@tenacity.retry(
    stop=tenacity.stop_after_attempt(5),
    wait=tenacity.wait_exponential(multiplier=1, max=60),
    reraise=True,
)
async def upload_with_retry(data: bytes) -> None:
    """Upload with retry logic."""
    await cold_storage.upload(data)
```

**Graceful Degradation**:

```python
async def search_with_fallback(
    query_embedding: list[float],
) -> list[dict]:
    """Search with tier fallback."""
    try:
        # Try hot tier (fastest)
        return await hot_store.search(query_embedding)
    except HotStoreUnavailable:
        try:
            # Fallback to warm tier (slower)
            logger.warning("Hot store unavailable, using warm tier")
            return await warm_store.search(query_embedding)
        except WarmStoreUnavailable:
            # Fallback to cold tier (slowest)
            logger.error("Hot and warm unavailable, using cold tier")
            return await cold_store.search(query_embedding)
```

**Alternatives Considered**:
1. **Fail fast approach**: ❌ (No graceful degradation)
2. **Full redundancy**: ❌ (Too expensive initially)

**Consequences**:
- Increased complexity with multiple fallback paths
- Need comprehensive monitoring
- Testing all failure scenarios is critical

---

## Decision 9: Partitioning Strategy

**Status**: ✅ Accepted

**Context**:
- Need efficient query pruning for 1B+ embeddings
- Both time-based and system-based queries
- Question: How to partition data for optimal performance?

**Decision**:
**Hybrid partitioning**: Composite key of `system_id` + `date` for optimal query pruning.

**Partition Structure**:

```
akasha-data/
├── embeddings/
│   ├── system-001/
│   │   ├── 2025/
│   │   │   ├── 01/
│   │   │   │   ├── 25/
│   │   │   │   │   └── batch_001.parquet
│   │   │   │   └── 26/
│   │   │   │       └── batch_002.parquet
│   │   │   └── 02/
│   │   └── 2025/
│   └── system-002/
│       └── ...
├── conversations/
│   └── ... (same structure)
└── metrics/
    └── ... (same structure)
```

**Partition Path Generator**:

```python
def get_partition_path(
    data_type: str,
    system_id: str,
    date: datetime,
) -> str:
    """Generate partition path with composite key."""
    return (
        f"{data_type}/"
        f"{system_id}/"
        f"{date.year:04d}/"
        f"{date.month:02d}/"
        f"{date.day:02d}/"
    )

# Example: embeddings/system-001/2025/01/25/batch_001.parquet
```

**Query Pruning Benefits**:

```sql
-- DuckDB automatically prunes partitions
SELECT * FROM read_parquet('embeddings/**/*.parquet')
WHERE
    system_id = 'system-001'        -- Only scans system-001 partitions
    AND date >= '2025-01-01'        -- Only scans 2025-01-* partitions
    AND date < '2025-02-01';
-- Result: 99%+ data reduction (from 1B to ~10M rows)
```

**Alternatives Considered**:
1. **Date-only partitioning**: ⚠️ (Per-system queries scan all dates)
2. **System-only partitioning**: ⚠️ (Time queries scan all systems)

**Consequences**:
- More complex path generation logic
- Need to maintain partition metadata
- Aging service must handle partition structure

---

## Decision 10: Scalability Path

**Status**: ✅ Accepted

**Context**:
- Starting with 100 systems, growing to 100,000+
- Need clear scaling triggers
- Question: When to add infrastructure/components?

**Decision**:
**Phase-based evolution** with clear scaling triggers.

**Scaling Triggers**:

| Phase | System Count | Embedding Count | Trigger Action | Add Component |
|-------|--------------|-----------------|----------------|---------------|
| **1** | 0-100 | 0-10M | Initial deployment | DuckDB-only stack |
| **2** | 100-1,000 | 10M-100M | HNSW index > RAM | Add Milvus |
| **3** | 1,000-10,000 | 100M-1B | Query latency >500ms | Add TimescaleDB read replicas |
| **4** | 10,000-100,000 | 1B+ | Edge count >100M | Add Neo4j |
| **5** | 100,000+ | 10B+ | Ops burden too high | Consider cloud-native services |

**Horizontal Scaling**:

```yaml
# Kubernetes HPA configuration
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: akasha-ingestion-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: akasha-ingestion
  minReplicas: 3
  maxReplicas: 50
  metrics:
    - type: Pods
      pods:
        metric:
          name: redis_queue_length
        target:
          type: AverageValue
          averageValue: "1000"  # Scale up if queue > 1000 per pod
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
```

**Vertical Scaling**:

| Component | Initial | Phase 2 | Phase 3+ |
|-----------|---------|---------|---------|
| **Ingestion Pods** | 2 CPU, 4GB RAM | 4 CPU, 8GB RAM | 8 CPU, 16GB RAM |
| **Hot Store** | In-memory (32GB) | NVMe SSD (512GB) | NVMe SSD (1TB) |
| **Vector Index** | In-memory HNSW | Distributed Milvus | Milvus cluster |

**Alternatives Considered**:
1. **Oversize from day 1**: ❌ (Wasteful, harder to optimize)
2. **Vertical scaling only**: ❌ (Hits ceiling quickly)

**Consequences**:
- Need monitoring for all scaling triggers
- Migration complexity when adding components
- Capacity planning required

---

## Decision 11: Data Ownership & Lifecycle

**Status**: ✅ Accepted

**Context**:
- Session-Buddy uploads to cloud storage
- Akasha ingests from cloud storage
- Question: Who owns what data? When can we delete?

**Decision**:
**Cloud storage as source of truth** with staged lifecycle.

**Data Ownership**:

| Stage | Owner | Storage | Retention | Deletion Policy |
|-------|-------|---------|-----------|-----------------|
| **Upload** | Session-Buddy | S3 incoming/ | 7 days | After successful ingestion |
| **Ingested** | Akasha | Hot tier | 7 days | Auto-migrate to warm |
| **Aged** | Akasha | Warm tier | 90 days | Auto-migrate to cold |
| **Archived** | Akasha | Cold tier | 7 years | Manual deletion only |
| **Backup** | Akasha | S3 backups | 30 days | Auto-rotation |

**Lifecycle Rules**:

```python
# S3 lifecycle policy
s3_client.put_bucket_lifecycle_configuration(
    Bucket="akasha-ingest",
    LifecycleConfiguration={
        "Rules": [
            {
                "Id": "DeleteAfterIngestion",
                "Status": "Enabled",
                "Prefix": "incoming/",
                "Expiration": {"Days": 7},  # Delete 7 days after upload
            },
            {
                "Id": "TransitionToGlacier",
                "Status": "Enabled",
                "Prefix": "conversations/",
                "Transitions": [
                    {
                        "Days": 30,
                        "StorageClass": "GLACIER"
                    }
                ]
            }
        ]
    }
)
```

**Cleanup Triggers**:

```python
# After successful ingestion, delete from incoming/
await oneiric_storage.delete(
    bucket="akasha-ingest",
    path=f"incoming/{system_id}/{upload_id}/"
)

# Verify deletion before marking as complete
exists = await oneiric_storage.exists(
    bucket="akasha-ingest",
    path=f"incoming/{system_id}/{upload_id}/"
)
assert not exists, "Failed to delete upload after ingestion"
```

**Alternatives Considered**:
1. **Akasha deletes immediately after ingestion**: ⚠️ (No recovery window)
2. **Session-Buddy owns all data**: ❌ (No separation of concerns)

**Consequences**:
- Need lifecycle management automation
- Cost of duplicate storage during 7-day window
- Must verify successful ingestion before deletion

---

## Decision 12: Monitoring & Observability

**Status**: ✅ Accepted

**Context**:
- Complex distributed system
- Need to detect issues early
- Question: What to monitor and how?

**Decision**:
**OpenTelemetry-based observability** with Prometheus metrics, structured logging, and distributed tracing.

**Key Metrics**:

| Metric | Type | Purpose | Alert Threshold |
|--------|------|---------|-----------------|
| `akasha_ingestion_total` | Counter | Total conversations ingested | - |
| `akasha_ingestion_duration_seconds` | Histogram | Ingestion latency | p99 > 5s |
| `akasha_search_total` | Counter | Total searches | - |
| `akasha_search_duration_seconds` | Histogram | Search latency | p99 > 500ms |
| `akasha_hot_store_size_bytes` | Gauge | Hot store disk usage | > 80% capacity |
| `akasha_cache_hit_rate` | Gauge | Cache effectiveness | < 50% |
| `akasha_backlog_count` | Gauge | Pending uploads | > 10,000 |

**Prometheus Metrics**:

```python
from prometheus_client import Counter, Histogram, Gauge

# Ingestion metrics
ingestion_total = Counter(
    "akasha_ingestion_total",
    "Total conversations ingested",
    ["system_id", "status"],
)

ingestion_duration_seconds = Histogram(
    "akasha_ingestion_duration_seconds",
    "Time to ingest a conversation",
    ["system_id"],
    buckets=[0.1, 0.5, 1, 2, 5, 10],  # Seconds
)

# Usage
ingestion_total.labels(system_id="system-001", status="success").inc()
ingestion_duration_seconds.labels(system_id="system-001").observe(1.23)
```

**Structured Logging**:

```python
import structlog

logger = structlog.get_logger()
logger.info(
    "ingestion_started",
    system_id=system_id,
    upload_id=upload_id,
    batch_size=len(batch),
)
```

**Distributed Tracing**:

```python
from opentelemetry import trace

tracer = trace.get_tracer(__name__)

with tracer.start_as_current_span("ingest_conversation") as span:
    span.set_attribute("system_id", system_id)
    span.set_attribute("conversation_id", conversation_id)

    # Ingestion logic
    ...
```

**Alerting Rules**:

```yaml
# Prometheus alerting rules
groups:
  - name: akasha_alerts
    rules:
      - alert: HighIngestionLatency
        expr: histogram_quantile(0.99, akasha_ingestion_duration_seconds) > 5
        for: 5m
        annotations:
          summary: "Ingestion latency p99 above 5 seconds"

      - alert: HighSearchLatency
        expr: histogram_quantile(0.99, akasha_search_duration_seconds) > 0.5
        for: 5m
        annotations:
          summary: "Search latency p99 above 500ms"

      - alert: LargeBacklog
        expr: akasha_backlog_count > 10000
        for: 5m
        annotations:
          summary: "Ingestion backlog above 10,000 uploads"
```

**Alternatives Considered**:
1. **Custom metrics**: ❌ (Reinventing wheel)
2. **Cloud-native monitoring only**: ⚠️ (Vendor lock-in)

**Consequences**:
- Need Prometheus/Grafana deployment
- Must define all metrics upfront
- Alert tuning required to avoid fatigue

---

## Summary of All Decisions

| # | Decision | Status | Impact |
|---|----------|--------|--------|
| 1 | Ingest-only pull model | ✅ | Decouples services, adds slight delay |
| 2 | Three-tier storage | ✅ | 80% cost reduction, adds aging complexity |
| 3 | Phase-based DB evolution | ✅ | Start simple, scale when needed |
| 4 | System-based sharding | ✅ | Predictable routing, hotspot risk |
| 5 | Mahavishnu orchestrates Akasha | ✅ | Centralized coordination, clear boundaries |
| 6 | Oneiric storage integration | ✅ | Multi-cloud support, Oneiric dependency |
| 7 | Dual API layer (MCP + REST) | ✅ | Flexible access, maintenance burden |
| 8 | Multi-layer resilience | ✅ | Graceful degradation, increased complexity |
| 9 | Hybrid partitioning | ✅ | Optimal query pruning, complex paths |
| 10 | Phase-based scaling | ✅ | Clear triggers, migration complexity |
| 11 | Cloud storage source of truth | ✅ | Recovery window, duplicate storage cost |
| 12 | OpenTelemetry observability | ✅ | Comprehensive monitoring, ops overhead |

---

## Implementation Roadmap

**Phase 1: Foundation** (Weeks 1-4)
- Core storage layer (DuckDB hot/warm)
- Oneiric integration for cold storage
- Basic ingestion pipeline
- MCP + REST API endpoints

**Phase 2: Advanced Features** (Weeks 5-8)
- Vector indexing (HNSW)
- Time-series aggregation (TimescaleDB)
- Knowledge graph (DuckDB + Redis)
- Distributed query execution

**Phase 3: Production Hardening** (Weeks 9-10)
- Circuit breakers and retry logic
- Comprehensive monitoring
- Load testing and optimization
- Kubernetes deployment

**Phase 4: Scale Preparation** (Weeks 11-12)
- Add Milvus for vector warm tier
- Implement tier aging service
- Cross-region replication
- Disaster recovery procedures

---

## References

- Specialist consultation: Code-Architect (Order 1A)
- Specialist consultation: Oneiric-Specialist (Order 1B)
- Specialist consultation: Orchestration-Specialist (Order 2A)
- Specialist consultation: Database Expert (Order 2B)
- Oneiric documentation: `/Users/les/Projects/oneiric/`
- Session-Buddy architecture: `/Users/les/Projects/session-buddy/CLAUDE.md`

---

**Document Version**: 1.0
**Last Updated**: 2025-01-25
**Next Review**: After Phase 1 completion (2025-02-22)
