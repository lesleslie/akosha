# Akasha Storage Implementation Guide

**Companion to**: AKASHA_STORAGE_ARCHITECTURE.md
**Focus**: Step-by-step implementation with working code examples

---

## Quick Start: 5-Minute Setup

### Step 1: Install Dependencies

```bash
# Create new project
uv init akasha
cd akasha

# Add Oneiric and storage dependencies
uv add oneiric
uv add boto3  # AWS S3
uv add azure-storage-blob  # Azure Blob
uv add google-cloud-storage  # GCS
uv add redis  # Redis cache
uv add duckdb  # Vector database
uv add pyarrow  # Parquet support
uv add pydantic  # Settings validation
uv add opentelemetry-api  # Metrics
uv add opentelemetry-sdk  # Metrics SDK
```

### Step 2: Create Directory Structure

```bash
mkdir -p akasha/storage
mkdir -p akasha/adapters
mkdir -p settings
mkdir -p tests
```

### Step 3: Create Oneiric Configuration

```yaml
# settings/akasha-storage.yml
storage:
  default_backend: "s3-hot"

  # Hot tier: S3 Standard + Redis cache
  s3-hot:
    provider: "s3"
    bucket_name: "${AKASHA_HOT_BUCKET:akasha-hot-dev}"
    region: "${AWS_REGION:us-east-1}"
    storage_class: "STANDARD"
    tier: "hot"

  redis-cache:
    provider: "redis"
    host: "${REDIS_HOST:localhost}"
    port: 6379
    db: 0
    ttl_seconds: 3600

  # Warm tier: S3 Infrequent Access
  s3-warm:
    provider: "s3"
    bucket_name: "${AKASHA_WARM_BUCKET:akasha-warm-dev}"
    region: "${AWS_REGION:us-east-1}"
    storage_class: "STANDARD_IA"
    tier: "warm"

  # Cold tier: S3 Glacier
  s3-cold:
    provider: "s3"
    bucket_name: "${AKASHA_COLD_BUCKET:akasha-cold-dev}"
    region: "${AWS_REGION:us-east-1}"
    storage_class: "GLACIER"
    tier: "cold"
```

### Step 4: Create Storage Adapter

```python
# akasha/storage/s3_adapter.py
"""Oneiric-compatible S3 storage adapter."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import boto3
from botocore.exceptions import ClientError

if t.TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

logger = logging.getLogger(__name__)


class S3StorageAdapter:
    """Oneiric-compatible S3 storage adapter.

    Implements the StorageProtocol interface for Oneiric integration.
    """

    def __init__(
        self,
        bucket_name: str,
        region: str = "us-east-1",
        storage_class: str = "STANDARD",
        tier: str = "hot",
    ):
        """Initialize S3 adapter.

        Args:
            bucket_name: S3 bucket name
            region: AWS region
            storage_class: S3 storage class (STANDARD, STANDARD_IA, GLACIER)
            tier: Storage tier (hot, warm, cold)
        """
        self.bucket_name = bucket_name
        self.region = region
        self.storage_class = storage_class
        self.tier = tier

        # Lazy initialization (create on first use)
        self._client: S3Client | None = None
        self._initialized = False

    async def init(self) -> None:
        """Initialize S3 client."""
        if self._initialized:
            return

        # Create S3 client in executor thread (boto3 is synchronous)
        loop = asyncio.get_event_loop()
        self._client = await loop.run_in_executor(
            None,
            lambda: boto3.client("s3", region_name=self.region),
        )

        # Check if bucket exists
        try:
            await loop.run_in_executor(
                None,
                lambda: self._client.head_bucket(Bucket=self.bucket_name),
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                logger.warning(f"Bucket {self.bucket_name} does not exist")
            else:
                raise

        self._initialized = True
        logger.info(f"S3 adapter initialized: {self.bucket_name} ({self.storage_class})")

    async def upload(
        self,
        bucket: str,
        path: str,
        data: bytes,
    ) -> None:
        """Upload data to S3.

        Args:
            bucket: Bucket name (should match self.bucket_name)
            path: Object key path
            data: Data to upload
        """
        if not self._initialized:
            await self.init()

        assert self._client is not None

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: self._client.put_object(
                Bucket=bucket,
                Key=path,
                Body=data,
                StorageClass=self.storage_class,
            ),
        )

        logger.debug(f"Uploaded {path} to {bucket} ({len(data)} bytes)")

    async def download(self, bucket: str, path: str) -> bytes:
        """Download data from S3.

        Args:
            bucket: Bucket name
            path: Object key path

        Returns:
            Downloaded data as bytes
        """
        if not self._initialized:
            await self.init()

        assert self._client is not None

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self._client.get_object(Bucket=bucket, Key=path),
        )

        data = response["Body"].read()
        logger.debug(f"Downloaded {path} from {bucket} ({len(data)} bytes)")

        return data

    async def delete(self, bucket: str, path: str) -> None:
        """Delete object from S3.

        Args:
            bucket: Bucket name
            path: Object key path
        """
        if not self._initialized:
            await self.init()

        assert self._client is not None

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: self._client.delete_object(Bucket=bucket, Key=path),
        )

        logger.debug(f"Deleted {path} from {bucket}")

    async def exists(self, bucket: str, path: str) -> bool:
        """Check if object exists in S3.

        Args:
            bucket: Bucket name
            path: Object key path

        Returns:
            True if object exists, False otherwise
        """
        if not self._initialized:
            await self.init()

        assert self._client is not None

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: self._client.head_object(Bucket=bucket, Key=path),
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return False
            raise
```

### Step 5: Register Adapter with Oneiric

```python
# akasha/storage/__init__.py
"""Akasha storage package with Oneiric integration."""

from oneiric.adapters.metadata import register_adapter_metadata, AdapterMetadata
from oneiric.core.resolution import Resolver
from akasha.storage.s3_adapter import S3StorageAdapter

def register_akasha_adapters(resolver: Resolver) -> None:
    """Register Akasha storage adapters with Oneiric resolver."""
    import os

    register_adapter_metadata(
        resolver,
        package_name="akasha",
        package_path=__file__,
        adapters=[
            # Hot tier
            AdapterMetadata(
                category="storage",
                provider="s3-hot",
                stack_level=100,
                factory=lambda: S3StorageAdapter(
                    bucket_name=os.getenv("AKASHA_HOT_BUCKET", "akasha-hot-dev"),
                    region=os.getenv("AWS_REGION", "us-east-1"),
                    storage_class="STANDARD",
                    tier="hot",
                ),
                description="S3 Standard storage for hot tier",
            ),

            # Warm tier
            AdapterMetadata(
                category="storage",
                provider="s3-warm",
                stack_level=50,
                factory=lambda: S3StorageAdapter(
                    bucket_name=os.getenv("AKASHA_WARM_BUCKET", "akasha-warm-dev"),
                    region=os.getenv("AWS_REGION", "us-east-1"),
                    storage_class="STANDARD_IA",
                    tier="warm",
                ),
                description="S3 Infrequent Access for warm tier",
            ),

            # Cold tier
            AdapterMetadata(
                category="storage",
                provider="s3-cold",
                stack_level=10,
                factory=lambda: S3StorageAdapter(
                    bucket_name=os.getenv("AKASHA_COLD_BUCKET", "akasha-cold-dev"),
                    region=os.getenv("AWS_REGION", "us-east-1"),
                    storage_class="GLACIER",
                    tier="cold",
                ),
                description="S3 Glacier for cold tier",
            ),
        ],
    )
```

### Step 6: Test the Integration

```python
# tests/test_storage_integration.py
"""Test Akasha storage integration with Oneiric."""

import pytest
from oneiric.core.resolution import Resolver
from oneiric.adapters.bridge import AdapterBridge
from oneiric.core.lifecycle import LifecycleManager
from akasha.storage import register_akasha_adapters

@pytest.mark.asyncio
async def test_s3_hot_adapter():
    """Test S3 hot tier adapter."""
    # Setup Oneiric resolver
    resolver = Resolver()
    register_akasha_adapters(resolver)

    # Create lifecycle manager
    lifecycle = LifecycleManager(resolver)

    # Create adapter bridge
    bridge = AdapterBridge(
        resolver=resolver,
        lifecycle=lifecycle,
        settings=Settings.load_yaml("settings/akasha-storage.yml")
    )

    # Use hot tier adapter
    handle = await bridge.use("storage-s3-hot")
    storage = handle.instance

    # Initialize adapter
    await storage.init()

    # Test upload
    test_data = b"Hello, Akasha!"
    await storage.upload(
        bucket="akasha-hot-dev",
        path="test/hello.txt",
        data=test_data,
    )

    # Test download
    downloaded = await storage.download(
        bucket="akasha-hot-dev",
        path="test/hello.txt",
    )
    assert downloaded == test_data

    # Test exists
    assert await storage.exists(
        bucket="akasha-hot-dev",
        path="test/hello.txt",
    )

    # Test delete
    await storage.delete(
        bucket="akasha-hot-dev",
        path="test/hello.txt",
    )

    # Verify deleted
    assert not await storage.exists(
        bucket="akasha-hot-dev",
        path="test/hello.txt",
    )
```

---

## Complete Implementation: Vector Embeddings Storage

```python
# akasha/storage/vector_embeddings.py
"""Vector embeddings storage with DuckDB + Parquet + Oneiric."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from oneiric.adapters.bridge import AdapterBridge
from oneiric.core.resolution import Resolver
from oneiric.core.lifecycle import LifecycleManager

logger = logging.getLogger(__name__)


class VectorEmbeddingStorage:
    """High-performance storage for FLOAT[384] vector embeddings.

    Architecture:
    - Hot tier: DuckDB with HNSW vector index (fast similarity search)
    - Warm tier: Parquet files with columnar compression
    - Cold tier: Compressed Parquet with S3 Glacier
    """

    def __init__(self, resolver: Resolver | None = None):
        """Initialize vector storage with Oneiric integration."""
        self.resolver = resolver or Resolver()
        self.lifecycle = LifecycleManager(self.resolver)
        self.bridge = AdapterBridge(
            resolver=self.resolver,
            lifecycle=self.lifecycle,
            settings=Settings.load_yaml("settings/akasha-storage.yml"),
        )

        # DuckDB connection (in-memory for hot tier)
        self.duckdb_conn: duckdb.DuckDBPyConnection | None = None
        self.embedding_dim = 384  # all-MiniLM-L6-v2

    async def initialize(self) -> None:
        """Initialize DuckDB database and create tables."""
        if self.duckdb_conn is not None:
            return

        # Create in-memory DuckDB connection
        self.duckdb_conn = duckdb.connect(database=":memory:")

        # Create embeddings table with HNSW index support
        self.duckdb_conn.execute("""
            CREATE TABLE IF NOT EXISTS embeddings (
                id VARCHAR PRIMARY KEY,
                content TEXT,
                embedding FLOAT[384],
                metadata JSON,
                source_system VARCHAR,
                created_at TIMESTAMP,
                tier VARCHAR DEFAULT 'hot'
            )
        """)

        # Enable HNSW extension (if available)
        try:
            self.duckdb_conn.execute("INSTALL 'vss';")
            self.duckdb_conn.execute("LOAD 'vss';")
            self.duckdb_conn.execute("SET hnsw_enable_experimental_persistence=true")

            # Create HNSW index for fast vector similarity search
            self.duckdb_conn.execute("""
                CREATE INDEX IF NOT EXISTS embeddings_hnsw_idx
                ON embeddings
                USING HNSW (embedding)
                WITH (
                    metric = 'cosine',
                    M = 16,
                    ef_construction = 64
                )
            """)
            logger.info("HNSW index created for embeddings table")
        except Exception as e:
            logger.warning(f"HNSW index not available: {e}")

    async def store_embedding(
        self,
        embedding_id: str,
        content: str,
        embedding: list[float],
        source_system: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Store a single embedding in hot tier (DuckDB).

        Args:
            embedding_id: Unique identifier for the embedding
            content: Original text content
            embedding: FLOAT[384] vector embedding
            source_system: Session-Buddy system identifier
            metadata: Optional metadata

        Returns:
            Storage path/key
        """
        if self.duckdb_conn is None:
            await self.initialize()

        # Insert into DuckDB
        self.duckdb_conn.execute("""
            INSERT INTO embeddings (id, content, embedding, metadata, source_system, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, [
            embedding_id,
            content,
            embedding,
            json.dumps(metadata or {}),
            source_system,
            datetime.now(UTC),
        ])

        logger.debug(f"Stored embedding {embedding_id} from {source_system}")

        # Also persist to Parquet in warm tier (async)
        asyncio.create_task(
            self._persist_to_warm_tier(
                embedding_id=embedding_id,
                content=content,
                embedding=embedding,
                source_system=source_system,
                metadata=metadata,
            )
        )

        return embedding_id

    async def store_embeddings_batch(
        self,
        embeddings: list[dict[str, Any]],
        source_system: str,
        date: str,
    ) -> str:
        """Store a batch of embeddings to Parquet file (warm tier).

        Args:
            embeddings: List of embedding records
            source_system: Session-Buddy system identifier
            date: Date partition (YYYY-MM-DD)

        Returns:
            Storage path for uploaded Parquet file
        """
        # Convert to PyArrow Table (columnar format)
        schema = pa.schema([
            ('id', pa.string()),
            ('content', pa.string()),
            ('embedding', pa.list_(pa.float32(), self.embedding_dim)),
            ('metadata', pa.string()),
            ('source_system', pa.string()),
            ('created_at', pa.timestamp('ns')),
        ])

        # Build arrays
        ids = [e["id"] for e in embeddings]
        contents = [e["content"] for e in embeddings]
        embedding_arrays = [e["embedding"] for e in embeddings]
        metadata_jsons = [json.dumps(e.get("metadata", {})) for e in embeddings]
        source_systems = [source_system] * len(embeddings)
        created_ats = [e.get("created_at", datetime.now(UTC)) for e in embeddings]

        table = pa.Table.from_arrays(
            [
                pa.array(ids),
                pa.array(contents),
                pa.array(embedding_arrays),
                pa.array(metadata_jsons),
                pa.array(source_systems),
                pa.array(created_ats),
            ],
            schema=schema,
        )

        # Write to Parquet with compression
        buffer = pa.BufferOutputStream()
        pq.write_table(
            table,
            buffer,
            compression='snappy',  # Fast compression for warm tier
            row_group_size=10000,  # Optimal for vector scans
        )

        # Generate storage path
        path = f"embeddings/{source_system}/{date}/batch_{uuid.uuid4()}.parquet"

        # Upload to warm tier storage (using Oneiric adapter)
        storage = await self.bridge.use("storage-s3-warm")
        await storage.instance.upload(
            bucket="akasha-warm-dev",
            path=path,
            data=buffer.getvalue().to_pybytes(),
        )

        logger.info(
            f"Stored {len(embeddings)} embeddings to warm tier: {path}",
            extra={
                "count": len(embeddings),
                "path": path,
                "size_bytes": buffer.size(),
            }
        )

        return path

    async def search_similar(
        self,
        query_embedding: list[float],
        source_systems: list[str] | None = None,
        limit: int = 100,
        threshold: float = 0.7,
    ) -> list[dict[str, Any]]:
        """Search for similar embeddings using DuckDB vector similarity.

        Args:
            query_embedding: Query vector FLOAT[384]
            source_systems: Optional filter by source systems
            limit: Maximum number of results
            threshold: Minimum similarity score (0.0 to 1.0)

        Returns:
            List of similar embeddings with similarity scores
        """
        if self.duckdb_conn is None:
            await self.initialize()

        # Set HNSW ef_search parameter for accuracy
        self.duckdb_conn.execute("SET hnsw_ef_search = 100")

        # Build query
        query_vector = str(query_embedding)
        sql = """
            SELECT
                id, content, metadata, source_system, created_at,
                array_cosine_similarity(embedding, ?::FLOAT[384]) as similarity
            FROM embeddings
            WHERE embedding IS NOT NULL
        """

        params = [query_vector]

        # Add source system filter if specified
        if source_systems:
            placeholders = ", ".join(["?" for _ in source_systems])
            sql += f" AND source_system IN ({placeholders})"
            params.extend(source_systems)

        sql += " ORDER BY similarity DESC LIMIT ?"
        params.append(limit * 2)  # Get extra for filtering

        # Execute query
        results = self.duckdb_conn.execute(sql, params).fetchall()

        # Filter by threshold and format results
        formatted_results = []
        for row in results:
            similarity = row[5] or 0.0
            if similarity >= threshold:
                formatted_results.append({
                    "id": row[0],
                    "content": row[1],
                    "metadata": json.loads(row[2]) if row[2] else {},
                    "source_system": row[3],
                    "created_at": row[4],
                    "similarity": float(similarity),
                })

        return formatted_results[:limit]

    async def _persist_to_warm_tier(
        self,
        embedding_id: str,
        content: str,
        embedding: list[float],
        source_system: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Persist embedding to warm tier (async background task)."""
        try:
            # Store as single-record Parquet file
            await self.store_embeddings_batch(
                embeddings=[{
                    "id": embedding_id,
                    "content": content,
                    "embedding": embedding,
                    "metadata": metadata,
                    "created_at": datetime.now(UTC),
                }],
                source_system=source_system,
                date=datetime.now(UTC).strftime("%Y-%m-%d"),
            )
        except Exception as e:
            logger.error(f"Failed to persist to warm tier: {e}")

    async def close(self) -> None:
        """Close database connections."""
        if self.duckdb_conn:
            self.duckdb_conn.close()
            self.duckdb_conn = None
```

---

## Complete Implementation: Redis Cache Layer

```python
# akasha/storage/redis_cache.py
"""Redis cache layer for hot tier acceleration."""

from __future__ import annotations

import json
import logging
from typing import Any

import redis.asyncio as aioredis
from oneiric.adapters.metadata import AdapterMetadata

logger = logging.getLogger(__name__)


class RedisCacheAdapter:
    """Oneiric-compatible Redis cache adapter."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        ttl_seconds: int = 3600,
    ):
        """Initialize Redis adapter.

        Args:
            host: Redis host
            port: Redis port
            db: Redis database number
            ttl_seconds: Default TTL for cached items
        """
        self.host = host
        self.port = port
        self.db = db
        self.ttl_seconds = ttl_seconds

        # Lazy initialization
        self._client: aioredis.Redis | None = None
        self._initialized = False

    async def init(self) -> None:
        """Initialize Redis client."""
        if self._initialized:
            return

        self._client = await aioredis.from_url(
            f"redis://{self.host}:{self.port}/{self.db}",
            encoding="utf-8",
            decode_responses=True,
        )

        # Test connection
        await self._client.ping()
        self._initialized = True

        logger.info(f"Redis cache initialized: {self.host}:{self.port}/{self.db}")

    async def get(self, key: str) -> Any | None:
        """Get value from cache.

        Args:
            key: Cache key

        Returns:
            Cached value or None if not found
        """
        if not self._initialized:
            await self.init()

        assert self._client is not None

        value = await self._client.get(key)
        if value is None:
            return None

        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        """Set value in cache.

        Args:
            key: Cache key
            value: Value to cache
            ttl: Time-to-live in seconds (uses default if None)
        """
        if not self._initialized:
            await self.init()

        assert self._client is not None

        ttl = ttl or self.ttl_seconds

        # Serialize value
        if not isinstance(value, (str, bytes)):
            value = json.dumps(value)

        await self._client.set(key, value, ex=ttl)

    async def delete(self, key: str) -> None:
        """Delete key from cache.

        Args:
            key: Cache key
        """
        if not self._initialized:
            await self.init()

        assert self._client is not None

        await self._client.delete(key)

    async def exists(self, key: str) -> bool:
        """Check if key exists in cache.

        Args:
            key: Cache key

        Returns:
            True if key exists, False otherwise
        """
        if not self._initialized:
            await self.init()

        assert self._client is not None

        return await self._client.exists(key) > 0


# Register with Oneiric
def register_redis_adapter(resolver: Resolver) -> None:
    """Register Redis cache adapter with Oneiric resolver."""
    import os

    register_adapter_metadata(
        resolver,
        package_name="akasha",
        package_path=__file__,
        adapters=[
            AdapterMetadata(
                category="cache",
                provider="redis-hot",
                stack_level=100,  # Same priority as S3 hot
                factory=lambda: RedisCacheAdapter(
                    host=os.getenv("REDIS_HOST", "localhost"),
                    port=6379,
                    db=0,
                    ttl_seconds=3600,
                ),
                description="Redis cache for hot data acceleration",
            ),
        ],
    )
```

---

## Usage Example: End-to-End Workflow

```python
# examples/akasha_workflow.py
"""Complete Akasha storage workflow example."""

import asyncio
from oneiric.core.resolution import Resolver
from oneiric.adapters.bridge import AdapterBridge
from oneiric.core.lifecycle import LifecycleManager
from akasha.storage import register_akasha_adapters
from akasha.storage.vector_embeddings import VectorEmbeddingStorage
from akasha.storage.redis_cache import register_redis_adapter


async def main():
    """Demonstrate complete Akasha workflow."""
    # 1. Setup Oneiric resolver
    resolver = Resolver()

    # 2. Register all adapters
    register_akasha_adapters(resolver)
    register_redis_adapter(resolver)

    # 3. Create lifecycle manager
    lifecycle = LifecycleManager(resolver)

    # 4. Create adapter bridge
    bridge = AdapterBridge(
        resolver=resolver,
        lifecycle=lifecycle,
        settings=Settings.load_yaml("settings/akasha-storage.yml"),
    )

    # 5. Initialize vector storage
    vector_storage = VectorEmbeddingStorage(resolver=resolver)
    await vector_storage.initialize()

    # 6. Store embeddings (hot tier: DuckDB + warm tier: S3 Parquet)
    embedding_id = await vector_storage.store_embedding(
        embedding_id="conv_12345",
        content="Example conversation text",
        embedding=[0.1] * 384,  # Dummy embedding
        source_system="session-buddy-001",
        metadata={"project": "akasha", "quality_score": 0.85},
    )

    print(f"Stored embedding: {embedding_id}")

    # 7. Search for similar embeddings
    similar = await vector_storage.search_similar(
        query_embedding=[0.1] * 384,
        source_systems=["session-buddy-001"],
        limit=10,
        threshold=0.7,
    )

    print(f"Found {len(similar)} similar embeddings")
    for result in similar[:3]:
        print(f"  - {result['id']}: {result['similarity']:.3f}")

    # 8. Cache results in Redis (hot tier acceleration)
    cache = await bridge.use("cache-redis-hot")
    await cache.instance.set(
        key=f"search:{embedding_id}",
        value=similar,
        ttl=3600,
    )

    print(f"Cached results in Redis")

    # 9. Retrieve from cache
    cached = await cache.instance.get(f"search:{embedding_id}")
    print(f"Retrieved {len(cached)} results from cache")

    # 10. Cleanup
    await vector_storage.close()


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Testing Strategy

```python
# tests/test_vector_storage.py
"""Test vector embeddings storage."""

import pytest
from akasha.storage.vector_embeddings import VectorEmbeddingStorage


@pytest.mark.asyncio
async def test_store_and_search():
    """Test storing and searching embeddings."""
    storage = VectorEmbeddingStorage()
    await storage.initialize()

    # Store test embedding
    embedding_id = await storage.store_embedding(
        embedding_id="test_001",
        content="Test content",
        embedding=[0.1] * 384,
        source_system="test-system",
    )

    # Search for similar embeddings
    results = await storage.search_similar(
        query_embedding=[0.1] * 384,
        limit=10,
        threshold=0.0,
    )

    assert len(results) >= 1
    assert results[0]["id"] == "test_001"
    assert results[0]["similarity"] >= 0.99

    await storage.close()


@pytest.mark.asyncio
async def test_batch_storage():
    """Test batch storage to Parquet."""
    storage = VectorEmbeddingStorage()
    await storage.initialize()

    # Create test embeddings
    embeddings = [
        {
            "id": f"test_{i:05d}",
            "content": f"Test content {i}",
            "embedding": [0.1 * i] * 384,
            "metadata": {"index": i},
        }
        for i in range(100)
    ]

    # Store batch
    path = await storage.store_embeddings_batch(
        embeddings=embeddings,
        source_system="test-system",
        date="2025-01-25",
    )

    assert path.endswith(".parquet")
    assert "test-system" in path

    await storage.close()
```

---

## Deployment Checklist

- [ ] Create S3 buckets (hot, warm, cold) with lifecycle policies
- [ ] Setup Redis cluster for caching
- [ ] Configure Oneiric settings for each environment (dev/staging/prod)
- [ ] Set up monitoring with OpenTelemetry
- [ ] Implement health checks for all storage backends
- [ ] Configure circuit breakers for resilience
- [ ] Set up automated tier transition jobs
- [ ] Implement backup and disaster recovery procedures

---

## Next Steps

1. **Read the Architecture**: See `AKASHA_STORAGE_ARCHITECTURE.md` for complete design
2. **Implement Core Adapters**: Start with S3, then add Redis, DuckDB
3. **Test Thoroughly**: Use pytest with async fixtures
4. **Deploy to Dev**: Test with real S3 buckets
5. **Monitor Performance**: Use OpenTelemetry metrics
6. **Scale Gradually**: Start with 100 systems, grow to 10,000+

---

## Support

- **Oneiric Documentation**: https://github.com/oneiric/oneiric
- **Session-Buddy**: https://github.com/lesleslie/session-buddy
- **Akasha Issues**: https://github.com/lesleslie/akasha/issues
