"""Prometheus metrics collection and export for Akosha.

This module provides production-ready Prometheus metrics for monitoring Akosha's
critical operations including ingestion, search, caching, storage tiers, and errors.

Example usage:

    ```python
    from akosha.observability.prometheus_metrics import (
        record_ingestion_record,
        observe_search_latency,
        record_cache_hit,
        record_cache_miss,
        update_store_sizes,
        increment_errors,
        get_metrics_registry,
    )
    from prometheus_client import start_http_server

    # Start metrics endpoint for Prometheus scraping
    start_http_server(8000)

    # Record metrics during operations
    async def ingest_memory(memory_data):
        record_ingestion_record(system_id="sys-123", status="success")
        # ... ingestion logic

    async def search_memory(query: str):
        with observe_search_latency(
            query_type="semantic",
            shard_count=3,
        ):
            # ... search logic
            results = await perform_search(query)
            return results

    # Record cache operations
    if cached:
        record_cache_hit(cache_tier="L1", query_type="semantic")
    else:
        record_cache_miss(cache_tier="L1", query_type="semantic")

    # Update store sizes (call periodically)
    update_store_sizes(
        hot_size=1_000_000,
        warm_size=50_000_000,
        cold_size=500_000_000,
    )

    # Record errors
    try:
        risky_operation()
    except Exception as e:
        increment_errors(
            component="ingestion_worker",
            error_type="database_error",
            severity="critical",
        )
        raise

    # Get metrics registry for custom exporters
    from prometheus_client import CollectorRegistry
    registry = get_metrics_registry()
    ```

Metrics exposed:
    - akosha_ingestion_throughput: Records per second by system and status
    - akosha_search_latency: Search latency in ms with p50, p95, p99 quantiles
    - akosha_cache_operations: Cache hits/misses by tier and query type
    - akosha_cache_hit_rate: Cache effectiveness ratio (0-1)
    - akosha_store_size: Record count by storage tier
    - akosha_errors: Error count by component, type, and severity
    - akosha_operations_total: Total operations by type and status
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock
from typing import Callable, Literal

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

logger = logging.getLogger(__name__)

# Singleton registry for all Akosha metrics
_registry: CollectorRegistry | None = None
_registry_lock = Lock()


def get_metrics_registry() -> CollectorRegistry:
    """Get or create the singleton Prometheus metrics registry.

    Returns:
        The shared CollectorRegistry for all Akosha metrics.
    """
    global _registry

    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = CollectorRegistry()
                logger.info("Created Prometheus metrics registry")

    return _registry


# ============================================================================
# Ingestion Metrics
# ============================================================================

ingestion_throughput: Gauge = Gauge(
    name="akosha_ingestion_throughput",
    documentation="Records processed per second by system and status",
    labelnames=["system_id", "status"],
    registry=get_metrics_registry(),
)

ingestion_bytes_total: Counter = Counter(
    name="akosha_ingestion_bytes_total",
    documentation="Total bytes ingested by system",
    labelnames=["system_id"],
    registry=get_metrics_registry(),
)

ingestion_duration_seconds: Histogram = Histogram(
    name="akosha_ingestion_duration_seconds",
    documentation="Time spent ingesting records in seconds",
    labelnames=["system_id", "operation"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
    registry=get_metrics_registry(),
)


def record_ingestion_record(
    system_id: str,
    status: Literal["success", "error", "skipped"],
    bytes_processed: int = 0,
) -> None:
    """Record a single ingestion event.

    Args:
        system_id: Identifier for the source system (e.g., "session-buddy-123")
        status: Status of the ingestion operation
        bytes_processed: Number of bytes processed (for size tracking)
    """
    ingestion_throughput.labels(
        system_id=system_id,
        status=status,
    ).inc()

    if bytes_processed > 0:
        ingestion_bytes_total.labels(
            system_id=system_id,
        ).inc(bytes_processed)


def update_ingestion_throughput(
    records_per_second: float,
    system_id: str = "all",
) -> None:
    """Update the ingestion throughput gauge.

    Call this periodically (e.g., every 10 seconds) with the calculated throughput.

    Args:
        records_per_second: Current processing rate
        system_id: System identifier or "all" for aggregate
    """
    ingestion_throughput.labels(
        system_id=system_id,
        status="success",
    ).set(records_per_second)


# ============================================================================
# Search Metrics
# ============================================================================

search_latency: Histogram = Histogram(
    name="akosha_search_latency_milliseconds",
    documentation="Search operation latency in milliseconds with percentiles",
    labelnames=["query_type", "shard_count", "tier"],
    buckets=(
        1.0,
        5.0,
        10.0,
        25.0,
        50.0,
        75.0,
        100.0,
        150.0,
        200.0,
        300.0,
        500.0,
        750.0,
        1000.0,
        2000.0,
        5000.0,
    ),
    registry=get_metrics_registry(),
)

search_results_total: Counter = Counter(
    name="akosha_search_results_total",
    documentation="Total number of searches performed",
    labelnames=["query_type", "tier", "has_results"],
    registry=get_metrics_registry(),
)

search_result_count: Histogram = Histogram(
    name="akosha_search_result_count",
    documentation="Number of results returned per search",
    labelnames=["query_type", "tier"],
    buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000),
    registry=get_metrics_registry(),
)


@contextmanager
def observe_search_latency(
    query_type: Literal["semantic", "keyword", "hybrid", "graph"],
    shard_count: int,
    tier: Literal["hot", "warm", "cold"] = "hot",
) -> Iterator[Callable[[int], None]]:
    """Context manager for measuring search latency.

    Automatically records the search operation duration and result count.

    Args:
        query_type: Type of search query
        shard_count: Number of shards queried
        tier: Storage tier queried

    Yields:
        A function to call with the number of results found

    Example:
        ```python
        with observe_search_latency("semantic", 3, "hot") as record_results:
            results = await search_store(query)
            record_results(len(results))
        ```
    """
    start_time = time.time()

    def record_results(count: int) -> None:
        """Record the number of results returned."""
        has_results = "true" if count > 0 else "false"
        search_results_total.labels(
            query_type=query_type,
            tier=tier,
            has_results=has_results,
        ).inc()
        search_result_count.labels(
            query_type=query_type,
            tier=tier,
        ).observe(count)

    yield record_results

    duration_ms = (time.time() - start_time) * 1000
    search_latency.labels(
        query_type=query_type,
        shard_count=str(shard_count),
        tier=tier,
    ).observe(duration_ms)


# ============================================================================
# Cache Metrics
# ============================================================================

cache_operations: Counter = Counter(
    name="akosha_cache_operations_total",
    documentation="Cache operations (hits and misses) by tier and query type",
    labelnames=["operation", "cache_tier", "query_type"],
    registry=get_metrics_registry(),
)

cache_hit_rate: Gauge = Gauge(
    name="akosha_cache_hit_rate",
    documentation="Cache hit rate ratio (0-1) by cache tier and query type",
    labelnames=["cache_tier", "query_type"],
    registry=get_metrics_registry(),
)

cache_size_bytes: Gauge = Gauge(
    name="akosha_cache_size_bytes",
    documentation="Current cache size in bytes by tier",
    labelnames=["cache_tier"],
    registry=get_metrics_registry(),
)

cache_entry_count: Gauge = Gauge(
    name="akosha_cache_entry_count",
    documentation="Number of entries in cache by tier",
    labelnames=["cache_tier"],
    registry=get_metrics_registry(),
)


def record_cache_hit(
    cache_tier: Literal["L1", "L2"],
    query_type: Literal["semantic", "keyword", "hybrid", "graph"] = "semantic",
) -> None:
    """Record a cache hit operation.

    Args:
        cache_tier: Cache tier that was hit (L1=memory, L2=Redis)
        query_type: Type of query that hit the cache
    """
    cache_operations.labels(
        operation="hit",
        cache_tier=cache_tier,
        query_type=query_type,
    ).inc()


def record_cache_miss(
    cache_tier: Literal["L1", "L2"],
    query_type: Literal["semantic", "keyword", "hybrid", "graph"] = "semantic",
) -> None:
    """Record a cache miss operation.

    Args:
        cache_tier: Cache tier that was checked
        query_type: Type of query that missed the cache
    """
    cache_operations.labels(
        operation="miss",
        cache_tier=cache_tier,
        query_type=query_type,
    ).inc()


def update_cache_hit_rate(
    hit_rate: float,
    cache_tier: Literal["L1", "L2"],
    query_type: Literal["semantic", "keyword", "hybrid", "graph"] = "semantic",
) -> None:
    """Update the cache hit rate gauge.

    Call this periodically with the calculated hit rate.

    Args:
        hit_rate: Hit rate as a ratio between 0 and 1
        cache_tier: Cache tier to update
        query_type: Query type for the hit rate
    """
    clamped_rate = max(0.0, min(1.0, hit_rate))
    cache_hit_rate.labels(
        cache_tier=cache_tier,
        query_type=query_type,
    ).set(clamped_rate)


def update_cache_size(
    size_bytes: int,
    cache_tier: Literal["L1", "L2"],
) -> None:
    """Update the current cache size in bytes.

    Args:
        size_bytes: Current cache size in bytes
        cache_tier: Cache tier to update
    """
    cache_size_bytes.labels(
        cache_tier=cache_tier,
    ).set(size_bytes)


def update_cache_entry_count(
    count: int,
    cache_tier: Literal["L1", "L2"],
) -> None:
    """Update the current cache entry count.

    Args:
        count: Number of entries in the cache
        cache_tier: Cache tier to update
    """
    cache_entry_count.labels(
        cache_tier=cache_tier,
    ).set(count)


# ============================================================================
# Storage Tier Metrics
# ============================================================================

store_size: Gauge = Gauge(
    name="akosha_store_size_records",
    documentation="Number of records in each storage tier",
    labelnames=["tier"],
    registry=get_metrics_registry(),
)

store_size_bytes: Gauge = Gauge(
    name="akosha_store_size_bytes",
    documentation="Storage size in bytes by tier",
    labelnames=["tier"],
    registry=get_metrics_registry(),
)

store_operations: Counter = Counter(
    name="akosha_store_operations_total",
    documentation="Storage operations (read/write/delete) by tier",
    labelnames=["tier", "operation", "status"],
    registry=get_metrics_registry(),
)

store_operation_duration: Histogram = Histogram(
    name="akosha_store_operation_duration_seconds",
    documentation="Storage operation duration in seconds",
    labelnames=["tier", "operation"],
    buckets=(
        0.0001,
        0.0005,
        0.001,
        0.005,
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        1.0,
        2.5,
        5.0,
    ),
    registry=get_metrics_registry(),
)


def update_store_sizes(
    hot_size: int,
    warm_size: int,
    cold_size: int,
    hot_bytes: int | None = None,
    warm_bytes: int | None = None,
    cold_bytes: int | None = None,
) -> None:
    """Update storage tier record counts.

    Call this periodically (e.g., every minute) with current sizes.

    Args:
        hot_size: Number of records in hot store (0-7 days)
        warm_size: Number of records in warm store (7-90 days)
        cold_size: Number of records in cold store (90+ days)
        hot_bytes: Hot store size in bytes (optional)
        warm_bytes: Warm store size in bytes (optional)
        cold_bytes: Cold store size in bytes (optional)
    """
    store_size.labels(tier="hot").set(hot_size)
    store_size.labels(tier="warm").set(warm_size)
    store_size.labels(tier="cold").set(cold_size)

    if hot_bytes is not None:
        store_size_bytes.labels(tier="hot").set(hot_bytes)
    if warm_bytes is not None:
        store_size_bytes.labels(tier="warm").set(warm_bytes)
    if cold_bytes is not None:
        store_size_bytes.labels(tier="cold").set(cold_bytes)


@contextmanager
def observe_store_operation(
    tier: Literal["hot", "warm", "cold"],
    operation: Literal["read", "write", "delete", "scan"],
) -> Iterator[Callable[[Literal["success", "error"]], None]]:
    """Context manager for measuring storage operation duration.

    Args:
        tier: Storage tier being accessed
        operation: Type of storage operation

    Yields:
        A function to call with the operation status

    Example:
        ```python
        with observe_store_operation("hot", "write") as record_status:
            try:
                await hot_store.write(record)
                record_status("success")
            except Exception as e:
                record_status("error")
                raise
        ```
    """
    start_time = time.time()

    def record_status(status: Literal["success", "error"]) -> None:
        """Record the operation status."""
        store_operations.labels(
            tier=tier,
            operation=operation,
            status=status,
        ).inc()

    yield record_status

    duration = time.time() - start_time
    store_operation_duration.labels(
        tier=tier,
        operation=operation,
    ).observe(duration)


# ============================================================================
# Error Metrics
# ============================================================================

error_total: Counter = Counter(
    name="akosha_errors_total",
    documentation="Total errors by component, type, and severity",
    labelnames=["component", "error_type", "severity"],
    registry=get_metrics_registry(),
)

error_last_timestamp: Gauge = Gauge(
    name="akosha_error_last_timestamp_seconds",
    documentation="Unix timestamp of last error by component and type",
    labelnames=["component", "error_type"],
    registry=get_metrics_registry(),
)


def increment_errors(
    component: Literal[
        "ingestion_worker",
        "ingestion_orchestrator",
        "hot_store",
        "warm_store",
        "cold_store",
        "vector_indexer",
        "cache_layer",
        "query_engine",
        "deduplication",
        "knowledge_graph",
        "time_series",
        "mcp_server",
        "aging_service",
    ],
    error_type: Literal[
        "database_error",
        "network_error",
        "storage_error",
        "validation_error",
        "timeout_error",
        "authentication_error",
        "rate_limit_error",
        "serialization_error",
        "embedding_error",
        "unknown_error",
    ],
    severity: Literal["critical", "error", "warning"] = "error",
) -> None:
    """Increment the error counter.

    Args:
        component: Component that raised the error
        error_type: Type/category of error
        severity: Error severity level
    """
    error_total.labels(
        component=component,
        error_type=error_type,
        severity=severity,
    ).inc()

    # Update last error timestamp
    import time as time_module

    error_last_timestamp.labels(
        component=component,
        error_type=error_type,
    ).set(time_module.time())


# ============================================================================
# General Operation Metrics
# ============================================================================

operations_total: Counter = Counter(
    name="akosha_operations_total",
    documentation="Total operations by type and status",
    labelnames=["operation_type", "status"],
    registry=get_metrics_registry(),
)

operation_duration: Histogram = Histogram(
    name="akosha_operation_duration_seconds",
    documentation="Operation duration in seconds",
    labelnames=["operation_type"],
    buckets=(
        0.001,
        0.005,
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        1.0,
        2.5,
        5.0,
        10.0,
        30.0,
        60.0,
        300.0,
    ),
    registry=get_metrics_registry(),
)


@contextmanager
def observe_operation(
    operation_type: str,
) -> Iterator[Callable[[Literal["success", "error"]], None]]:
    """Context manager for measuring any operation duration and status.

    Args:
        operation_type: Type of operation being measured

    Yields:
        A function to call with the operation status

    Example:
        ```python
        with observe_operation("embedding_generation") as record_status:
            try:
                embedding = await generate_embedding(text)
                record_status("success")
            except Exception:
                record_status("error")
                raise
        ```
    """
    start_time = time.time()

    def record_status(status: Literal["success", "error"]) -> None:
        """Record the operation status."""
        operations_total.labels(
            operation_type=operation_type,
            status=status,
        ).inc()

    yield record_status

    duration = time.time() - start_time
    operation_duration.labels(operation_type=operation_type).observe(duration)


# ============================================================================
# Deduplication Metrics
# ============================================================================

deduplication_checks: Counter = Counter(
    name="akosha_deduplication_checks_total",
    documentation="Deduplication checks performed",
    labelnames=["check_type", "result"],
    registry=get_metrics_registry(),
)

deduplication_duration: Histogram = Histogram(
    name="akosha_deduplication_duration_seconds",
    documentation="Time spent on deduplication checks",
    labelnames=["check_type"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    registry=get_metrics_registry(),
)


def record_deduplication_check(
    check_type: Literal["exact", "fuzzy"],
    result: Literal["duplicate", "unique"],
) -> None:
    """Record a deduplication check result.

    Args:
        check_type: Type of deduplication check performed
        result: Result of the check
    """
    deduplication_checks.labels(
        check_type=check_type,
        result=result,
    ).inc()


# ============================================================================
# Embedding Metrics
# ============================================================================

embedding_generation_duration: Histogram = Histogram(
    name="akosha_embedding_generation_duration_seconds",
    documentation="Time spent generating embeddings",
    labelnames=["model_type"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=get_metrics_registry(),
)

embedding_batch_size: Histogram = Histogram(
    name="akosha_embedding_batch_size",
    documentation="Number of embeddings processed per batch",
    buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000),
    registry=get_metrics_registry(),
)


# ============================================================================
# Vector Index Metrics
# ============================================================================

vector_index_size: Gauge = Gauge(
    name="akosha_vector_index_size_vectors",
    documentation="Number of vectors in the index",
    labelnames=["index_name"],
    registry=get_metrics_registry(),
)

vector_index_build_duration: Histogram = Histogram(
    name="akosha_vector_index_build_duration_seconds",
    documentation="Time spent building vector index",
    labelnames=["index_name"],
    buckets=(1.0, 5.0, 10.0, 30.0, 60.0, 300.0, 600.0, 1800.0),
    registry=get_metrics_registry(),
)


# ============================================================================
# Knowledge Graph Metrics
# ============================================================================

knowledge_graph_entities: Gauge = Gauge(
    name="akosha_knowledge_graph_entities_total",
    documentation="Total number of entities in knowledge graph",
    registry=get_metrics_registry(),
)

knowledge_graph_relationships: Gauge = Gauge(
    name="akosha_knowledge_graph_relationships_total",
    documentation="Total number of relationships in knowledge graph",
    registry=get_metrics_registry(),
)

knowledge_graph_query_duration: Histogram = Histogram(
    name="akosha_knowledge_graph_query_duration_seconds",
    documentation="Knowledge graph query duration",
    labelnames=["query_type"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    registry=get_metrics_registry(),
)


# ============================================================================
# HTTP/MCP Server Metrics
# ============================================================================

http_requests_total: Counter = Counter(
    name="akosha_http_requests_total",
    documentation="Total HTTP requests",
    labelnames=["method", "endpoint", "status"],
    registry=get_metrics_registry(),
)

http_request_duration: Histogram = Histogram(
    name="akosha_http_request_duration_seconds",
    documentation="HTTP request latency in seconds",
    labelnames=["method", "endpoint"],
    buckets=(
        0.005,
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        1.0,
        2.5,
        5.0,
        10.0,
    ),
    registry=get_metrics_registry(),
)

http_active_requests: Gauge = Gauge(
    name="akosha_http_active_requests",
    documentation="Number of active HTTP requests",
    registry=get_metrics_registry(),
)


# ============================================================================
# Metrics Endpoint
# ============================================================================


def generate_metrics() -> bytes:
    """Generate Prometheus metrics exposition format.

    Returns:
        Metrics in Prometheus text exposition format as bytes.

    Example:
        ```python
        from fastapi import FastAPI
        from akosha.observability.prometheus_metrics import generate_metrics

        app = FastAPI()

        @app.get("/metrics")
        async def metrics():
            from fastapi.responses import Response
            return Response(content=generate_metrics(), media_type="text/plain")
        ```
    """
    from prometheus_client import generate_latest

    return generate_latest(get_metrics_registry())


def start_metrics_server(port: int = 8000, addr: str = "0.0.0.0") -> None:
    """Start the Prometheus metrics HTTP server.

    This runs a blocking HTTP server that exposes metrics on /metrics.

    Args:
        port: Port to listen on (default: 8000)
        addr: Address to bind to (default: 0.0.0.0)

    Example:
        ```python
        import asyncio

        def run_metrics_server():
            from akosha.observability.prometheus_metrics import start_metrics_server

            # Start in background
            import threading
            thread = threading.Thread(
                target=start_metrics_server,
                kwargs={"port": 8000},
                daemon=True,
            )
            thread.start()

        asyncio.run(run_metrics_server())
        ```
    """
    from prometheus_client import start_http_server

    start_http_server(port=port, addr=addr)
    logger.info(f"Prometheus metrics server started on http://{addr}:{port}/metrics")


# ============================================================================
# Utilities
# ============================================================================


def reset_all_metrics() -> None:
    """Reset all metrics to zero.

    WARNING: This is primarily useful for testing. Do not use in production.

    Example:
        ```python
        import pytest

        @pytest.fixture(autouse=True)
        def reset_metrics():
            from akosha.observability.prometheus_metrics import reset_all_metrics

            yield
            reset_all_metrics()
        ```
    """
    # Clear all metrics by re-creating the registry
    global _registry
    with _registry_lock:
        _registry = CollectorRegistry()
        # Re-register all metrics with new registry
        _reregister_metrics(_registry)

    logger.warning("All Prometheus metrics reset to zero")


def _reregister_metrics(registry: CollectorRegistry) -> None:
    """Re-register all metrics with the given registry.

    This is called internally after reset_all_metrics().

    Args:
        registry: The new registry to register metrics with
    """
    global \
        ingestion_throughput, \
        ingestion_bytes_total, \
        ingestion_duration_seconds, \
        search_latency, \
        search_results_total, \
        search_result_count, \
        cache_operations, \
        cache_hit_rate, \
        cache_size_bytes, \
        cache_entry_count, \
        store_size, \
        store_size_bytes, \
        store_operations, \
        store_operation_duration, \
        error_total, \
        error_last_timestamp, \
        operations_total, \
        operation_duration, \
        deduplication_checks, \
        deduplication_duration, \
        embedding_generation_duration, \
        embedding_batch_size, \
        vector_index_size, \
        vector_index_build_duration, \
        knowledge_graph_entities, \
        knowledge_graph_relationships, \
        knowledge_graph_query_duration, \
        http_requests_total, \
        http_request_duration, \
        http_active_requests

    # Re-create all metrics with new registry
    ingestion_throughput = Gauge(
        name="akosha_ingestion_throughput",
        documentation="Records processed per second by system and status",
        labelnames=["system_id", "status"],
        registry=registry,
    )
    ingestion_bytes_total = Counter(
        name="akosha_ingestion_bytes_total",
        documentation="Total bytes ingested by system",
        labelnames=["system_id"],
        registry=registry,
    )
    ingestion_duration_seconds = Histogram(
        name="akosha_ingestion_duration_seconds",
        documentation="Time spent ingesting records in seconds",
        labelnames=["system_id", "operation"],
        buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
        registry=registry,
    )
    search_latency = Histogram(
        name="akosha_search_latency_milliseconds",
        documentation="Search operation latency in milliseconds with percentiles",
        labelnames=["query_type", "shard_count", "tier"],
        buckets=(
            1.0,
            5.0,
            10.0,
            25.0,
            50.0,
            75.0,
            100.0,
            150.0,
            200.0,
            300.0,
            500.0,
            750.0,
            1000.0,
            2000.0,
            5000.0,
        ),
        registry=registry,
    )
    search_results_total = Counter(
        name="akosha_search_results_total",
        documentation="Total number of searches performed",
        labelnames=["query_type", "tier", "has_results"],
        registry=registry,
    )
    search_result_count = Histogram(
        name="akosha_search_result_count",
        documentation="Number of results returned per search",
        labelnames=["query_type", "tier"],
        buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000),
        registry=registry,
    )
    cache_operations = Counter(
        name="akosha_cache_operations_total",
        documentation="Cache operations (hits and misses) by tier and query type",
        labelnames=["operation", "cache_tier", "query_type"],
        registry=registry,
    )
    cache_hit_rate = Gauge(
        name="akosha_cache_hit_rate",
        documentation="Cache hit rate ratio (0-1) by cache tier and query type",
        labelnames=["cache_tier", "query_type"],
        registry=registry,
    )
    cache_size_bytes = Gauge(
        name="akosha_cache_size_bytes",
        documentation="Current cache size in bytes by tier",
        labelnames=["cache_tier"],
        registry=registry,
    )
    cache_entry_count = Gauge(
        name="akosha_cache_entry_count",
        documentation="Number of entries in cache by tier",
        labelnames=["cache_tier"],
        registry=registry,
    )
    store_size = Gauge(
        name="akosha_store_size_records",
        documentation="Number of records in each storage tier",
        labelnames=["tier"],
        registry=registry,
    )
    store_size_bytes = Gauge(
        name="akosha_store_size_bytes",
        documentation="Storage size in bytes by tier",
        labelnames=["tier"],
        registry=registry,
    )
    store_operations = Counter(
        name="akosha_store_operations_total",
        documentation="Storage operations (read/write/delete) by tier",
        labelnames=["tier", "operation", "status"],
        registry=registry,
    )
    store_operation_duration = Histogram(
        name="akosha_store_operation_duration_seconds",
        documentation="Storage operation duration in seconds",
        labelnames=["tier", "operation"],
        buckets=(
            0.0001,
            0.0005,
            0.001,
            0.005,
            0.01,
            0.025,
            0.05,
            0.1,
            0.25,
            0.5,
            1.0,
            2.5,
            5.0,
        ),
        registry=registry,
    )
    error_total = Counter(
        name="akosha_errors_total",
        documentation="Total errors by component, type, and severity",
        labelnames=["component", "error_type", "severity"],
        registry=registry,
    )
    error_last_timestamp = Gauge(
        name="akosha_error_last_timestamp_seconds",
        documentation="Unix timestamp of last error by component and type",
        labelnames=["component", "error_type"],
        registry=registry,
    )
    operations_total = Counter(
        name="akosha_operations_total",
        documentation="Total operations by type and status",
        labelnames=["operation_type", "status"],
        registry=registry,
    )
    operation_duration = Histogram(
        name="akosha_operation_duration_seconds",
        documentation="Operation duration in seconds",
        labelnames=["operation_type"],
        buckets=(
            0.001,
            0.005,
            0.01,
            0.025,
            0.05,
            0.1,
            0.25,
            0.5,
            1.0,
            2.5,
            5.0,
            10.0,
            30.0,
            60.0,
            300.0,
        ),
        registry=registry,
    )
    deduplication_checks = Counter(
        name="akosha_deduplication_checks_total",
        documentation="Deduplication checks performed",
        labelnames=["check_type", "result"],
        registry=registry,
    )
    deduplication_duration = Histogram(
        name="akosha_deduplication_duration_seconds",
        documentation="Time spent on deduplication checks",
        labelnames=["check_type"],
        buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
        registry=registry,
    )
    embedding_generation_duration = Histogram(
        name="akosha_embedding_generation_duration_seconds",
        documentation="Time spent generating embeddings",
        labelnames=["model_type"],
        buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
        registry=registry,
    )
    embedding_batch_size = Histogram(
        name="akosha_embedding_batch_size",
        documentation="Number of embeddings processed per batch",
        buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000),
        registry=registry,
    )
    vector_index_size = Gauge(
        name="akosha_vector_index_size_vectors",
        documentation="Number of vectors in the index",
        labelnames=["index_name"],
        registry=registry,
    )
    vector_index_build_duration = Histogram(
        name="akosha_vector_index_build_duration_seconds",
        documentation="Time spent building vector index",
        labelnames=["index_name"],
        buckets=(1.0, 5.0, 10.0, 30.0, 60.0, 300.0, 600.0, 1800.0),
        registry=registry,
    )
    knowledge_graph_entities = Gauge(
        name="akosha_knowledge_graph_entities_total",
        documentation="Total number of entities in knowledge graph",
        registry=registry,
    )
    knowledge_graph_relationships = Gauge(
        name="akosha_knowledge_graph_relationships_total",
        documentation="Total number of relationships in knowledge graph",
        registry=registry,
    )
    knowledge_graph_query_duration = Histogram(
        name="akosha_knowledge_graph_query_duration_seconds",
        documentation="Knowledge graph query duration",
        labelnames=["query_type"],
        buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
        registry=registry,
    )
    http_requests_total = Counter(
        name="akosha_http_requests_total",
        documentation="Total HTTP requests",
        labelnames=["method", "endpoint", "status"],
        registry=registry,
    )
    http_request_duration = Histogram(
        name="akosha_http_request_duration_seconds",
        documentation="HTTP request latency in seconds",
        labelnames=["method", "endpoint"],
        buckets=(
            0.005,
            0.01,
            0.025,
            0.05,
            0.1,
            0.25,
            0.5,
            1.0,
            2.5,
            5.0,
            10.0,
        ),
        registry=registry,
    )
    http_active_requests = Gauge(
        name="akosha_http_active_requests",
        documentation="Number of active HTTP requests",
        registry=registry,
    )


def _collect_metrics_text(registry: CollectorRegistry) -> str:
    """Generate Prometheus metrics text from the registry.

    Args:
        registry: The Prometheus metrics registry.

    Returns:
        Decoded metrics text in Prometheus exposition format.
    """
    from prometheus_client import exposition

    return exposition.generate_latest(registry).decode("utf-8")


def _is_valid_metric_line(line: str) -> bool:
    """Check if a line is a valid metric data line.

    Args:
        line: A line from the metrics text output.

    Returns:
        True if the line contains metric data (not empty or comment).
    """
    stripped = line.strip()
    return bool(stripped and not stripped.startswith("#"))


def _parse_labeled_metric(line: str) -> tuple[str, str, float] | None:
    """Parse a metric line that contains labels.

    Args:
        line: Metric line in format 'metric_name{labels} value'

    Returns:
        Tuple of (metric_name, labels, value) or None if parsing fails.
    """
    try:
        name_part, value_part = line.split("}", 1)
        metric_name = name_part.split("{", 1)[0]
        labels = name_part.split("{", 1)[1]
        value = float(value_part.strip())
        return metric_name, labels, value
    except (ValueError, IndexError):
        return None


def _parse_unlabeled_metric(line: str) -> tuple[str, float] | None:
    """Parse a metric line without labels.

    Args:
        line: Metric line in format 'metric_name value'

    Returns:
        Tuple of (metric_name, value) or None if parsing fails.
    """
    parts = line.split()
    if len(parts) >= 2:
        try:
            return parts[0], float(parts[1])
        except ValueError:
            return None
    return None


def _add_metric_to_summary(
    summary: dict[str, dict[str, float]],
    metric_name: str,
    labels: str,
    value: float,
) -> None:
    """Add a parsed metric to the summary dictionary.

    Args:
        summary: The summary dictionary to update.
        metric_name: Name of the metric.
        labels: Label string (empty string for unlabeled metrics).
        value: Numeric value of the metric.
    """
    if metric_name not in summary:
        summary[metric_name] = {}
    summary[metric_name][labels] = value


def _aggregate_metrics_from_text(metrics_text: str) -> dict[str, dict[str, float]]:
    """Parse metrics text and aggregate into a summary dictionary.

    Args:
        metrics_text: Raw Prometheus metrics text output.

    Returns:
        Dictionary mapping metric names to their labeled samples and values.
    """
    summary: dict[str, dict[str, float]] = {}

    for line in metrics_text.split("\n"):
        if not _is_valid_metric_line(line):
            continue

        if "{" in line:
            labeled_result = _parse_labeled_metric(line)
            if labeled_result:
                metric_name, labels, value = labeled_result
                _add_metric_to_summary(summary, metric_name, labels, value)
        else:
            unlabeled_result = _parse_unlabeled_metric(line)
            if unlabeled_result:
                metric_name, value = unlabeled_result
                _add_metric_to_summary(summary, metric_name, "", value)

    return summary


def get_metric_summary() -> dict[str, dict[str, float]]:
    """Get a summary of all current metric values.

    Returns:
        Dictionary mapping metric names to their labeled samples and values.

    Example:
        ```python
        summary = get_metric_summary()
        print(f"Cache hit rate: {summary['akosha_cache_hit_rate']}")
        ```
    """
    registry = get_metrics_registry()
    metrics_text = _collect_metrics_text(registry)
    return _aggregate_metrics_from_text(metrics_text)


__all__ = [
    "cache_entry_count",
    "cache_hit_rate",
    "cache_operations",
    "cache_size_bytes",
    "deduplication_checks",
    "deduplication_duration",
    "embedding_batch_size",
    # Embeddings
    "embedding_generation_duration",
    "error_last_timestamp",
    "error_total",
    # Endpoint
    "generate_metrics",
    "get_metric_summary",
    # Registry
    "get_metrics_registry",
    "http_active_requests",
    "http_request_duration",
    # HTTP/MCP
    "http_requests_total",
    # Errors
    "increment_errors",
    "ingestion_bytes_total",
    "ingestion_duration_seconds",
    "ingestion_throughput",
    # Knowledge Graph
    "knowledge_graph_entities",
    "knowledge_graph_query_duration",
    "knowledge_graph_relationships",
    # Operations
    "observe_operation",
    # Search
    "observe_search_latency",
    "observe_store_operation",
    "operation_duration",
    "operations_total",
    # Cache
    "record_cache_hit",
    "record_cache_miss",
    # Deduplication
    "record_deduplication_check",
    # Ingestion
    "record_ingestion_record",
    # Utilities
    "reset_all_metrics",
    "search_latency",
    "search_result_count",
    "search_results_total",
    "start_metrics_server",
    "store_operation_duration",
    "store_operations",
    "store_size",
    "store_size_bytes",
    "update_cache_entry_count",
    "update_cache_hit_rate",
    "update_cache_size",
    "update_ingestion_throughput",
    # Storage
    "update_store_sizes",
    "vector_index_build_duration",
    # Vector Index
    "vector_index_size",
]
