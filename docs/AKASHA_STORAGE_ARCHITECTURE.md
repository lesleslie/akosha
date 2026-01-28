# Akasha Universal Memory Aggregation System - Storage Architecture

**Project**: Akasha (आकाश)
**Version**: 1.0.0
**Date**: 2025-01-25
**Architecture**: Oneiric-based Multi-Tier Storage System

## Executive Summary

Akasha is a universal memory aggregation system designed to ingest and store massive amounts of data from 100-10,000+ Session-Buddy systems. This document defines the comprehensive Oneiric-based storage architecture that provides tiered storage, cross-cloud redundancy, and seamless scalability.

### Scale Requirements

- **Initial**: 100-1,000 Session-Buddy systems
- **Target**: 10,000+ systems
- **Data Types**: Vector embeddings (FLOAT[384]), time-series metrics, knowledge graphs, conversation text
- **Access Patterns**: Real-time search, batch analytics, archival retention

---

## 1. Oneiric Architecture Foundation

### 1.1 Core Oneiric Concepts

Akasha leverages Oneiric's universal resolution layer with four key patterns:

```python
# PATTERN: Oneiric's 4-tier resolution precedence system
from oneiric.core.resolution import Resolver, Candidate
from oneiric.core.lifecycle import LifecycleManager
from oneiric.adapters.bridge import AdapterBridge

# Initialize resolver with 4-tier precedence
resolver = Resolver()

# Tier 1: Explicit override (highest priority)
# Tier 2: Inferred priority (from ONEIRIC_STACK_ORDER env var)
# Tier 3: Stack level (Z-index style layering)
# Tier 4: Registration order (last registered wins)

# Register storage adapters via metadata helper
from oneiric.adapters.metadata import register_adapter_metadata

register_adapter_metadata(
    resolver,
    package_name="akasha",
    package_path=__file__,
    adapters=[
        AdapterMetadata(
            category="storage",
            provider="s3-hot",  # Hot tier: S3 Standard
            stack_level=100,  # Highest priority for hot data
            factory=lambda: S3StorageAdapter(tier="hot"),
            description="Hot tier storage for frequently accessed data",
        ),
        AdapterMetadata(
            category="storage",
            provider="s3-warm",  # Warm tier: S3 Infrequent Access
            stack_level=50,  # Medium priority
            factory=lambda: S3StorageAdapter(tier="warm"),
            description="Warm tier storage for aggregated analytics",
        ),
        AdapterMetadata(
            category="storage",
            provider="s3-cold",  # Cold tier: S3 Glacier
            stack_level=10,  # Lowest priority
            factory=lambda: S3StorageAdapter(tier="cold"),
            description="Cold tier storage for long-term archival",
        ),
    ],
)
```

### 1.2 Domain-Agnostic Bridge Pattern

```python
# PATTERN: All Oneiric domains use the same bridge pattern
from oneiric.domains import AdapterBridge

storage_bridge = AdapterBridge(
    resolver=resolver,
    lifecycle=lifecycle,
    settings=Settings.load_yaml("settings/storage.yml")
)

# Use storage adapter (automatic resolution + lifecycle)
handle = await storage_bridge.use("storage")
await handle.instance.store(
    bucket="akasha-vectors",
    path="conversations/2025-01-25/embedding_12345.parquet",
    data=embedding_bytes
)
```

---

## 2. Multi-Tier Storage Strategy

### 2.1 Tier Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         AKASHA STORAGE LAYER                    │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  HOT TIER (Fastest, Most Expensive)                             │
│  ├─ Backend: S3 Standard / Azure Blob Hot / GCS Standard       │
│  ├─ Data: Recent embeddings (< 7 days), active knowledge graphs │
│  ├─ Cache: Redis for sub-ms queries                             │
│  └─ Latency: < 100ms (p99)                                      │
│                                                                  │
│  WARM TIER (Medium Speed, Medium Cost)                          │
│  ├─ Backend: S3 IA / Azure Blob Cool / GCS Nearline            │
│  ├─ Data: Aggregated metrics, rolled-up embeddings             │
│  ├─ Format: Parquet files with columnar compression            │
│  └─ Latency: < 500ms (p99)                                      │
│                                                                  │
│  COLD TIER (Slowest, Cheapest)                                  │
│  ├─ Backend: S3 Glacier / Azure Archive / GCS Coldline         │
│  ├─ Data: Historical conversations (> 90 days), compressed logs │
│  ├─ Retrieval: 3-12 hours restore time                          │
│  └─ Latency: Hours (acceptable for archival)                    │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 Data Lifecycle Management

```python
# PATTERN: Automatic tier promotion/demotion with Oneiric lifecycle hooks
from oneiric.core.lifecycle import LifecycleManager

class AkashaDataLifecycle:
    """Manages data movement across storage tiers."""

    def __init__(self, lifecycle: LifecycleManager):
        self.lifecycle = lifecycle
        self.tier_policies = {
            "hot": {"max_age_days": 7, "access_count_min": 10},
            "warm": {"max_age_days": 90, "access_count_min": 2},
            "cold": {"max_age_days": None, "access_count_min": 0},
        }

    async def evaluate_tier_transition(
        self,
        data_id: str,
        current_tier: str,
        access_metrics: dict[str, Any]
    ) -> str | None:
        """Evaluate if data should move to a different tier.

        Returns:
            Target tier ("hot", "warm", "cold") or None if no transition needed
        """
        policy = self.tier_policies[current_tier]
        age_days = self._calculate_age_days(data_id)
        access_count = access_metrics.get("count", 0)

        # Promote: Frequently accessed recent data
        if current_tier == "warm" and access_count >= policy["access_count_min"]:
            return "hot" if age_days < 7 else None

        # Demote: Old or infrequently accessed data
        if current_tier == "hot" and (age_days > 7 or access_count < 2):
            return "warm"

        if current_tier == "warm" and age_days > 90:
            return "cold"

        return None

    async def execute_tier_transition(
        self,
        data_id: str,
        source_tier: str,
        target_tier: str
    ) -> bool:
        """Execute storage tier transition with Oneiric lifecycle."""
        try:
            # 1. Resolve source and target adapters
            source_handle = await storage_bridge.use(f"storage-{source_tier}")
            target_handle = await storage_bridge.use(f"storage-{target_tier}")

            # 2. Download from source
            source_path = f"data/{data_id}"
            data = await source_handle.instance.download(
                bucket="akasha-unified",
                path=source_path
            )

            # 3. Upload to target
            await target_handle.instance.upload(
                bucket="akasha-unified",
                path=source_path,
                data=data
            )

            # 4. Delete from source (after verification)
            await source_handle.instance.delete(
                bucket="akasha-unified",
                path=source_path
            )

            # 5. Update metadata
            await self._update_tier_metadata(data_id, target_tier)

            logger.info(
                "tier-transition-complete",
                data_id=data_id,
                source_tier=source_tier,
                target_tier=target_tier,
            )
            return True

        except Exception as e:
            logger.error(f"Tier transition failed: {e}")
            return False
```

---

## 3. Backend Selection & Configuration

### 3.1 Recommended Backend Combinations

#### Option 1: AWS-Native (Recommended for AWS deployments)

```yaml
# settings/akasha-storage.yml
storage:
  default_backend: "s3-hot"

  s3-hot:
    provider: "s3"
    bucket_name: "${AKASHA_HOT_BUCKET:akasha-hot-${env}}"
    region: "${AWS_REGION:us-east-1}"
    storage_class: "STANDARD"  # S3 Standard
    tier: "hot"
    # Lifecycle rules
    lifecycle_rules:
      - id: "hot-to-warm"
        transition_days: 7
        storage_class: "STANDARD_IA"
      - id: "warm-to-cold"
        transition_days: 90
        storage_class: "GLACIER"

  s3-warm:
    provider: "s3"
    bucket_name: "${AKASHA_WARM_BUCKET:akasha-warm-${env}}"
    region: "${AWS_REGION:us-east-1}"
    storage_class: "STANDARD_IA"  # Infrequent Access
    tier: "warm"

  s3-cold:
    provider: "s3"
    bucket_name: "${AKASHA_COLD_BUCKET:akasha-cold-${env}}"
    region: "${AWS_REGION:us-east-1}"
    storage_class: "GLACIER"
    tier: "cold"

  # Redis cache for hot tier acceleration
  redis-cache:
    provider: "redis"
    host: "${REDIS_HOST:localhost}"
    port: 6379
    db: 0
    ttl_seconds: 3600  # 1 hour cache TTL
```

#### Option 2: Multi-Cloud (Best for redundancy)

```yaml
storage:
  # Primary: AWS S3
  s3-primary:
    provider: "s3"
    bucket_name: "akasha-primary"
    region: "us-east-1"
    storage_class: "STANDARD"

  # Secondary: Azure Blob (cross-cloud redundancy)
  azure-secondary:
    provider: "azure"
    container_name: "akasha-secondary"
    storage_class: "HOT"
    account_name: "${AZURE_STORAGE_ACCOUNT}"

  # Tertiary: GCS (third region)
  gcs-tertiary:
    provider: "gcs"
    bucket_name: "akasha-tertiary"
    storage_class: "STANDARD"
    project_id: "${GCP_PROJECT_ID}"
```

#### Option 3: Hybrid Cloud (On-prem + Cloud)

```yaml
storage:
  # On-premises local storage (for sensitive data)
  file-onprem:
    provider: "file"
    local_path: "/data/akasha/onprem"
    tier: "hot"
    # Data residency compliance

  # Cloud backup (for non-sensitive data)
  s3-cloud:
    provider: "s3"
    bucket_name: "akasha-cloud-backup"
    region: "us-east-1"
    tier: "warm"
    # Cost optimization
```

### 3.2 Oneiric Adapter Configuration

```python
# akasha/storage/config.py
from pathlib import Path
from oneiric.core.config import Settings

class AkashaStorageSettings:
    """Centralized storage configuration for Akasha."""

    @staticmethod
    def from_settings() -> Settings:
        """Load storage settings from Oneiric config files.

        Searches in order:
        1. settings/akasha-storage.yml (project-specific)
        2. ~/.config/oneiric/settings/storage.yml (user-specific)
        3. Environment variables (highest priority)
        """
        config_path = Path("settings/akasha-storage.yml")
        if config_path.exists():
            return Settings.load_yaml(config_path)

        # Fallback to user config
        user_config = Path.home() / ".config" / "oneiric" / "settings" / "storage.yml"
        if user_config.exists():
            return Settings.load_yaml(user_config)

        # Default configuration
        return Settings.load_yaml(Path("settings/default-storage.yml"))

    @staticmethod
    def get_storage_adapter(tier: str = "hot") -> Any:
        """Get Oneiric storage adapter for specific tier.

        Args:
            tier: Storage tier ("hot", "warm", "cold")

        Returns:
            Configured Oneiric storage adapter instance
        """
        from oneiric.adapters import AdapterBridge
        from oneiric.core.resolution import Resolver
        from oneiric.core.lifecycle import LifecycleManager

        resolver = Resolver()
        lifecycle = LifecycleManager(resolver)

        # Register Akasha storage adapters
        AkashaStorageSettings._register_adapters(resolver)

        # Create bridge
        bridge = AdapterBridge(
            resolver=resolver,
            lifecycle=lifecycle,
            settings=AkashaStorageSettings.from_settings()
        )

        return bridge.use(f"storage-{tier}")

    @staticmethod
    def _register_adapters(resolver: Resolver) -> None:
        """Register all storage adapters with Oneiric resolver."""
        from oneiric.adapters.metadata import register_adapter_metadata, AdapterMetadata

        register_adapter_metadata(
            resolver,
            package_name="akasha",
            package_path=__file__,
            adapters=[
                # Hot tier adapters
                AdapterMetadata(
                    category="storage",
                    provider="s3-hot",
                    stack_level=100,
                    factory=lambda: S3StorageAdapter(
                        bucket_name=os.getenv("AKASHA_HOT_BUCKET"),
                        storage_class="STANDARD",
                    ),
                    description="S3 Standard storage for hot tier",
                ),
                AdapterMetadata(
                    category="storage",
                    provider="redis-cache",
                    stack_level=100,  # Same level as S3 hot (cache layer)
                    factory=lambda: RedisCacheAdapter(
                        host=os.getenv("REDIS_HOST", "localhost"),
                        port=6379,
                        ttl=3600,
                    ),
                    description="Redis cache for hot data acceleration",
                ),

                # Warm tier adapters
                AdapterMetadata(
                    category="storage",
                    provider="s3-warm",
                    stack_level=50,
                    factory=lambda: S3StorageAdapter(
                        bucket_name=os.getenv("AKASHA_WARM_BUCKET"),
                        storage_class="STANDARD_IA",
                    ),
                    description="S3 Infrequent Access for warm tier",
                ),

                # Cold tier adapters
                AdapterMetadata(
                    category="storage",
                    provider="s3-cold",
                    stack_level=10,
                    factory=lambda: S3StorageAdapter(
                        bucket_name=os.getenv("AKASHA_COLD_BUCKET"),
                        storage_class="GLACIER",
                    ),
                    description="S3 Glacier for cold tier",
                ),
            ],
        )
```

---

## 4. Data Type Storage Patterns

### 4.1 Vector Embeddings (FLOAT[384])

```python
# PATTERN: Columnar storage for vector embeddings with Parquet + DuckDB
import pyarrow as pa
import pyarrow.parquet as pq
from typing import Any

class VectorEmbeddingStorage:
    """High-performance storage for FLOAT[384] vector embeddings.

    Strategy:
    - Format: Parquet files with columnar compression (Snappy)
    - Partitioning: By date (YYYY/MM/DD) and source system
    - Indexing: HNSW vector index on DuckDB for fast similarity search
    - Lifecycle: Hot (7 days) → Warm (90 days) → Cold (archival)
    """

    def __init__(self, storage_adapter: Any):
        self.storage = storage_adapter
        self.embedding_dim = 384  # all-MiniLM-L6-v2

    async def store_embeddings_batch(
        self,
        embeddings: list[dict[str, Any]],
        source_system: str,
        date: str,
    ) -> str:
        """Store a batch of embeddings to Parquet file.

        Args:
            embeddings: List of embedding records
            source_system: Session-Buddy system identifier
            date: Date partition (YYYY-MM-DD)

        Returns:
            Storage path for the uploaded file
        """
        # Convert to PyArrow Table (columnar format)
        schema = pa.schema([
            ('id', pa.string()),
            ('content', pa.string()),
            ('embedding', pa.list_(pa.float32(), self.embedding_dim)),
            ('metadata', pa.string()),
            ('created_at', pa.timestamp('ns')),
        ])

        # Build arrays
        ids = [e["id"] for e in embeddings]
        contents = [e["content"] for e in embeddings]
        embedding_arrays = [e["embedding"] for e in embeddings]
        metadata_jsons = [json.dumps(e.get("metadata", {})) for e in embeddings]
        created_ats = [e["created_at"] for e in embeddings]

        table = pa.Table.from_arrays(
            [
                pa.array(ids),
                pa.array(contents),
                pa.array(embedding_arrays),
                pa.array(metadata_jsons),
                pa.array(created_ats),
            ],
            schema=schema,
        )

        # Write to Parquet with compression
        buffer = pa.BufferOutputStream()
        pq.write_table(
            table,
            buffer,
            compression='snappy',  # Fast compression for hot tier
            row_group_size=10000,  # Optimal for vector scans
        )

        # Generate storage path
        path = f"embeddings/{source_system}/{date}/batch_{uuid.uuid4()}.parquet"

        # Upload to storage (using Oneiric adapter)
        await self.storage.upload(
            bucket="akasha-vectors",
            path=path,
            data=buffer.getvalue().to_pybytes(),
        )

        logger.info(
            "embeddings-stored",
            count=len(embeddings),
            path=path,
            size_bytes=buffer.size(),
        )
        return path

    async def search_similar(
        self,
        query_embedding: list[float],
        source_systems: list[str] | None = None,
        limit: int = 100,
        tier: str = "hot",
    ) -> list[dict[str, Any]]:
        """Search for similar embeddings using DuckDB vector similarity.

        Uses Oneiric's DuckDB adapter for HNSW-accelerated vector search.
        """
        # Resolve DuckDB adapter
        from oneiric.adapters.database.duckdb import DuckDBAdapter

        db = DuckDBAdapter(
            database_path=":memory:",  # In-memory for fast queries
            read_only=False,
        )
        await db.initialize()

        # Scan Parquet files from storage tier
        scan_paths = await self._get_scan_paths(source_systems, tier)

        # Load Parquet files into DuckDB
        for path in scan_paths:
            await db.execute(f"""
                CREATE OR REPLACE VIEW embeddings_view AS
                SELECT * FROM read_parquet('s3://akasha-vectors/{path}')
            """)

        # Vector similarity search with HNSW index
        query_vector = str(query_embedding)
        results = await db.execute(f"""
            SELECT
                id, content, metadata, created_at,
                array_cosine_similarity(embedding, '{query_vector}'::FLOAT[384]) as similarity
            FROM embeddings_view
            WHERE embedding IS NOT NULL
            ORDER BY similarity DESC
            LIMIT {limit}
        """)

        return results
```

### 4.2 Time-Series Metrics Storage

```python
# PATTERN: Time-series optimized storage with rollup aggregation
class TimeSeriesMetricsStorage:
    """Storage for time-series metrics with automatic rollup.

    Strategy:
    - Raw data: 1-minute granularity (hot tier)
    - Rollup 1: 5-minute averages (warm tier)
    - Rollup 2: 1-hour averages (cold tier)
    - Format: Column-oriented with time partitioning
    """

    async def store_metric(
        self,
        metric_name: str,
        value: float,
        timestamp: datetime,
        tags: dict[str, str] | None = None,
    ) -> None:
        """Store a single metric point."""
        # Append to in-memory buffer (batch writes)
        await self._buffer.append({
            "metric": metric_name,
            "value": value,
            "timestamp": timestamp,
            "tags": tags or {},
        })

        # Flush buffer when full (optimizes write throughput)
        if len(self._buffer) >= 1000:
            await self._flush_buffer()

    async def query_metrics(
        self,
        metric_name: str,
        start_time: datetime,
        end_time: datetime,
        granularity: str = "1m",
    ) -> list[dict[str, Any]]:
        """Query metrics with automatic granularity selection.

        Automatically selects appropriate tier and rollup level based on time range.
        """
        time_range_hours = (end_time - start_time).total_seconds() / 3600

        # Select granularity based on time range
        if time_range_hours < 24:
            # Use raw 1-minute data from hot tier
            tier = "hot"
            rollup = "raw"
        elif time_range_hours < 720:  # 30 days
            # Use 5-minute rollup from warm tier
            tier = "warm"
            rollup = "5m"
        else:
            # Use 1-hour rollup from cold tier
            tier = "cold"
            rollup = "1h"

        # Query appropriate tier
        storage = await storage_bridge.use(f"storage-{tier}")
        data = await storage.instance.query_timeseries(
            metric=metric_name,
            start=start_time,
            end=end_time,
            rollup=rollup,
        )

        return data
```

### 4.3 Knowledge Graph Storage

```python
# PATTERN: Hybrid storage for graph data (edges + adjacency lists)
class KnowledgeGraphStorage:
    """Storage for knowledge graph with hybrid structure.

    Strategy:
    - Nodes: DuckDB table with vector embeddings
    - Edges: Parquet files partitioned by node ID
    - Adjacency lists: Materialized in Redis for fast traversals
    - Full graph dumps: Parquet files for batch analytics
    """

    def __init__(self):
        self.duckdb = None  # DuckDB adapter
        self.redis = None   # Redis adapter (for hot adjacency lists)

    async def store_edge(
        self,
        source_id: str,
        target_id: str,
        edge_type: str,
        properties: dict[str, Any],
    ) -> None:
        """Store a graph edge."""
        # 1. Store in DuckDB (persistent storage)
        await self.duckdb.execute("""
            INSERT INTO kg_edges (source_id, target_id, edge_type, properties)
            VALUES (?, ?, ?, ?)
        """, [source_id, target_id, edge_type, json.dumps(properties)])

        # 2. Update adjacency list in Redis (fast access cache)
        redis_key = f"adj:{source_id}"
        await self.redis.zadd(
            redis_key,
            {target_id: properties.get("weight", 1.0)}
        )

        # 3. Set TTL on adjacency list (auto-refresh on access)
        await self.redis.expire(redis_key, 86400)  # 1 day

    async def query_neighbors(
        self,
        node_id: str,
        edge_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query neighbors of a node (fast path via Redis)."""
        # Try Redis first (hot tier)
        redis_key = f"adj:{node_id}"
        neighbors = await self.redis.zrevrange(
            redis_key,
            0,
            limit - 1,
            withscores=True,
        )

        if neighbors:
            # Cache hit - return immediately
            return [
                {"node_id": n[0], "weight": n[1]}
                for n in neighbors
            ]

        # Cache miss - query DuckDB (warm tier)
        query = """
            SELECT target_id, properties
            FROM kg_edges
            WHERE source_id = ?
        """
        params = [node_id]

        if edge_type:
            query += " AND edge_type = ?"
            params.append(edge_type)

        query += f" LIMIT {limit}"

        results = await self.duckdb.execute(query, params)

        # Populate Redis cache for future queries
        pipe = self.redis.pipeline()
        for row in results:
            target_id = row[0]
            weight = json.loads(row[1]).get("weight", 1.0)
            pipe.zadd(redis_key, {target_id: weight})
        pipe.expire(redis_key, 86400)
        await pipe.execute()

        return results
```

### 4.4 Conversation Text Storage

```python
# PATTERN: Full-text search optimized storage with FTS5
class ConversationTextStorage:
    """Storage for conversation text with full-text search.

    Strategy:
    - Text: GZIP compressed in Parquet files
    - FTS Index: SQLite FTS5 full-text index (hot tier)
    - Archived: Columnar storage without FTS (cold tier)
    """

    async def store_conversation(
        self,
        conversation_id: str,
        content: str,
        metadata: dict[str, Any],
    ) -> None:
        """Store conversation with full-text indexing."""
        # 1. Store compressed text in Parquet
        compressed_content = gzip.compress(content.encode('utf-8'))

        await self.storage.upload(
            bucket="akasha-conversations",
            path=f"conversations/{conversation_id}.parquet",
            data=self._make_parquet({
                "id": conversation_id,
                "content_compressed": compressed_content,
                "metadata": json.dumps(metadata),
                "created_at": datetime.now(UTC),
            }),
        )

        # 2. Update FTS index (hot tier only)
        await self._update_fts_index(conversation_id, content, metadata)

    async def search_text(
        self,
        query: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Full-text search across conversations."""
        # Try FTS index first (hot tier)
        fts_results = await self._search_fts(query, limit)
        if fts_results:
            return fts_results

        # Fallback to Parquet scan (warm/cold tier)
        return await self._scan_parquet(query, limit)
```

---

## 5. Integration Architecture

### 5.1 Akasha Storage Adapter Registration

```python
# akasha/storage/__init__.py
from oneiric.adapters.metadata import register_adapter_metadata, AdapterMetadata
from oneiric.core.resolution import Resolver

def register_akasha_storage_adapters(resolver: Resolver) -> None:
    """Register all Akasha storage adapters with Oneiric resolver.

    This enables Akasha to leverage Oneiric's 4-tier resolution system,
    lifecycle management, and hot-swapping capabilities.
    """
    register_adapter_metadata(
        resolver,
        package_name="akasha",
        package_path=__file__,
        adapters=[
            # ==========================================
            # HOT TIER ADAPTERS (stack_level=100)
            # ==========================================

            AdapterMetadata(
                category="storage",
                provider="s3-hot",
                stack_level=100,
                factory=lambda: S3HotStorageAdapter(
                    bucket_name=os.getenv("AKASHA_HOT_BUCKET"),
                    region=os.getenv("AWS_REGION", "us-east-1"),
                    storage_class="STANDARD",
                ),
                description="S3 Standard storage for hot tier (frequent access)",
            ),

            AdapterMetadata(
                category="cache",
                provider="redis-hot",
                stack_level=100,
                factory=lambda: RedisCacheAdapter(
                    host=os.getenv("REDIS_HOST", "localhost"),
                    port=6379,
                    db=0,
                    ttl_seconds=3600,
                ),
                description="Redis cache for hot data acceleration",
            ),

            AdapterMetadata(
                category="database",
                provider="duckdb-hot",
                stack_level=100,
                factory=lambda: DuckDBAdapter(
                    database_path=":memory:",  # In-memory for fast queries
                    read_only=False,
                ),
                description="In-memory DuckDB for vector similarity search",
            ),

            # ==========================================
            # WARM TIER ADAPTERS (stack_level=50)
            # ==========================================

            AdapterMetadata(
                category="storage",
                provider="s3-warm",
                stack_level=50,
                factory=lambda: S3WarmStorageAdapter(
                    bucket_name=os.getenv("AKASHA_WARM_BUCKET"),
                    region=os.getenv("AWS_REGION", "us-east-1"),
                    storage_class="STANDARD_IA",
                ),
                description="S3 Infrequent Access for warm tier (analytics)",
            ),

            AdapterMetadata(
                category="database",
                provider="duckdb-warm",
                stack_level=50,
                factory=lambda: DuckDBAdapter(
                    database_path="/data/akasha/warm.duckdb",
                    read_only=False,
                ),
                description="Disk-based DuckDB for warm tier analytics",
            ),

            # ==========================================
            # COLD TIER ADAPTERS (stack_level=10)
            # ==========================================

            AdapterMetadata(
                category="storage",
                provider="s3-cold",
                stack_level=10,
                factory=lambda: S3ColdStorageAdapter(
                    bucket_name=os.getenv("AKASHA_COLD_BUCKET"),
                    region=os.getenv("AWS_REGION", "us-east-1"),
                    storage_class="GLACIER",
                ),
                description="S3 Glacier for cold tier (archival)",
            ),

            AdapterMetadata(
                category="storage",
                provider="azure-cold",
                stack_level=10,  # Same level as S3 cold (backup)
                factory=lambda: AzureColdStorageAdapter(
                    container_name=os.getenv("AZURE_CONTAINER"),
                    account_name=os.getenv("AZURE_STORAGE_ACCOUNT"),
                ),
                description="Azure Archive for cross-cloud redundancy",
            ),
        ],
    )
```

### 5.2 Adapter Lifecycle Management

```python
# akasha/storage/lifecycle.py
from oneiric.core.lifecycle import LifecycleManager
from oneiric.adapters.bridge import AdapterBridge

class AkashaStorageLifecycle:
    """Manages storage adapter lifecycle with hot-swapping.

    Enables:
    - Zero-downtime configuration changes
    - Automatic failover on backend failures
    - Graceful degradation when backends are unavailable
    """

    def __init__(self, resolver: Resolver):
        self.resolver = resolver
        self.lifecycle = LifecycleManager(resolver)
        self.bridge = AdapterBridge(
            resolver=resolver,
            lifecycle=lifecycle,
            settings=Settings.load_yaml("settings/akasha-storage.yml")
        )

    async def get_storage_adapter(
        self,
        tier: str,
        auto_failover: bool = True,
    ) -> Any:
        """Get storage adapter with automatic failover.

        Args:
            tier: Storage tier ("hot", "warm", "cold")
            auto_failover: If True, automatically falls back to lower tier on failure

        Returns:
            Active storage adapter instance

        Raises:
            RuntimeError: If all backends fail
        """
        try:
            # Try primary adapter for tier
            handle = await self.bridge.use(f"storage-{tier}")
            return handle.instance

        except Exception as e:
            logger.warning(f"Primary {tier} adapter failed: {e}")

            if not auto_failover:
                raise

            # Failover to next lower tier
            tier_fallback_order = {"hot": "warm", "warm": "cold", "cold": None}
            fallback_tier = tier_fallback_order.get(tier)

            if fallback_tier:
                logger.info(f"Failing over from {tier} to {fallback_tier}")
                return await self.get_storage_adapter(fallback_tier, auto_failover)

            raise RuntimeError("All storage backends failed")

    async def swap_backend(
        self,
        tier: str,
        new_backend: str,
        force: bool = False,
    ) -> bool:
        """Hot-swap storage backend with automatic rollback on failure.

        Args:
            tier: Storage tier to swap
            new_backend: New backend provider (e.g., "s3-hot" → "azure-hot")
            force: Force swap even if health check fails

        Returns:
            True if swap succeeded, False otherwise
        """
        try:
            # Perform swap via Oneiric lifecycle
            await self.lifecycle.swap(
                domain="storage",
                key=f"storage-{tier}",
                provider=new_backend,
                force=force,
            )

            logger.info(
                "backend-swap-success",
                tier=tier,
                new_backend=new_backend,
            )
            return True

        except Exception as e:
            logger.error(f"Backend swap failed: {e}")
            return False
```

---

## 6. Multi-Backend Coordination

### 6.1 Cross-Cloud Redundancy Pattern

```python
# PATTERN: Multi-cloud write with read preference
class MultiCloudCoordinator:
    """Coordinates multi-cloud storage with automatic failover.

    Strategy:
    - Writes: Synchronous write to primary, async replication to secondary/tertiary
    - Reads: Read from nearest/healthiest backend with preference order
    - Failover: Automatic promotion of secondary if primary fails
    """

    def __init__(self):
        self.primary_region = os.getenv("AKASHA_PRIMARY_REGION", "us-east-1")
        self.backend_health: dict[str, bool] = {}

    async def write_with_replication(
        self,
        bucket: str,
        path: str,
        data: bytes,
        replication_factor: int = 3,
    ) -> bool:
        """Write data with multi-cloud replication.

        Args:
            bucket: Storage bucket name
            path: Object path
            data: Data to write
            replication_factor: Number of replicas (1-3)

        Returns:
            True if write succeeded to at least primary
        """
        # 1. Synchronous write to primary
        primary = await storage_bridge.use("storage-s3-primary")
        try:
            await primary.instance.upload(bucket, path, data)
            self.backend_health["s3-primary"] = True
        except Exception as e:
            logger.error(f"Primary write failed: {e}")
            self.backend_health["s3-primary"] = False
            return False

        # 2. Async replication to secondary/tertiary (fire-and-forget)
        if replication_factor >= 2:
            asyncio.create_task(
                self._replicate_to_secondary(bucket, path, data)
            )

        if replication_factor >= 3:
            asyncio.create_task(
                self._replicate_to_tertiary(bucket, path, data)
            )

        return True

    async def read_from_healthy_backend(
        self,
        bucket: str,
        path: str,
    ) -> bytes | None:
        """Read data from healthiest available backend.

        Tries backends in preference order:
        1. Primary (same region)
        2. Secondary (different region)
        3. Tertiary (different cloud provider)
        """
        backends = ["s3-primary", "azure-secondary", "gcs-tertiary"]

        for backend_name in backends:
            # Check backend health
            if not self.backend_health.get(backend_name, True):
                continue

            try:
                backend = await storage_bridge.use(f"storage-{backend_name}")
                data = await backend.instance.download(bucket, path)
                logger.info(f"Read from {backend_name} succeeded")
                return data

            except Exception as e:
                logger.warning(f"Read from {backend_name} failed: {e}")
                self.backend_health[backend_name] = False

        logger.error("All backends failed for read")
        return None
```

### 6.2 Hybrid Cloud Pattern

```python
# PATTERN: Hybrid cloud with data residency control
class HybridCloudStorage:
    """Hybrid cloud storage with automatic data classification.

    Strategy:
    - Sensitive data: On-premises storage (data residency compliance)
    - Public data: Cloud storage (cost optimization)
    - Automatic classification: Regex + ML-based data detection
    """

    def __init__(self):
        self.onprem_adapter = None  # Local file storage
        self.cloud_adapter = None   # S3 storage
        self.classifier = DataClassifier()

    async def store_with_classification(
        self,
        data_id: str,
        data: bytes,
        metadata: dict[str, Any],
    ) -> str:
        """Store data after automatic classification."""
        # Classify data sensitivity
        sensitivity = await self.classifier.classify(data, metadata)

        if sensitivity == "sensitive":
            # Store on-premises (data residency)
            path = f"sensitive/{data_id}"
            await self.onprem_adapter.upload(
                bucket="akasha-onprem",
                path=path,
                data=data,
            )
            logger.info(f"Stored sensitive data on-prem: {path}")
            return path

        else:
            # Store in cloud (cost optimization)
            path = f"public/{data_id}"
            await self.cloud_adapter.upload(
                bucket="akasha-cloud",
                path=path,
                data=data,
            )
            logger.info(f"Stored public data in cloud: {path}")
            return path
```

---

## 7. Failure Handling & Fallbacks

### 7.1 Circuit Breaker Pattern

```python
# PATTERN: Circuit breaker for failing storage backends
from enum import Enum
from dataclasses import dataclass

class CircuitState(Enum):
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing if backend recovered

@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5  # Open circuit after N failures
    success_threshold: int = 2  # Close circuit after N successes
    timeout_seconds: int = 60   # Wait before trying again

class StorageCircuitBreaker:
    """Circuit breaker for storage backend failures."""

    def __init__(self, backend_name: str, config: CircuitBreakerConfig):
        self.backend_name = backend_name
        self.config = config
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = None

    async def execute(
        self,
        operation: t.Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Execute operation with circuit breaker protection.

        Raises:
            RuntimeError: If circuit is OPEN (backend is failing)
        """
        # Check if circuit should reset to HALF_OPEN
        if self.state == CircuitState.OPEN:
            if time.time() - self.last_failure_time > self.config.timeout_seconds:
                logger.info(f"Circuit breaker HALF_OPEN for {self.backend_name}")
                self.state = CircuitState.HALF_OPEN
            else:
                raise RuntimeError(
                    f"Circuit breaker OPEN for {self.backend_name} "
                    f"(too many failures)"
                )

        # Execute operation
        try:
            result = await operation(*args, **kwargs)

            # Success - update counters
            self.success_count += 1
            if self.state == CircuitState.HALF_OPEN:
                if self.success_count >= self.config.success_threshold:
                    logger.info(f"Circuit breaker CLOSED for {self.backend_name}")
                    self.state = CircuitState.CLOSED
                    self.failure_count = 0

            return result

        except Exception as e:
            # Failure - update counters
            self.failure_count += 1
            self.last_failure_time = time.time()

            if self.failure_count >= self.config.failure_threshold:
                logger.error(
                    f"Circuit breaker OPEN for {self.backend_name} "
                    f"({self.failure_count} failures)"
                )
                self.state = CircuitState.OPEN

            raise e
```

### 7.2 Retry with Exponential Backoff

```python
# PATTERN: Resilient storage operations with automatic retries
class ResilientStorageClient:
    """Storage client with automatic retry and exponential backoff."""

    def __init__(self, storage_adapter: Any):
        self.storage = storage_adapter
        self.circuit_breaker = StorageCircuitBreaker(
            backend_name=storage_adapter.__class__.__name__,
            config=CircuitBreakerConfig(
                failure_threshold=5,
                success_threshold=2,
                timeout_seconds=60,
            ),
        )

    async def upload_with_retry(
        self,
        bucket: str,
        path: str,
        data: bytes,
        max_retries: int = 3,
        initial_delay: float = 1.0,
    ) -> bool:
        """Upload with automatic retry on transient failures."""
        delay = initial_delay

        for attempt in range(max_retries + 1):
            try:
                # Execute with circuit breaker protection
                return await self.circuit_breaker.execute(
                    self.storage.upload,
                    bucket,
                    path,
                    data,
                )

            except Exception as e:
                # Check if error is retryable
                if not self._is_retryable_error(e):
                    logger.error(f"Non-retryable error: {e}")
                    return False

                if attempt == max_retries:
                    logger.error(f"Max retries exceeded: {e}")
                    return False

                # Exponential backoff with jitter
                jitter = random.uniform(0, 0.1 * delay)
                await asyncio.sleep(delay + jitter)

                delay *= 2  # Exponential backoff
                logger.info(f"Retry attempt {attempt + 1}/{max_retries}")

        return False

    @staticmethod
    def _is_retryable_error(error: Exception) -> bool:
        """Check if error is transient/retryable."""
        # Retry on network errors, timeouts, rate limits
        retryable_patterns = [
            "ConnectionError",
            "Timeout",
            "RateLimit",
            "ServiceUnavailable",
            "5xx",  # Server errors
        ]

        error_str = str(error)
        return any(pattern in error_str for pattern in retryable_patterns)
```

---

## 8. Configuration Best Practices

### 8.1 Environment-Based Configuration

```yaml
# settings/akasha-storage.yml (production)
storage:
  default_backend: "s3-hot"

  # Production: S3 Standard + Redis cache
  s3-hot:
    bucket_name: "akasha-hot-prod"
    region: "${AWS_REGION:us-east-1}"
    storage_class: "STANDARD"

  redis-cache:
    host: "${REDIS_HOST:redis.prod.internal}"
    port: 6379
    ttl_seconds: 3600

  # Warm tier: S3 IA
  s3-warm:
    bucket_name: "akasha-warm-prod"
    region: "${AWS_REGION:us-east-1}"
    storage_class: "STANDARD_IA"

  # Cold tier: S3 Glacier
  s3-cold:
    bucket_name: "akasha-cold-prod"
    region: "${AWS_REGION:us-east-1}"
    storage_class: "GLACIER"
```

```yaml
# settings/akasha-storage-dev.yml (development)
storage:
  default_backend: "file"

  # Development: Local file storage
  file-dev:
    local_path: "${PWD}/data/akasha/dev"
    tier: "hot"

  # Minimal Redis for testing
  redis-dev:
    host: "localhost"
    port: 6379
    db: 15  # Separate DB for dev
```

### 8.2 Settings Validation

```python
# akasha/storage/validation.py
from pydantic import BaseModel, Field, validator

class StorageTierConfig(BaseModel):
    """Validated storage tier configuration."""

    provider: str = Field(..., pattern="^(s3|azure|gcs|file|redis)$")
    bucket_name: str | None = None
    region: str = "us-east-1"
    storage_class: str = "STANDARD"
    tier: t.Literal["hot", "warm", "cold"] = "hot"

    @validator("bucket_name")
    def validate_bucket_name(cls, v, values):
        """Validate bucket name is set for cloud providers."""
        provider = values.get("provider", "")
        if provider in ("s3", "azure", "gcs") and not v:
            raise ValueError(f"bucket_name required for {provider} provider")
        return v

class AkashaStorageConfig(BaseModel):
    """Complete Akasha storage configuration."""

    default_backend: str = "s3-hot"
    s3_hot: StorageTierConfig | None = None
    s3_warm: StorageTierConfig | None = None
    s3_cold: StorageTierConfig | None = None
    redis_cache: StorageTierConfig | None = None

    @validator("default_backend")
    def validate_backend_exists(cls, v, values):
        """Ensure default backend is configured."""
        # Check if the referenced backend exists in config
        backend_key = v.replace("-", "_")
        if backend_key not in values:
            raise ValueError(f"Default backend '{v}' not configured")
        return v

    @classmethod
    def from_yaml(cls, config_path: str | Path) -> "AkashaStorageConfig":
        """Load and validate configuration from YAML file."""
        import yaml

        with open(config_path) as f:
            config_data = yaml.safe_load(f)

        return cls(**config_data)
```

---

## 9. Performance Optimization

### 9.1 Connection Pooling

```python
# PATTERN: Connection pooling for storage backends
class StorageConnectionPool:
    """Connection pool for storage backend adapters."""

    def __init__(
        self,
        adapter_factory: t.Callable[[], Any],
        pool_size: int = 10,
        max_overflow: int = 20,
    ):
        self.adapter_factory = adapter_factory
        self.pool_size = pool_size
        self.max_overflow = max_overflow
        self.pool: queue.Queue[Any] = queue.Queue(maxsize=pool_size)
        self.overflow_count = 0

    async def acquire(self) -> Any:
        """Acquire adapter from pool (create if needed)."""
        try:
            # Try to get from pool (non-blocking)
            adapter = self.pool.get_nowait()
            return adapter

        except queue.Empty:
            # Pool exhausted - check overflow limit
            if self.overflow_count < self.max_overflow:
                self.overflow_count += 1
                logger.debug(f"Creating overflow adapter {self.overflow_count}")
                return self.adapter_factory()

            # Wait for an adapter to become available
            logger.warning("Connection pool exhausted, waiting...")
            adapter = await asyncio.to_thread(self.pool.get)
            return adapter

    async def release(self, adapter: Any) -> None:
        """Release adapter back to pool."""
        try:
            self.pool.put_nowait(adapter)
        except queue.Full:
            # Pool is full - discard overflow adapter
            self.overflow_count -= 1
            await adapter.close()
```

### 9.2 Parallel Upload/Download

```python
# PATTERN: Parallel operations for large files
class ParallelStorageOperations:
    """Parallel storage operations for improved throughput."""

    async def upload_large_file(
        self,
        bucket: str,
        path: str,
        data: bytes,
        chunk_size: int = 8 * 1024 * 1024,  # 8 MB chunks
        max_concurrency: int = 10,
    ) -> str:
        """Upload large file in parallel chunks using multipart upload."""
        file_size = len(data)

        if file_size < chunk_size * 2:
            # Small file - upload directly
            await self.storage.upload(bucket, path, data)
            return path

        # Large file - multipart upload
        upload_id = await self._create_multipart_upload(bucket, path)

        # Split into chunks
        chunks = [
            (i, data[i:i + chunk_size])
            for i in range(0, file_size, chunk_size)
        ]

        # Upload chunks in parallel
        tasks = [
            self._upload_chunk(bucket, path, upload_id, part_num, chunk_data)
            for part_num, chunk_data in chunks
        ]

        # Limit concurrency
        results = await self._execute_with_concurrency_limit(
            tasks,
            max_concurrency=max_concurrency,
        )

        # Complete multipart upload
        await self._complete_multipart_upload(bucket, path, upload_id, results)

        return path

    async def _execute_with_concurrency_limit(
        self,
        tasks: list[t.Any],
        max_concurrency: int,
    ) -> list[t.Any]:
        """Execute tasks with concurrency limit."""
        semaphore = asyncio.Semaphore(max_concurrency)

        async def bounded_task(task):
            async with semaphore:
                return await task

        return await asyncio.gather(*[bounded_task(t) for t in tasks])
```

---

## 10. Monitoring & Observability

### 10.1 Storage Metrics Collection

```python
# PATTERN: Comprehensive storage metrics with OpenTelemetry
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider

class StorageMetricsCollector:
    """Collects and reports storage metrics."""

    def __init__(self):
        self.meter = metrics.get_meter(__name__)
        self._setup_instruments()

    def _setup_instruments(self):
        """Setup OpenTelemetry metrics instruments."""
        # Operation counters
        self.upload_counter = self.meter.create_counter(
            "akasha.storage.uploads",
            description="Number of upload operations",
        )

        self.download_counter = self.meter.create_counter(
            "akasha.storage.downloads",
            description="Number of download operations",
        )

        # Latency histograms
        self.upload_latency = self.meter.create_histogram(
            "akasha.storage.upload_latency_ms",
            description="Upload operation latency",
        )

        self.download_latency = self.meter.create_histogram(
            "akasha.storage.download_latency_ms",
            description="Download operation latency",
        )

        # Byte counters
        self.bytes_uploaded = self.meter.create_counter(
            "akasha.storage.bytes_uploaded",
            description="Total bytes uploaded",
        )

        self.bytes_downloaded = self.meter.create_counter(
            "akasha.storage.bytes_downloaded",
            description="Total bytes downloaded",
        )

    def record_upload(
        self,
        tier: str,
        size_bytes: int,
        latency_ms: float,
        success: bool,
    ):
        """Record upload metrics."""
        self.upload_counter.add(1, {"tier": tier, "success": str(success)})
        self.upload_latency.record(latency_ms, {"tier": tier})
        if success:
            self.bytes_uploaded.add(size_bytes, {"tier": tier})

    def record_download(
        self,
        tier: str,
        size_bytes: int,
        latency_ms: float,
        success: bool,
    ):
        """Record download metrics."""
        self.download_counter.add(1, {"tier": tier, "success": str(success)})
        self.download_latency.record(latency_ms, {"tier": tier})
        if success:
            self.bytes_downloaded.add(size_bytes, {"tier": tier})
```

### 10.2 Health Checks

```python
# PATTERN: Comprehensive health checks for storage backends
class StorageHealthChecker:
    """Health checks for storage backends."""

    async def check_storage_health(
        self,
        adapter: Any,
        tier: str,
    ) -> dict[str, Any]:
        """Perform comprehensive health check on storage adapter.

        Returns:
            Health check results with status and metrics
        """
        health = {
            "tier": tier,
            "backend": adapter.__class__.__name__,
            "healthy": False,
            "latency_ms": 0,
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

        start_time = time.time()

        try:
            # 1. Test read/write operation
            test_data = b"health_check_test"
            test_path = f"_health/{uuid.uuid4()}"

            await adapter.upload(
                bucket="akasha-health",
                path=test_path,
                data=test_data,
            )

            downloaded = await adapter.download(
                bucket="akasha-health",
                path=test_path,
            )

            if downloaded != test_data:
                raise ValueError("Data corruption detected")

            await adapter.delete(
                bucket="akasha-health",
                path=test_path,
            )

            # Calculate latency
            latency_ms = (time.time() - start_time) * 1000

            health["healthy"] = True
            health["latency_ms"] = round(latency_ms, 2)

        except Exception as e:
            health["error"] = str(e)
            health["latency_ms"] = round((time.time() - start_time) * 1000, 2)

        return health

    async def check_all_backends(self) -> dict[str, dict[str, Any]]:
        """Check health of all configured storage backends."""
        results = {}

        for tier in ["hot", "warm", "cold"]:
            try:
                adapter = await storage_bridge.use(f"storage-{tier}")
                results[tier] = await self.check_storage_health(adapter, tier)
            except Exception as e:
                results[tier] = {
                    "tier": tier,
                    "healthy": False,
                    "error": str(e),
                }

        return results
```

---

## 11. Implementation Roadmap

### Phase 1: Core Storage (Weeks 1-2)
- [ ] Implement Oneiric storage adapter registration
- [ ] Create DuckDB vector storage with HNSW indexes
- [ ] Implement Parquet-based embeddings storage
- [ ] Setup S3 bucket lifecycle policies

### Phase 2: Tier Management (Weeks 3-4)
- [ ] Implement automatic tier promotion/demotion
- [ ] Create lifecycle hooks for data aging
- [ ] Build tier transition orchestrator
- [ ] Add tier migration jobs

### Phase 3: Multi-Cloud (Weeks 5-6)
- [ ] Implement Azure Blob adapter
- [ ] Implement GCS adapter
- [ ] Create multi-cloud coordinator
- [ ] Setup cross-cloud replication

### Phase 4: Caching & Performance (Weeks 7-8)
- [ ] Implement Redis cache layer
- [ ] Add connection pooling
- [ ] Implement parallel upload/download
- [ ] Optimize Parquet compression

### Phase 5: Resilience (Weeks 9-10)
- [ ] Implement circuit breakers
- [ ] Add retry with exponential backoff
- [ ] Create health check system
- [ ] Build metrics collection

---

## 12. Key Benefits of Oneiric Architecture

### 12.1 Universal Resolution System
- **4-tier precedence**: Explicit → Inferred → Stack level → Registration order
- **Explainable decisions**: Full traceability of why specific backend was chosen
- **Dynamic rebalancing**: Change priorities without code changes

### 12.2 Hot-Swappable Components
- **Zero-downtime updates**: Swap backends without restarting
- **Automatic rollback**: Revert on failure with health checks
- **Graceful degradation**: Continue operation with degraded functionality

### 12.3 Domain-Agnostic Bridges
- **Consistent patterns**: Same API for all storage types
- **Reduced complexity**: No need to learn backend-specific APIs
- **Type safety**: Full Pydantic validation

### 12.4 Remote-Ready Architecture
- **Manifest-based delivery**: Load adapters from remote URLs
- **Signature verification**: Ensure adapter integrity
- **Cache management**: Automatic caching of remote adapters

---

## 13. Conclusion

This architecture provides Akasha with a production-ready, scalable storage foundation built on Oneiric's universal resolution system. Key advantages:

1. **Tiered Storage**: Automatic lifecycle management with cost optimization
2. **Multi-Cloud Support**: Cross-cloud redundancy with automatic failover
3. **Performance**: Redis cache + DuckDB vector search + Parquet compression
4. **Resilience**: Circuit breakers + retry + health checks
5. **Observability**: Comprehensive metrics with OpenTelemetry
6. **Flexibility**: Hot-swappable backends via Oneiric lifecycle

The Oneiric integration enables Akasha to scale from 100 to 10,000+ Session-Buddy systems while maintaining optimal performance, cost, and reliability.
