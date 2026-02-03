# Prometheus Metrics for Akosha

## Overview

Akosha includes a comprehensive Prometheus metrics system for monitoring all critical operations including ingestion, search, caching, storage tiers, errors, and more.

## Location

- **Metrics Module**: `/Users/les/Projects/akosha/akosha/observability/prometheus_metrics.py`
- **Tests**: `/Users/les/Projects/akosha/tests/unit/test_prometheus_metrics.py`

## Available Metrics

### Ingestion Metrics

- `akosha_ingestion_throughput` - Records processed per second by system and status (gauge)
- `akosha_ingestion_bytes_total` - Total bytes ingested by system (counter)
- `akosha_ingestion_duration_seconds` - Time spent ingesting records (histogram)

### Search Metrics

- `akosha_search_latency_milliseconds` - Search operation latency with percentiles (histogram)
- `akosha_search_results_total` - Total number of searches performed (counter)
- `akosha_search_result_count` - Number of results returned per search (histogram)

### Cache Metrics

- `akosha_cache_operations_total` - Cache hits and misses by tier and query type (counter)
- `akosha_cache_hit_rate` - Cache effectiveness ratio 0-1 by tier and query type (gauge)
- `akosha_cache_size_bytes` - Current cache size in bytes by tier (gauge)
- `akosha_cache_entry_count` - Number of entries in cache by tier (gauge)

### Storage Tier Metrics

- `akosha_store_size_records` - Number of records in each storage tier (gauge)
- `akosha_store_size_bytes` - Storage size in bytes by tier (gauge)
- `akosha_store_operations_total` - Storage operations by tier, operation, and status (counter)
- `akosha_store_operation_duration_seconds` - Storage operation duration (histogram)

### Error Metrics

- `akosha_errors_total` - Total errors by component, type, and severity (counter)
- `akosha_error_last_timestamp_seconds` - Unix timestamp of last error (gauge)

### General Operation Metrics

- `akosha_operations_total` - Total operations by type and status (counter)
- `akosha_operation_duration_seconds` - Operation duration in seconds (histogram)

### Deduplication Metrics

- `akosha_deduplication_checks_total` - Deduplication checks performed (counter)
- `akosha_deduplication_duration_seconds` - Time spent on deduplication checks (histogram)

### Embedding Metrics

- `akosha_embedding_generation_duration_seconds` - Time spent generating embeddings (histogram)
- `akosha_embedding_batch_size` - Number of embeddings processed per batch (histogram)

### Vector Index Metrics

- `akosha_vector_index_size_vectors` - Number of vectors in the index (gauge)
- `akosha_vector_index_build_duration_seconds` - Time spent building vector index (histogram)

### Knowledge Graph Metrics

- `akosha_knowledge_graph_entities_total` - Total number of entities in knowledge graph (gauge)
- `akosha_knowledge_graph_relationships_total` - Total number of relationships (gauge)
- `akosha_knowledge_graph_query_duration_seconds` - Knowledge graph query duration (histogram)

### HTTP/MCP Server Metrics

- `akosha_http_requests_total` - Total HTTP requests (counter)
- `akosha_http_request_duration_seconds` - HTTP request latency (histogram)
- `akosha_http_active_requests` - Number of active HTTP requests (gauge)

## Usage Examples

### Basic Recording

```python
from akosha.observability import prometheus_metrics

# Record ingestion
prometheus_metrics.record_ingestion_record(
    system_id="session-buddy-123",
    status="success",
    bytes_processed=1024
)

# Update throughput periodically
prometheus_metrics.update_ingestion_throughput(
    records_per_second=100.5,
    system_id="all"
)
```

### Search Metrics

```python
# Record search latency and results
with prometheus_metrics.observe_search_latency(
    query_type="semantic",
    shard_count=3,
    tier="hot"
) as record_results:
    results = await search_store(query)
    record_results(len(results))
```

### Cache Metrics

```python
# Record cache hit/miss
if cached_result:
    prometheus_metrics.record_cache_hit(cache_tier="L1", query_type="semantic")
else:
    prometheus_metrics.record_cache_miss(cache_tier="L1", query_type="semantic")

# Update cache hit rate periodically
prometheus_metrics.update_cache_hit_rate(
    hit_rate=0.85,
    cache_tier="L1",
    query_type="semantic"
)
```

### Storage Metrics

```python
# Update store sizes periodically
prometheus_metrics.update_store_sizes(
    hot_size=1_000_000,
    warm_size=50_000_000,
    cold_size=500_000_000,
    hot_bytes=1024 * 1024 * 1024,  # 1 GB
    warm_bytes=1024 * 1024 * 1024 * 50,  # 50 GB
    cold_bytes=1024 * 1024 * 1024 * 500  # 500 GB
)

# Observe storage operations
with prometheus_metrics.observe_store_operation("hot", "write") as record_status:
    try:
        await hot_store.write(record)
        record_status("success")
    except Exception:
        record_status("error")
        raise
```

### Error Tracking

```python
try:
    risky_operation()
except Exception as e:
    prometheus_metrics.increment_errors(
        component="hot_store",
        error_type="database_error",
        severity="critical"
    )
    raise
```

### Starting the Metrics Server

```python
from akosha.observability import prometheus_metrics

# Start metrics HTTP server on port 8000
prometheus_metrics.start_metrics_server(port=8000)

# Or integrate with FastAPI
from fastapi import FastAPI

app = FastAPI()

@app.get("/metrics")
async def metrics():
    from prometheus_client import CONTENT_TYPE_LATEST
    from akosha.observability import prometheus_metrics

    metrics_data = prometheus_metrics.generate_metrics()
    return Response(content=metrics_data, media_type=CONTENT_TYPE_LATEST)
```

## Prometheus Configuration

Add to your `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: 'akosha'
    scrape_interval: 15s
    static_configs:
      - targets: ['localhost:8000']
    metrics_path: '/metrics'
```

## Example Grafana Queries

### Ingestion Rate

```promql
rate(akosha_ingestion_throughput{status="success"}[5m])
```

### Search Latency (p95)

```promql
histogram_quantile(0.95, rate(akosha_search_latency_milliseconds_bucket[5m]))
```

### Cache Hit Rate

```promql
akosha_cache_hit_rate{cache_tier="L1"}
```

### Store Size by Tier

```promql
akosha_store_size_records
```

### Error Rate

```promql
rate(akosha_errors_total[5m])
```

## Testing

All metrics have comprehensive unit tests:

```bash
# Run all Prometheus metrics tests
pytest tests/unit/test_prometheus_metrics.py -v

# Run specific test class
pytest tests/unit/test_prometheus_metrics.py::TestIngestionMetrics -v

# Run with coverage
pytest tests/unit/test_prometheus_metrics.py --cov=akosha/observability/prometheus_metrics
```

## Test Coverage

- **31 tests** covering all metric types
- **96.52% code coverage** for prometheus_metrics.py
- Tests for:
  - Ingestion metrics (3 tests)
  - Search metrics (3 tests)
  - Cache metrics (5 tests)
  - Storage metrics (3 tests)
  - Error metrics (3 tests)
  - Operation metrics (2 tests)
  - Deduplication metrics (2 tests)
  - Knowledge graph metrics (2 tests)
  - Vector index metrics (1 test)
  - Metrics generation (3 tests)
  - Metrics isolation (1 test)
  - Label combinations (2 tests)
  - Performance tests (1 test)

## Label Values

### System ID

- Dynamic values like `"session-buddy-123"`, `"system-456"`

### Status

- `"success"`, `"error"`, `"skipped"`

### Query Type

- `"semantic"`, `"keyword"`, `"hybrid"`, `"graph"`

### Tier

- `"hot"`, `"warm"`, `"cold"`

### Cache Tier

- `"L1"` (memory), `"L2"` (Redis)

### Component

- `"ingestion_worker"`, `"hot_store"`, `"warm_store"`, `"cold_store"`
- `"vector_indexer"`, `"cache_layer"`, `"query_engine"`
- `"deduplication"`, `"knowledge_graph"`, `"time_series"`
- `"mcp_server"`, `"aging_service"`

### Error Type

- `"database_error"`, `"network_error"`, `"storage_error"`
- `"validation_error"`, `"timeout_error"`, `"authentication_error"`
- `"rate_limit_error"`, `"serialization_error"`, `"embedding_error"`

### Severity

- `"critical"`, `"error"`, `"warning"`

### Operation

- `"read"`, `"write"`, `"delete"`, `"scan"`

## Best Practices

1. **Update gauges periodically**: Call gauge update functions (like `update_store_sizes`) on a schedule (e.g., every minute)
1. **Use context managers**: For operations, use the provided context managers (`observe_search_latency`, `observe_store_operation`)
1. **Label consistency**: Use consistent label values across your application
1. **Performance**: Metrics recording is fast (\<1ms per operation), but avoid recording in extremely tight loops
1. **Testing**: Use `reset_all_metrics()` in tests to ensure isolation

## Architecture

- Uses `prometheus_client` library for metric collection
- Singleton registry pattern ensures all metrics use the same registry
- Thread-safe operations with proper locking
- Context managers for automatic metric recording
- Comprehensive type hints for all functions
- Well-documented with examples in docstrings

## Integration Points

### With Storage Layer

Metrics are automatically recorded when:

- Records are ingested into hot/warm/cold stores
- Cache hits/misses occur
- Storage operations (read/write/delete) are performed

### With Query Layer

Metrics are recorded for:

- Search latency by query type and tier
- Result counts
- Query success/failure rates

### With Ingestion Pipeline

Metrics track:

- Throughput (records/second)
- Bytes processed
- Ingestion duration by operation type

### With Error Handling

All errors are tracked with:

- Component identification
- Error categorization
- Severity levels
- Timestamps
