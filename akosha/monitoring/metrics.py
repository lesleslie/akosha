"""Prometheus metrics collection for Akosha.

Provides instrumentation for ingestion, query, and storage operations.
"""

import logging

from prometheus_client import Counter, Gauge, Histogram

logger = logging.getLogger(__name__)

# =============================================================================
# Ingestion Metrics
# =============================================================================

ingestion_requests_total = Counter(
    "akosha_ingestion_requests_total",
    "Total ingestion requests processed",
    ["system_id", "status"],
)

ingestion_duration_seconds = Histogram(
    "akosha_ingestion_duration_seconds",
    "Ingestion request duration in seconds",
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 300.0],
)

ingestion_queue_size = Gauge(
    "akosha_ingestion_queue_size",
    "Current number of uploads waiting to be processed",
)

ingestion_conversations_processed = Counter(
    "akosha_ingestion_conversations_processed_total",
    "Total conversations processed from uploads",
    ["system_id"],
)

ingestion_errors_total = Counter(
    "akosha_ingestion_errors_total",
    "Total ingestion errors encountered",
    ["error_type", "system_id"],
)

# =============================================================================
# Query Metrics
# =============================================================================

query_requests_total = Counter(
    "akosha_query_requests_total",
    "Total query requests processed",
    ["query_type", "status"],
)

query_duration_seconds = Histogram(
    "akosha_query_duration_seconds",
    "Query request duration in seconds",
    buckets=[0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0],
)

query_results_size = Histogram(
    "akosha_query_results_size",
    "Number of results returned from query",
    buckets=[1, 5, 10, 20, 50, 100, 200, 500],
)

query_cache_hits = Counter(
    "akosha_query_cache_hits_total",
    "Total cache hits for queries",
    ["cache_level"],  # l1, l2, miss
)

# =============================================================================
# Storage Metrics
# =============================================================================

hot_store_size_bytes = Gauge(
    "akosha_hot_store_size_bytes",
    "Current size of hot store in bytes",
)

hot_store_record_count = Gauge(
    "akosha_hot_store_record_count",
    "Current number of records in hot store",
)

warm_store_size_bytes = Gauge(
    "akosha_warm_store_size_bytes",
    "Current size of warm store in bytes",
)

warm_store_record_count = Gauge(
    "akosha_warm_store_record_count",
    "Current number of records in warm store",
)

cold_store_size_bytes = Gauge(
    "akosha_cold_store_size_bytes",
    "Total size of cold store (Parquet files) in bytes",
)

migration_records_total = Counter(
    "akosha_migration_records_total",
    "Total records migrated between tiers",
    ["source_tier", "target_tier", "status"],
)

migration_duration_seconds = Histogram(
    "akosha_migration_duration_seconds",
    "Duration of tier migration operations in seconds",
    buckets=[60.0, 300.0, 900.0, 3600.0, 7200.0],
)

# =============================================================================
# System Metrics
# =============================================================================

system_cpu_usage_percent = Gauge(
    "akosha_system_cpu_usage_percent",
    "Current CPU usage percentage",
)

system_memory_usage_bytes = Gauge(
    "akosha_system_memory_usage_bytes",
    "Current memory usage in bytes",
)

system_disk_usage_bytes = Gauge(
    "akosha_system_disk_usage_bytes",
    "Current disk usage in bytes",
    ["mount_point"],
)

# =============================================================================
# Metrics Collection Helpers
# =============================================================================


class MetricsCollector:
    """Helper class for collecting system metrics."""

    @staticmethod
    async def collect_system_metrics() -> None:
        """Collect current system metrics and update Gauges."""
        import psutil

        # CPU usage
        cpu_percent = psutil.cpu_percent(interval=1)
        system_cpu_usage_percent.set(cpu_percent)

        # Memory usage
        memory = psutil.virtual_memory()
        system_memory_usage_bytes.set(memory.used)

        # Disk usage
        disk = psutil.disk_usage("/")
        system_disk_usage_bytes.labels(mount_point="/").set(disk.used)


# =============================================================================
# Decorators for automatic instrumentation
# =============================================================================


def track_ingestion(system_id: str = "unknown"):
    """Decorator to track ingestion metrics.

    Usage:
        @track_ingestion("system-1")
        async def process_upload(upload):
            # ... processing logic
            return result
    """

    def decorator(func):
        async def wrapper(*args, **kwargs):
            status = "success"
            start_time = None

            try:
                start_time = None  # Will be set in histogram
                result = await func(*args, **kwargs)

                ingestion_requests_total.labels(
                    system_id=system_id,
                    status=status,
                ).inc()

                return result

            except Exception as e:
                status = "error"
                ingestion_errors_total.labels(
                    error_type=type(e).__name__,
                    system_id=system_id,
                ).inc()
                raise

            finally:
                if start_time is not None:
                    # Track duration if we started tracking
                    pass  # Histogram handles timing automatically

        return wrapper

    return decorator


def track_query(query_type: str = "search"):
    """Decorator to track query metrics.

    Usage:
        @track_query("semantic_search")
        async def search_similar(query_embedding):
            # ... search logic
            return results
    """

    def decorator(func):
        async def wrapper(*args, **kwargs):
            status = "success"

            try:
                with query_duration_seconds.time():
                    result = await func(*args, **kwargs)

                query_requests_total.labels(
                    query_type=query_type,
                    status=status,
                ).inc()

                # Track result size
                if isinstance(result, list):
                    query_results_size.observe(len(result))

                return result

            except Exception:
                status = "error"
                raise

            finally:
                pass

        return wrapper

    return decorator


def track_migration(source_tier: str, target_tier: str):
    """Decorator to track tier migration metrics.

    Usage:
        @track_migration("hot", "warm")
        async def migrate_hot_to_warm(records):
            # ... migration logic
            return stats
    """

    def decorator(func):
        async def wrapper(*args, **kwargs):
            status = "success"

            try:
                with migration_duration_seconds.time():
                    result = await func(*args, **kwargs)

                migration_records_total.labels(
                    source_tier=source_tier,
                    target_tier=target_tier,
                    status=status,
                ).inc(amount=getattr(result, "records_migrated", 0))

                return result

            except Exception:
                status = "error"
                migration_records_total.labels(
                    source_tier=source_tier,
                    target_tier=target_tier,
                    status=status,
                ).inc()
                raise

        return wrapper

    return decorator


# =============================================================================
# FastAPI integration
# =============================================================================

from fastapi import FastAPI, Request
from starlette.responses import Response


def setup_metrics_endpoint(app: FastAPI) -> None:
    """Setup /metrics endpoint for Prometheus scraping.

    Usage:
        app = FastAPI()
        setup_metrics_endpoint(app)
    """

    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    @app.get("/metrics")
    async def metrics():
        """Expose Prometheus metrics."""
        return Response(
            content=generate_latest(),
            media_type=CONTENT_TYPE_LATEST,
        )


def setup_middleware(app: FastAPI) -> None:
    """Setup middleware for automatic request tracking.

    Usage:
        app = FastAPI()
        setup_middleware(app)
    """

    @app.middleware("http")
    async def track_requests(request: Request, call_next):
        """Track all HTTP requests."""
        # Record start time

        # Process request
        response = await call_next(request)

        # Track metrics based on endpoint
        endpoint = request.url.path

        # Update metrics (example - customize as needed)
        if endpoint.startswith("/ingest/"):
            ingestion_requests_total.labels(
                system_id="unknown",
                status="success" if response.status_code < 400 else "error",
            ).inc()

        elif endpoint.startswith("/query/"):
            query_requests_total.labels(
                query_type="search",
                status="success" if response.status_code < 400 else "error",
            ).inc()

        return response


# =============================================================================
# Convenience functions for manual metric updates
# =============================================================================


def record_ingestion_event(system_id: str, status: str, duration: float = 0.0):
    """Record ingestion event manually."""
    ingestion_requests_total.labels(system_id=system_id, status=status).inc()
    if duration > 0:
        ingestion_duration_seconds.observe(duration)


def record_query_event(query_type: str, status: str, duration: float = 0.0, result_count: int = 0):
    """Record query event manually."""
    query_requests_total.labels(query_type=query_type, status=status).inc()
    if duration > 0:
        query_duration_seconds.observe(duration)
    if result_count > 0:
        query_results_size.observe(result_count)


def update_storage_metrics(
    hot_size_bytes: int,
    hot_record_count: int,
    warm_size_bytes: int,
    warm_record_count: int,
) -> None:
    """Update storage metrics manually."""
    hot_store_size_bytes.set(hot_size_bytes)
    hot_store_record_count.set(hot_record_count)
    warm_store_size_bytes.set(warm_size_bytes)
    warm_store_record_count.set(warm_record_count)


def record_cache_hit(cache_level: str) -> None:
    """Record cache hit."""
    query_cache_hits.labels(cache_level=cache_level).inc()
