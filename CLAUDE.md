# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is **Akasha** (आकाश), a universal memory aggregation system for the Session-Buddy ecosystem. It ingests, stores, analyzes, and provides cross-system intelligence from 100-10,000 Session-Buddy instances.

**Scale**: 10TB-1PB with 100M-1B vector embeddings
**Purpose**: Cross-system intelligence, trend analysis, distributed pattern recognition

## Development Commands

### Installation & Setup

```bash
# Install all dependencies (development + production)
uv sync --group dev

# Run server locally
python -m akasha.server

# Verify installation
python -c "from akasha.storage import HotStore; print('✅ Akasha ready')"
```

### Quick Start Development

```bash
# Complete development setup
uv sync --group dev && \
  pytest -m "not slow" && \
  crackerjack lint
```

### Code Quality & Linting

```bash
# Lint and format code
crackerjack lint

# Type checking
crackerjack typecheck

# Security scanning
crackerjack security

# Full quality analysis
crackerjack analyze
```

### Testing & Development

```bash
# Run comprehensive test suite
pytest

# Quick smoke tests (exclude slow)
pytest -m "not slow"

# Run specific test categories
pytest tests/unit/                    # Unit tests only
pytest tests/integration/             # Integration tests only
pytest -m performance                 # Performance tests only

# Coverage reporting
pytest --cov=akasha --cov-report=term-missing

# Development debugging mode
pytest -v --tb=short
```

## Architecture Overview

### System Boundaries & Responsibilities

**Session-Buddy**:
- ✅ User memory collection per system
- ✅ Upload system memories to cloud storage (S3/Azure/GCS)
- ❌ Does NOT sync to Akasha directly (uses cloud as buffer)

**Mahavishnu**:
- ✅ Orchestrates Akasha workflows via MCP
- ✅ Schedules ingestion jobs and health checks
- ✅ Scales Akasha pods based on backlog metrics
- ❌ Does NOT execute ingestion logic

**Oneiric**:
- ✅ Universal storage abstraction (multi-cloud)
- ✅ Storage adapters: file, S3, Azure, GCS, Redis
- ❌ Does NOT handle memory processing

**Akasha**:
- ✅ Ingests system memories from cloud storage (pull model)
- ✅ Cross-system deduplication (exact + fuzzy)
- ✅ Vector indexing with HNSW
- ✅ Time-series aggregation
- ✅ Knowledge graph construction
- ✅ Serves queries (MCP + REST APIs)
- ❌ Does NOT schedule its own workflows (Mahavishnu handles)

### Three-Tier Storage Architecture

**Hot Tier (0-7 days)**:
- Storage: DuckDB in-memory + Redis cache
- Purpose: Real-time search, recent analytics
- Characteristics: Full embeddings, sub-100ms latency
- Access: Most frequent queries

**Warm Tier (7-90 days)**:
- Storage: DuckDB on-disk (NVMe SSD)
- Purpose: Historical analytics, trend analysis
- Characteristics: Compressed embeddings (INT8), 100-500ms latency
- Access: Aggregations and historical queries

**Cold Tier (90+ days)**:
- Storage: Parquet files in S3/Azure/GCS
- Purpose: Compliance, archival, deep analytics
- Characteristics: Summaries only, no embeddings, cost-optimized
- Access: Rare archival queries

### Technology Evolution Path

**Phase 1** (0-100 systems, <10M embeddings):
- Vector: DuckDB with HNSW
- Time-Series: DuckDB
- Knowledge Graph: DuckDB + Redis

**Phase 2** (100-1,000 systems, 10M-100M embeddings):
- Vector: Add Milvus for warm tier
- Time-Series: Add TimescaleDB
- Knowledge Graph: DuckDB + Redis

**Phase 3** (1,000-10,000 systems, 100M-1B embeddings):
- Vector: Milvus cluster
- Time-Series: TimescaleDB + read replicas
- Knowledge Graph: Add Neo4j

**Phase 4** (10,000+ systems, 1B+ embeddings):
- Consider cloud-native services (AWS OpenSearch, Azure AI Search)

## Core Components

### Storage Layer (`akasha/storage/`)

- **hot_store.py**: DuckDB in-memory for recent data
- **warm_store.py**: DuckDB on-disk for historical data
- **cold_store.py**: Oneiric-based Parquet export for archival
- **sharding.py**: Consistent hashing router (256 shards)
- **aging.py**: Hot→Warm→Cold tier migration service

### Ingestion Pipeline (`akasha/ingestion/`)

- **worker.py**: Pull-based ingestion from cloud storage
- **discovery.py**: Upload discovery via Oneiric
- **orchestrator.py**: Multi-worker coordinator

### Processing Services (`akasha/processing/`)

- **deduplication.py**: Exact SHA-256 + MinHash fuzzy matching
- **enrichment.py**: Metadata enrichment (system profiles, geo-tags)
- **vector_indexer.py**: HNSW index management
- **time_series.py**: Hourly/daily aggregation with trend detection
- **knowledge_graph.py**: Entity extraction and relationship linking

### Query Layer (`akasha/query/`)

- **distributed.py**: Fan-out query engine across shards
- **aggregator.py**: Result merging and re-ranking
- **faceted.py**: Faceted search with filters

### Cache Layer (`akasha/cache/`)

- **layered_cache.py**: L1 (memory) + L2 (Redis) caching

### API Layer (`akasha/api/`)

- **routes.py**: FastAPI route definitions
- **middleware.py**: Authentication, logging, rate limiting

## Configuration

### Environment Variables

```bash
# Storage Configuration
AKASHA_HOT_PATH=/data/akasha/hot
AKASHA_WARM_PATH=/data/akasha/warm
AKASHA_COLD_BUCKET=akasha-cold-data

# Oneiric Storage (Cold Tier)
S3_BUCKET=akasha-cold-data
S3_REGION=us-west-2

# Cache Layer
REDIS_HOST=redis.cache.local
REDIS_PORT=6379
REDIS_DB=0

# API Configuration
AKASHA_API_PORT=8000
AKASHA_MCP_PORT=3001

# Mahavishnu Integration
MAHAVISHNU_MCP_URL=http://mahavishnu:3000
```

### Configuration Files

- `config/akasha.yaml` - Main configuration
- `config/akasha_storage.yaml` - Storage backend configuration
- `config/akasha_secrets.yaml` - Secrets (not in git)

## Development Guidelines

### Type Safety

- **Always use comprehensive type hints** with modern Python 3.13+ syntax
- Import typing as `import typing as t` and prefix all typing references
- Use built-in collection types: `list[str]` instead of `t.List[str]`
- Use pipe operator for unions: `str | None` instead of `Optional[str]`

### Async/Await Patterns

- **Always use async/await** for database and storage operations
- Use executor threads for blocking I/O operations
- Follow the hybrid pattern: async signature, sync operation internally

```python
async def store_conversation(content: str) -> str:
    """Async signature for API consistency."""
    # Fast local operation (<1ms)
    conn = self._get_conn()  # Sync connection
    conn.execute("INSERT INTO conversations ...")  # Sync operation
    return conversation_id
```

### Error Handling

- **Never suppress exceptions silently**
- Use structured error messages with context
- Implement graceful degradation with fallbacks
- Log all errors with structured logging

### Testing Patterns

- **Unit tests**: Test individual functions in isolation
- **Integration tests**: Test component interactions
- **Performance tests**: Benchmark critical paths
- **Test coverage**: Maintain >85% coverage

## Key Design Patterns

### Ingestion Pattern (Pull Model)

```python
# 1. Session-Buddy uploads to cloud (no Akasha dependency)
await oneiric_storage.upload(
    bucket="session-buddy-memories",
    path=f"systems/{system_id}/memory.db",
    data=memory_db
)

# 2. Akasha worker pulls from cloud (independent)
uploads = await oneiric_storage.list_prefixes(
    bucket="session-buddy-memories",
    prefix=f"systems/{system_id}/"
)
await process_uploads(uploads)
```

### Tier Aging Pattern

```python
# Hot → Warm: Compress and summarize
warm_record = WarmRecord(
    embedding=quantize_to_int8(hot_record.embedding),  # FLOAT → INT8
    summary=extractive_summary(hot_record.content, num_sentences=3),
)

# Warm → Cold: Ultra-compress for archival
cold_record = ColdRecord(
    fingerprint=minhash_fingerprint(warm_record.summary),
    ultra_summary=single_sentence_summary(warm_record.summary),
)
```

### Distributed Query Pattern

```python
# Fan-out to shards
tasks = [search_shard(shard_id, query) for shard_id in target_shards]
results = await asyncio.gather(*tasks)

# Merge and re-rank
all_results = merge_results(results)
all_results.sort(key=lambda r: r["similarity"], reverse=True)
return all_results[:limit]
```

## Troubleshooting

### Common Issues

**Issue**: Ingestion backlog growing
```bash
# Check backlog size
curl http://akasha:8000/api/v1/metrics | jq .akasha_backlog_count

# Scale up pods
kubectl scale deployment akasha-ingestion --replicas=10
```

**Issue**: Search latency high
```bash
# Check hot store health
curl http://akasha:8000/health

# Check cache hit rate
curl http://akasha:8000/api/v1/metrics | jq .akasha_cache_hit_rate
```

**Issue**: Memory usage high
```bash
# Check hot store size
du -sh /data/akasha/hot

# Trigger manual aging
python -m akasha.scripts.trigger_aging
```

## Integration with Ecosystem

### Session-Buddy Integration

Akasha automatically ingests from Session-Buddy uploads:

```text
Session-Buddy → S3://session-buddy-memories/systems/{id}/
                                  ↓
                            Akasha Worker (polls)
                                  ↓
                           Hot Store → Warm Store → Cold Store
```

### Mahavishnu Integration

Akasha registers workflows with Mahavishnu:

```python
# Akasha startup
await register_with_mahavishnu(
    workflows=[
        "akasha-daily-ingest",
        "akasha-tier-transition",
        "akasha-health-check",
    ]
)
```

### Oneiric Integration

Akasha uses Oneiric storage adapters:

```python
from oneiric.adapters import StorageAdapter

# Resolve storage backend via Oneiric
storage = await bridge.use("storage-s3-cold")
await storage.instance.upload(
    bucket="akasha-cold",
    path="conversations/...",
    data=parquet_bytes
)
```

## Performance Benchmarks

### Target Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| **Ingestion Throughput** | 1000 uploads/minute | `pytest tests/performance/test_ingestion.py` |
| **Search Latency (p50)** | <50ms | `pytest tests/performance/test_search.py` |
| **Search Latency (p99)** | <200ms | Same benchmark |
| **Hot→Warm Aging** | <1 hour for 1TB | Aging service benchmark |
| **Cache Hit Rate** | >50% | Production metrics |

## Architecture Decision Records

All major architectural decisions are documented in:
- **[ADR-001: Architecture Decisions](docs/ADR_001_ARCHITECTURE_DECISIONS.md)**

## References

- **Architecture Decisions**: [ADR-001](docs/ADR_001_ARCHITECTURE_DECISIONS.md)
- **Implementation Guide**: [IMPLEMENTATION_GUIDE.md](docs/IMPLEMENTATION_GUIDE.md)
- **API Reference**: [API_REFERENCE.md](docs/API_REFERENCE.md)
- **Session-Buddy**: https://github.com/yourorg/session-buddy
- **Mahavishnu**: https://github.com/yourorg/mahavishnu
- **Oneiric**: https://github.com/yourorg/oneiric
