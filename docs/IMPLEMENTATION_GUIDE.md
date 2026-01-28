# Akasha Implementation Guide

**Phase 1 Foundation** - Weeks 1-4

---

## Overview

This guide provides step-by-step implementation instructions for Akasha Phase 1, which establishes the core infrastructure for universal memory aggregation.

**Phase 1 Scope**:
- Core storage layer (DuckDB hot/warm stores)
- Oneiric integration for cold storage
- Basic ingestion pipeline
- MCP + REST API endpoints
- Initial testing framework

**Target Scale**: 100 systems, <10M embeddings

---

## Prerequisites

### System Requirements

- Python 3.13+
- 16GB RAM minimum (32GB recommended)
- 100GB NVMe SSD storage
- Redis server (for caching)
- S3 bucket or equivalent cloud storage

### External Dependencies

- **Session-Buddy**: Must be uploading system memories to cloud storage
- **Mahavishnu**: Must be running for workflow orchestration
- **Oneiric**: Storage adapters must be available

---

## Week 1: Foundation

### Task 1.1: Project Setup ✅

**Status**: ✅ Complete
**Files Created**:
- `/Users/les/Projects/akasha/` - Project directory
- `pyproject.toml` - UV configuration
- `.envrc` - Direnv configuration
- `README.md` - Project documentation
- `CLAUDE.md` - AI assistant instructions

**Acceptance Criteria**:
- [x] Project directory created
- [x] UV venv initialized
- [x] .envrc copied from crackerjack
- [x] Basic project structure created

### Task 1.2: Configuration Management

**File**: `akasha/config.py`

Create centralized configuration management with Pydantic:

```python
"""Akasha configuration management."""

from __future__ import annotations
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class HotStorageConfig(BaseModel):
    """Hot storage configuration."""

    backend: str = "duckdb-memory"
    path: Path = Field(default_factory=lambda: Path("/data/akasha/hot"))
    write_ahead_log: bool = True
    wal_path: Path = Field(default_factory=lambda: Path("/data/akasha/wal"))


class WarmStorageConfig(BaseModel):
    """Warm storage configuration."""

    backend: str = "duckdb-ssd"
    path: Path = Field(default_factory=lambda: Path("/data/akasha/warm"))
    num_partitions: int = 256


class ColdStorageConfig(BaseModel):
    """Cold storage configuration."""

    backend: str = "s3"  # or azure, gcs
    bucket: str = Field(default_factory=lambda: os.getenv("AKASHA_COLD_BUCKET", "akasha-cold-data"))
    prefix: str = "conversations/"
    format: str = "parquet"


class CacheConfig(BaseModel):
    """Cache configuration."""

    backend: str = "redis"
    host: str = Field(default_factory=lambda: os.getenv("REDIS_HOST", "localhost"))
    port: int = 6379
    db: int = 0
    local_ttl_seconds: int = 60
    redis_ttl_seconds: int = 3600


class AkashaConfig(BaseSettings):
    """Main Akasha configuration."""

    # Storage
    hot: HotStorageConfig = Field(default_factory=HotStorageConfig)
    warm: WarmStorageConfig = Field(default_factory=WarmStorageConfig)
    cold: ColdStorageConfig = Field(default_factory=ColdStorageConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)

    # API
    api_port: int = 8000
    mcp_port: int = 3001
    debug: bool = False

    # Processing
    ingestion_workers: int = 3
    max_concurrent_ingests: int = 100
    shard_count: int = 256

    class Config:
        env_file = ".env"
        env_nested_delimiter = "__"


# Global configuration instance
config = AkashaConfig()
```

**Acceptance Criteria**:
- [ ] Configuration loads from environment variables
- [ ] YAML file support (via `config/akasha.yaml`)
- [ ] Type validation with Pydantic
- [ ] Default values for all settings

### Task 1.3: Data Models

**File**: `akasha/models/__init__.py`

Define core data models:

```python
"""Akasha data models."""

from __future__ import annotations
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


@dataclass
class SystemMemoryUpload:
    """System memory upload from Session-Buddy."""

    system_id: str
    upload_id: str
    manifest: dict[str, Any]
    storage_prefix: str
    uploaded_at: datetime


class HotRecord(BaseModel):
    """Hot tier record with full embeddings."""

    system_id: str
    conversation_id: str
    content: str
    embedding: list[float]  # FLOAT[384]
    timestamp: datetime
    metadata: dict[str, Any]

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


class WarmRecord(BaseModel):
    """Warm tier record with compressed embeddings."""

    system_id: str
    conversation_id: str
    embedding: list[int]  # INT8[384] (quantized)
    summary: str  # Extractive summary (3 sentences)
    timestamp: datetime
    metadata: dict[str, Any]


class ColdRecord(BaseModel):
    """Cold tier record with ultra-compressed data."""

    system_id: str
    conversation_id: str
    fingerprint: bytes  # MinHash fingerprint (for deduplication)
    ultra_summary: str  # Single sentence summary
    timestamp: datetime
    daily_metrics: dict[str, float]
```

**Acceptance Criteria**:
- [ ] All models defined with type hints
- [ ] Pydantic validation working
- [ ] JSON serialization/deserialization

---

## Week 2: Storage Layer

### Task 2.1: Hot Store (DuckDB In-Memory)

**File**: `akasha/storage/hot_store.py`

```python
"""Hot store: DuckDB in-memory for recent data."""

from __future__ import annotations
import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from akasha.models import HotRecord

logger = logging.getLogger(__name__)


class HotStore:
    """Hot store with DuckDB in-memory storage."""

    def __init__(self, database_path: str | Path = ":memory:"):
        """Initialize hot store.

        Args:
            database_path: DuckDB database path (":memory:" for in-memory)
        """
        self.db_path = database_path
        self.conn: duckdb.DuckDBPyConnection | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize database schema."""
        async with self._lock:
            self.conn = duckdb.connect(self.db_path)

            # Create conversations table with HNSW index support
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    system_id VARCHAR,
                    conversation_id VARCHAR PRIMARY KEY,
                    content TEXT,
                    embedding FLOAT[384],
                    timestamp TIMESTAMP,
                    metadata JSON,
                    content_hash VARCHAR,
                    uploaded_at TIMESTAMP DEFAULT NOW()
                )
            """)

            # Create HNSW index for vector search
            self.conn.execute("""
                CREATE INDEX IF NOT EXISTS embedding_hnsw_index
                ON conversations USING HNSW (embedding)
                WITH (m = 16, ef_construction = 200)
            """)

            logger.info("Hot store initialized")

    async def insert(self, record: HotRecord) -> None:
        """Insert conversation into hot store.

        Args:
            record: Hot record to insert
        """
        async with self._lock:
            self.conn.execute("""
                INSERT INTO conversations
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                record.system_id,
                record.conversation_id,
                record.content,
                record.embedding,
                record.timestamp,
                record.metadata,
                self._compute_content_hash(record.content),
                datetime.now(UTC),
            ])

    async def search_similar(
        self,
        query_embedding: list[float],
        system_id: str | None = None,
        limit: int = 10,
        threshold: float = 0.7,
    ) -> list[dict[str, Any]]:
        """Search for similar conversations using vector similarity.

        Args:
            query_embedding: Query vector (FLOAT[384])
            system_id: Optional system filter
            limit: Maximum results to return
            threshold: Minimum similarity score (0-1)

        Returns:
            List of similar conversations with metadata
        """
        async with self._lock:
            # Set HNSW search parameters
            self.conn.execute("SET hnsw_ef_search = 100")

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
                [query_embedding, limit]
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
            ]

    @staticmethod
    def _compute_content_hash(content: str) -> str:
        """Compute SHA-256 hash of content."""
        import hashlib
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    async def close(self) -> None:
        """Close database connection."""
        async with self._lock:
            if self.conn:
                self.conn.close()
                logger.info("Hot store closed")
```

**Acceptance Criteria**:
- [ ] DuckDB in-memory database initialized
- [ ] HNSW index created for embeddings
- [ ] Insert and search operations working
- [ ] Thread-safe with asyncio lock

### Task 2.2: Warm Store (DuckDB on-Disk)

**File**: `akasha/storage/warm_store.py`

```python
"""Warm store: DuckDB on-disk for historical data."""

from __future__ import annotations
import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from akasha.models import WarmRecord

logger = logging.getLogger(__name__)


class WarmStore:
    """Warm store with DuckDB on-disk storage."""

    def __init__(self, database_path: Path):
        """Initialize warm store.

        Args:
            database_path: Path to DuckDB database file
        """
        self.db_path = database_path
        self.conn: duckdb.DuckDBPyConnection | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize database schema."""
        async with self._lock:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = duckdb.connect(str(self.db_path))

            # Create warm conversations table (compressed embeddings)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    system_id VARCHAR,
                    conversation_id VARCHAR PRIMARY KEY,
                    embedding INT8[384],  -- Quantized to INT8 (75% size reduction)
                    summary TEXT,  -- Extractive summary (3 sentences)
                    timestamp TIMESTAMP,
                    metadata JSON,
                    uploaded_at TIMESTAMP DEFAULT NOW()
                )
            """)

            # Partition by date for efficient queries
            self.conn.execute("""
                CREATE INDEX IF NOT EXISTS date_partition_idx
                ON conversations (date_trunc('day', timestamp))
            """)

            logger.info(f"Warm store initialized at {self.db_path}")

    async def insert(self, record: WarmRecord) -> None:
        """Insert conversation into warm store.

        Args:
            record: Warm record to insert
        """
        async with self._lock:
            self.conn.execute("""
                INSERT INTO conversations
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, [
                record.system_id,
                record.conversation_id,
                record.embedding,
                record.summary,
                record.timestamp,
                record.metadata,
                datetime.now(UTC),
            ])

    async def close(self) -> None:
        """Close database connection."""
        async with self._lock:
            if self.conn:
                self.conn.close()
                logger.info("Warm store closed")
```

**Acceptance Criteria**:
- [ ] DuckDB on-disk database created
- [ ] INT8 quantization for embeddings (75% size reduction)
- [ ] Partitioned by date for efficient queries

---

## Week 3: Ingestion Pipeline

### Task 3.1: Ingestion Worker

**File**: `akasha/ingestion/worker.py`

Create pull-based ingestion worker that polls cloud storage for new uploads:

```python
"""Ingestion worker: Pull system memories from cloud storage."""

from __future__ import annotations
import asyncio
import logging
from datetime import UTC, datetime

from oneiric.adapters import StorageAdapter

from akasha.models import SystemMemoryUpload
from akasha.storage.hot_store import HotStore

logger = logging.getLogger(__name__)


class IngestionWorker:
    """Pull-based ingestion worker."""

    def __init__(
        self,
        storage: StorageAdapter,
        hot_store: HotStore,
        poll_interval_seconds: int = 30,
    ):
        """Initialize worker.

        Args:
            storage: Oneiric storage adapter
            hot_store: Hot store for data insertion
            poll_interval_seconds: Polling interval
        """
        self.storage = storage
        self.hot_store = hot_store
        self.poll_interval_seconds = poll_interval_seconds
        self._running = False

    async def run(self) -> None:
        """Main worker loop."""
        self._running = True
        logger.info("Ingestion worker started")

        while self._running:
            try:
                # 1. Discover new uploads
                uploads = await self._discover_uploads()

                if uploads:
                    logger.info(f"Discovered {len(uploads)} new uploads")

                    # 2. Process uploads
                    for upload in uploads:
                        await self._process_upload(upload)

                # 3. Wait before next poll
                await asyncio.sleep(self.poll_interval_seconds)

            except Exception as e:
                logger.error(f"Ingestion worker error: {e}")
                await asyncio.sleep(60)  # Backoff on error

    async def _discover_uploads(self) -> list[SystemMemoryUpload]:
        """Discover new uploads from cloud storage.

        Returns:
            List of discovered uploads
        """
        uploads = []

        # List all system prefixes
        async for system_prefix in self.storage.list_prefixes("system_id="):
            system_id = system_prefix.split("=")[1]

            # List upload prefixes within system
            async for upload_prefix in self.storage.list_prefixes(f"{system_prefix}/upload_id="):
                upload_id = upload_prefix.split("=")[1]

                # Check for manifest file
                manifest_path = f"{upload_prefix}/manifest.json"
                if await self.storage.exists(manifest_path):
                    manifest_data = await self.storage.download(manifest_path)
                    uploads.append(SystemMemoryUpload(
                        system_id=system_id,
                        upload_id=upload_id,
                        manifest=manifest_data,
                        storage_prefix=upload_prefix,
                    ))

        return uploads

    async def _process_upload(self, upload: SystemMemoryUpload) -> None:
        """Process a single upload.

        Args:
            upload: Upload to process
        """
        logger.info(f"Processing upload: {upload.system_id}/{upload.upload_id}")

        # Implementation: Extract, deduplicate, insert into hot store
        # TODO: Implement in Task 3.2

    def stop(self) -> None:
        """Stop the worker."""
        self._running = False
        logger.info("Ingestion worker stopped")
```

**Acceptance Criteria**:
- [ ] Worker polls cloud storage for new uploads
- [ ] Discovers system_id=XXX/upload_id=YYY pattern
- [ ] Processes uploads sequentially
- [ ] Graceful shutdown with `stop()` method

---

## Week 4: API Layer

### Task 4.1: REST API with FastAPI

**File**: `akasha/api/routes.py`

```python
"""FastAPI routes for Akasha."""

from __future__ import annotations
import logging
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from akasha.storage.hot_store import HotStore

logger = logging.getLogger(__name__)

app = FastAPI(title="Akasha Universal Memory API")

# Initialize hot store (TODO: use dependency injection)
hot_store = HotStore()


class SearchRequest(BaseModel):
    """Search request."""

    query: str = Field(..., description="Search query text")
    system_id: str | None = Field(None, description="Filter to system")
    limit: int = Field(10, ge=1, le=100, description="Max results")
    threshold: float = Field(0.7, ge=0.0, le=1.0, description="Min similarity")


class SearchResponse(BaseModel):
    """Search response."""

    total_results: int
    results: list[dict]
    query_time_ms: int


@app.on_event("startup")
async def startup():
    """Initialize hot store on startup."""
    await hot_store.initialize()
    logger.info("Akasha API started")


@app.on_event("shutdown")
async def shutdown():
    """Close hot store on shutdown."""
    await hot_store.close()
    logger.info("Akasha API stopped")


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}


@app.post("/api/v1/search", response_model=SearchResponse)
async def search_conversations(request: SearchRequest) -> SearchResponse:
    """Search across all system memories.

    Args:
        request: Search request

    Returns:
        Search response with results
    """
    start_time = datetime.now(UTC)

    # Generate query embedding (TODO: implement embedding service)
    query_embedding = [0.0] * 384  # Placeholder

    # Search hot store
    results = await hot_store.search_similar(
        query_embedding=query_embedding,
        system_id=request.system_id,
        limit=request.limit,
        threshold=request.threshold,
    )

    elapsed_ms = int((datetime.now(UTC) - start_time).total_seconds() * 1000)

    return SearchResponse(
        total_results=len(results),
        results=results,
        query_time_ms=elapsed_ms,
    )
```

**Acceptance Criteria**:
- [ ] FastAPI server starts successfully
- [ ] Health check endpoint working
- [ ] Search endpoint accepts requests
- [ ] Proper startup/shutdown hooks

---

## Testing

### Unit Tests

**File**: `tests/unit/test_storage.py`

```python
"""Unit tests for storage layer."""

import pytest
from akasha.storage.hot_store import HotStore
from akasha.models import HotRecord


@pytest.fixture
async def hot_store():
    """Fixture for hot store."""
    store = HotStore(database_path=":memory:")
    await store.initialize()
    yield store
    await store.close()


@pytest.mark.asyncio
async def test_insert_conversation(hot_store):
    """Test inserting a conversation."""
    record = HotRecord(
        system_id="test-system",
        conversation_id="test-conv",
        content="Test content",
        embedding=[0.0] * 384,
        timestamp=datetime.now(UTC),
        metadata={},
    )

    await hot_store.insert(record)

    # Verify insertion
    # TODO: Add get_conversation method to verify


@pytest.mark.asyncio
async def test_search_similar(hot_store):
    """Test vector similarity search."""
    # Insert test conversations
    # TODO: Implement
```

**Acceptance Criteria**:
- [ ] Unit tests for hot store
- [ ] Unit tests for warm store
- [ ] Unit tests for ingestion worker
- [ ] >85% code coverage

---

## Deployment

### Local Development

```bash
# Run server locally
uv run python -m akasha.server

# Run tests
uv run pytest

# Run linting
uv run crackerjack lint
```

### Kubernetes (Future)

See `k8s/deployment.yaml` for Kubernetes deployment manifests.

---

## Checklist

### Week 1
- [ ] Task 1.1: Project setup ✅
- [ ] Task 1.2: Configuration management
- [ ] Task 1.3: Data models

### Week 2
- [ ] Task 2.1: Hot store implementation
- [ ] Task 2.2: Warm store implementation
- [ ] Task 2.3: Sharding strategy

### Week 3
- [ ] Task 3.1: Ingestion worker
- [ ] Task 3.2: Upload discovery
- [ ] Task 3.3: Deduplication service

### Week 4
- [ ] Task 4.1: REST API with FastAPI
- [ ] Task 4.2: MCP server integration
- [ ] Task 4.3: End-to-end testing

---

## Next Steps

After completing Phase 1, proceed to:
- **Phase 2**: Advanced Features (Vector indexing, time-series, knowledge graph)
- **Phase 3**: Production Hardening (Circuit breakers, monitoring)
- **Phase 4**: Scale Preparation (Milvus, TimescaleDB, Neo4j)

See [ADR-001](ADR_001_ARCHITECTURE_DECISIONS.md) for complete architecture decisions.
