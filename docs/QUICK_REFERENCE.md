# Akasha Oneiric Integration - Quick Reference

## TL;DR

**Akasha** = Universal memory aggregation system using **Oneiric's universal resolution layer** for pluggable, hot-swappable storage backends with automatic tier management.

## Key Design Decisions

### 1. Storage Tiers

| Tier | Backend | Data Types | Latency | Cost |
|------|---------|------------|---------|------|
| **Hot** | S3 Standard + Redis cache | Recent embeddings (< 7 days), active graphs | < 100ms | $$$$ |
| **Warm** | S3 IA + Parquet/DuckDB | Aggregated metrics, rolled-up data | < 500ms | $$ |
| **Cold** | S3 Glacier | Archives (> 90 days), logs | Hours | $ |

### 2. Data Type Storage Patterns

```python
# Vector Embeddings (FLOAT[384])
- Hot: DuckDB with HNSW index (similarity search)
- Warm: Parquet files (columnar compression)
- Cold: Compressed Parquet (Glacier)

# Time-Series Metrics
- Raw: 1-minute granularity (hot)
- Rollup 1: 5-minute averages (warm)
- Rollup 2: 1-hour averages (cold)

# Knowledge Graph
- Nodes: DuckDB table with embeddings
- Edges: Parquet partitioned by node ID
- Adjacency: Redis materialized lists (fast traversals)

# Conversations
- Text: GZIP compressed Parquet
- FTS Index: SQLite FTS5 (hot tier only)
- Archived: Columnar storage (cold)
```

### 3. Oneiric Integration Pattern

```python
# PATTERN: Oneiric's 4-tier resolution precedence
from oneiric.core.resolution import Resolver
from oneiric.adapters.metadata import register_adapter_metadata

resolver = Resolver()

register_adapter_metadata(
    resolver,
    package_name="akasha",
    adapters=[
        AdapterMetadata(
            category="storage",
            provider="s3-hot",
            stack_level=100,  # Highest priority
            factory=lambda: S3StorageAdapter(...),
        ),
        AdapterMetadata(
            category="storage",
            provider="s3-warm",
            stack_level=50,  # Medium priority
            factory=lambda: S3StorageAdapter(...),
        ),
        AdapterMetadata(
            category="storage",
            provider="s3-cold",
            stack_level=10,  # Lowest priority
            factory=lambda: S3StorageAdapter(...),
        ),
    ],
)

# Resolution: Explicit → Inferred → Stack level → Registration order
storage = resolver.resolve("storage", "s3-hot")
```

## Implementation Checklist

### Phase 1: Core Storage (Weeks 1-2)
```bash
✅ Oneiric adapter registration
✅ DuckDB vector storage with HNSW
✅ Parquet embeddings storage
✅ S3 bucket lifecycle policies
```

### Phase 2: Tier Management (Weeks 3-4)
```bash
✅ Automatic tier promotion/demotion
✅ Lifecycle hooks for data aging
✅ Tier transition orchestrator
✅ Migration jobs
```

### Phase 3: Multi-Cloud (Weeks 5-6)
```bash
✅ Azure Blob adapter
✅ GCS adapter
✅ Multi-cloud coordinator
✅ Cross-cloud replication
```

### Phase 4: Caching & Performance (Weeks 7-8)
```bash
✅ Redis cache layer
✅ Connection pooling
✅ Parallel upload/download
✅ Parquet compression optimization
```

### Phase 5: Resilience (Weeks 9-10)
```bash
✅ Circuit breakers
✅ Retry with exponential backoff
✅ Health checks
✅ Metrics collection
```

## Configuration Example

```yaml
# settings/akasha-storage.yml
storage:
  default_backend: "s3-hot"

  s3-hot:
    bucket_name: "akasha-hot-prod"
    region: "${AWS_REGION:us-east-1}"
    storage_class: "STANDARD"

  redis-cache:
    host: "${REDIS_HOST:redis.prod.internal}"
    port: 6379
    ttl_seconds: 3600

  s3-warm:
    bucket_name: "akasha-warm-prod"
    region: "${AWS_REGION:us-east-1}"
    storage_class: "STANDARD_IA"

  s3-cold:
    bucket_name: "akasha-cold-prod"
    region: "${AWS_REGION:us-east-1}"
    storage_class: "GLACIER"
```

## Code Snippets

### Register Adapters
```python
# akasha/storage/__init__.py
from oneiric.adapters.metadata import register_adapter_metadata, AdapterMetadata

def register_akasha_adapters(resolver: Resolver):
    register_adapter_metadata(
        resolver,
        package_name="akasha",
        adapters=[
            AdapterMetadata(
                category="storage",
                provider="s3-hot",
                stack_level=100,
                factory=lambda: S3StorageAdapter(...),
            ),
        ],
    )
```

### Use Storage
```python
# Use via Oneiric bridge
from oneiric.adapters.bridge import AdapterBridge

bridge = AdapterBridge(resolver=resolver, lifecycle=lifecycle)
storage = await bridge.use("storage-s3-hot")
await storage.instance.upload(bucket="akasha", path="test.txt", data=b"Hello")
```

### Tier Transitions
```python
# Automatic tier promotion/demotion
class AkashaDataLifecycle:
    async def evaluate_tier_transition(
        self, data_id: str, current_tier: str, access_metrics: dict
    ) -> str | None:
        # Hot → Warm: Age > 7 days or access count < 2
        # Warm → Cold: Age > 90 days
        # Warm → Hot: Access count >= 10
        pass
```

## Key Benefits of Oneiric

1. **Universal Resolution**: 4-tier precedence system
2. **Hot-Swapping**: Zero-downtime backend changes
3. **Domain-Agnostic**: Same patterns for all storage types
4. **Remote-Ready**: Load adapters from manifests
5. **Type Safety**: Full Pydantic validation
6. **Observability**: OpenTelemetry integration

## Files Created

```
/Users/les/Projects/akasha/docs/
├── AKASHA_STORAGE_ARCHITECTURE.md  (Complete architecture)
├── AKASHA_IMPLEMENTATION_GUIDE.md  (Step-by-step guide)
└── QUICK_REFERENCE.md              (This file)
```

## Next Steps

1. **Review Architecture**: `AKASHA_STORAGE_ARCHITECTURE.md`
2. **Follow Implementation Guide**: `AKASHA_IMPLEMENTATION_GUIDE.md`
3. **Start Coding**: Implement S3 adapter first
4. **Test Thoroughly**: Use pytest with async fixtures
5. **Deploy Gradually**: 100 → 1,000 → 10,000 systems

## Scale Requirements

- **Initial**: 100-1,000 Session-Buddy systems
- **Target**: 10,000+ systems
- **Data Types**: Vectors, time-series, graphs, text
- **Access Patterns**: Real-time search, batch analytics, archival

## Questions Answered

### Q1: What Oneiric patterns for each data type?
- **Vectors**: DuckDB (hot) → Parquet (warm) → Compressed (cold)
- **Time-series**: Raw (hot) → 5min rollups (warm) → 1hour rollups (cold)
- **Graphs**: DuckDB nodes + Redis adjacency (hot) → Parquet edges (warm)
- **Text**: SQLite FTS5 (hot) → GZIP Parquet (cold)

### Q2: How to implement multi-tier storage?
- Use Oneiric's `stack_level` for tier priority
- Implement lifecycle hooks for automatic transitions
- Create promotion/demotion policies based on age + access count

### Q3: Best practices for Oneiric config at scale?
- Use environment variables for deployment-specific settings
- Validate configs with Pydantic models
- Register adapters with proper `stack_level` values
- Use Oneiric's `AdapterBridge` for consistent access patterns

### Q4: How to handle backend failures and fallbacks?
- Circuit breakers for automatic failure detection
- Retry with exponential backoff for transient errors
- Multi-cloud coordinator for cross-cloud redundancy
- Health checks with automatic promotion of secondary backends

---

**Summary**: Akasha leverages Oneiric's universal resolution layer to provide a scalable, resilient, multi-tier storage system that can handle 10,000+ Session-Buddy systems with automatic lifecycle management, cross-cloud redundancy, and seamless hot-swapping of storage backends.
