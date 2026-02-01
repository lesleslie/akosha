# Akosha Phase 3: Production Hardening

**Status**: Planned
**Duration**: Weeks 9-10 (2 weeks)
**Focus**: Reliability, observability, performance optimization

______________________________________________________________________

## Overview

Phase 2 added advanced features. Phase 3 hardens the system for production deployment with:

1. **Resilience Patterns** (circuit breakers, retries, graceful degradation)
1. **Observability** (OpenTelemetry tracing, Prometheus metrics, structured logging)
1. **Performance Optimization** (profiling, caching strategies, query optimization)
1. **Deployment** (Kubernetes manifests, health checks, rolling updates)

______________________________________________________________________

## Week 9: Resilience & Observability

### Task 9.1: Circuit Breakers

**File**: `akosha/utils/resilience.py`

```python
"""Circuit breakers and retry logic with tenacity."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

import tenacity

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """Circuit breaker for external service calls.

    Prevents cascading failures by failing fast when a service
    is experiencing issues.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        expected_exception: type[Exception] = Exception,
    ):
        """Initialize circuit breaker.

        Args:
            failure_threshold: Failures before opening circuit
            recovery_timeout: Seconds before attempting recovery
            expected_exception: Exception that indicates failure
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception
        self._failures = 0
        self._last_failure_time = None
        self._state = "closed"  # closed, open, half-open

    async def call(self, func: Callable, *args, **kwargs) -> Any:
        """Execute function with circuit breaker protection.

        Args:
            func: Function to call
            *args: Function args
            **kwargs: Function kwargs

        Returns:
            Function result

        Raises:
            CircuitBreakerOpen: When circuit is open
        """
        if self._state == "open":
            if self._should_attempt_reset():
                self._state = "half-open"
                logger.info("Circuit breaker: half-open (testing recovery)")
            else:
                raise CircuitBreakerOpen("Circuit breaker is open")

        try:
            result = await func(*args, **kwargs)
            self._on_success()
            return result
        except self.expected_exception as e:
            self._on_failure()
            raise e

    def _on_success(self) -> None:
        """Handle successful call."""
        self._failures = 0
        if self._state == "half-open":
            self._state = "closed"
            logger.info("Circuit breaker: closed (recovery successful)")

    def _on_failure(self) -> None:
        """Handle failed call."""
        self._failures += 1
        self._last_failure_time = datetime.now()

        if self._failures >= self.failure_threshold:
            self._state = "open"
            logger.warning(
                f"Circuit breaker: open (threshold {self.failure_threshold} reached)"
            )

    def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to attempt reset."""
        return (
            self._last_failure_time is not None
            and (datetime.now() - self._last_failure_time).total_seconds()
            >= self.recovery_timeout
        )


class CircuitBreakerOpen(Exception):
    """Raised when circuit breaker is open."""

    pass


def retry_with_exponential_backoff(
    max_attempts: int = 3,
    wait_min: float = 1.0,
    wait_max: float = 10.0,
):
    """Decorator for retry with exponential backoff.

    Args:
        max_attempts: Maximum retry attempts
        wait_min: Minimum wait time (seconds)
        wait_max: Maximum wait time (seconds)
    """
    return tenacity.retry(
        stop=tenacity.stop_after_attempt(max_attempts),
        wait=tenacity.wait_exponential(multiplier=1, min=wait_min, max=wait_max),
        reraise=True,
    )
```

### Task 9.2: OpenTelemetry Tracing

**File**: `akosha/monitoring/tracing.py`

```python
"""OpenTelemetry distributed tracing."""

from __future__ = annotations

import logging
from contextlib import asynccontextmanager

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

logger = logging.getLogger(__name__)


class TracingConfig:
    """OpenTelemetry tracing configuration."""

    def __init__(self) -> None:
        """Initialize tracing."""
        self._tracer = None
        self._configured = False

    def configure(self, service_name: str, otlp_endpoint: str | None = None) -> None:
        """Configure OpenTelemetry.

        Args:
            service_name: Service name for traces
            otlp_endpoint: OTLP collector endpoint
        """
        provider = TracerProvider()

        if otlp_endpoint:
            otlp_exporter = OTLPSpanExporter(
                endpoint=otlp_endpoint,
                insecure=True,
            )
            provider.add_span_processor(BatchSpanProcessor(otlp_exporter))

        trace.set_tracer_provider(provider)
        self._tracer = trace.get_tracer(__name__)
        self._configured = True

        logger.info(f"Tracing configured: {service_name}")

    def get_tracer(self):
        """Get tracer instance."""
        return self._tracer


@asynccontextmanager
async def trace_operation(
    operation_name: str,
    tracer,
    **attributes,
):
    """Context manager for tracing an operation.

    Args:
        operation_name: Operation name
        tracer: Tracer instance
        **attributes: Span attributes

    Yields:
            Span object
    """
    with tracer.start_as_current_span(
        operation_name,
        attributes=attributes,
    ) as span:
        try:
            yield span
            span.set_status("ok")
        except Exception as e:
            span.record_exception(e)
            span.set_status(f"error: {type(e).__name__}")
            raise
```

### Task 9.3: Prometheus Metrics

**File**: `akosha/monitoring/metrics.py`

```python
"""Prometheus metrics for Akosha."""

from prometheus_client import Counter, Gauge, Histogram

# Ingestion metrics
ingestion_total = Counter(
    "akosha_ingestion_total",
    "Total conversations ingested",
    ["system_id", "status"],
)

ingestion_duration_seconds = Histogram(
    "akosha_ingestion_duration_seconds",
    "Ingestion processing time",
    ["system_id"],
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0],
)

# Search metrics
search_total = Counter(
    "akosha_search_total",
    "Total searches performed",
    ["system_id", "tier"],
)

search_duration_seconds = Histogram(
    "akosha_search_duration_seconds",
    "Search query time",
    ["tier"],
    buckets=[0.01, 0.05, 0.1, 0.5, 1.0, 2.0],
)

# Storage metrics
storage_size_bytes = Gauge(
    "akosha_storage_size_bytes",
    "Storage tier size in bytes",
    ["tier"],
)

storage_conversation_count = Gauge(
    "akosha_storage_conversation_count",
    "Number of conversations in storage tier",
    ["tier", "system_id"],
)

# Graph metrics
graph_entities_total = Gauge(
    "akosha_graph_entities_total",
    "Total entities in knowledge graph",
)

graph_edges_total = Gauge(
    "akosha_graph_edges_total",
    "Total edges in knowledge graph",
)


def record_ingestion(system_id: str, duration_seconds: float, status: str) -> None:
    """Record ingestion metrics.

    Args:
        system_id: System identifier
        duration_seconds: Processing time
        status: "success" or "failure"
    """
    ingestion_total.labels(system_id=system_id, status=status).inc()
    ingestion_duration_seconds.labels(system_id=system_id).observe(duration_seconds)


def record_search(system_id: str, tier: str, duration_seconds: float) -> None:
    """Record search metrics.

    Args:
        system_id: System identifier
        tier: Storage tier
        duration_seconds: Query time
    """
    search_total.labels(system_id=system_id, tier=tier).inc()
    search_duration_seconds.labels(tier=tier).observe(duration_seconds)
```

______________________________________________________________________

## Week 10: Kubernetes Deployment

### Task 10.1: Kubernetes Manifests

**File**: `k8s/deployment.yaml`

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: akosha-config
  namespace: akosha
data:
  AKOSHA__STORAGE__HOT__BACKEND: "duckdb-memory"
  AKOSHA__STORAGE__WARM__BACKEND: "duckdb-ssd"
  AKOSHA__STORAGE__COLD__BACKEND: "s3"
  AKOSHA__STORAGE__COLD__BUCKET: "akosha-prod"
  AKOSHA__INGESTION__WORKERS: "3"
  AKOSHA__LOG__LEVEL: "INFO"

---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: akosha-ingestion
  namespace: akosha
spec:
  replicas: 3
  selector:
    matchLabels:
      app: akosha-ingestion
  template:
    metadata:
      labels:
        app: akosha-ingestion
        version: v0.1.0
    spec:
      containers:
      - name: akosha-ingestion
        image: akosha:v0.1.0
        ports:
        - containerPort: 8000
          name: http
        env:
        - name: AKOSHA__LOG_LEVEL
          valueFrom:
            configMapKeyRef:
              name: akosha-config
              key: AKOSHA__LOG__LEVEL
        resources:
          requests:
            memory: "256Mi"
            cpu: "250m"
          limits:
            memory: "2Gi"
            cpu: "1000m"
        livenessProbe:
          httpGet:
            path: /health
            port: http
          initialDelaySeconds: 30
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /health
            port: http
          initialDelaySeconds: 10
          periodSeconds: 5

---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: akosha-ingestion-hpa
  namespace: akosha
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: akosha-ingestion
  minReplicas: 3
  maxReplicas: 50
  metrics:
  - type: Pods
    pods:
      metric:
        name: redis_queue_length
        target:
          type: AverageValue
          averageValue: "1000"
  - type: Resource
    resource:
      cpu:
        target:
          type: Utilization
          averageUtilization: 70

---
apiVersion: v1
kind: Service
metadata:
  name: akosha-ingestion
  namespace: akosha
spec:
  selector:
    app: akosha-ingestion
  ports:
  - port: 8000
    targetPort: http
    name: http
  type: ClusterIP

---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: akosha-ingestion
  namespace: akosha
  annotations:
    cert-manager.io/cluster-issuer: "letsencrypt-prod"
spec:
  rules:
  - host: akosha.example.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: akosha-ingestion
            port:
              number: 8000
  tls:
  - hosts:
    - akosha.example.com
    secretName: akosha-tls
```

### Task 10.2: Performance Testing

**File**: `tests/performance/test_load.py`

```python
"""Load testing for Akosha."""

import asyncio
import time
from datetime import UTC, datetime

from locust import HttpUser, task, between


class AkoshaUser(HttpUser):
    """Simulated Akosha user."""

    wait_time = between(1, 3)

    @task
    def search_all_systems(self):
        """Search across all systems."""
        self.client.post("/api/v1/search", json={
            "query": "authentication implementation",
            "limit": 10,
        })

    @task(3)
    def get_metrics(self):
        """Get system metrics."""
        self.client.get("/api/v1/analytics/metrics")


if __name__ == "__main__":
    # Run load test
    # locust -f tests/performance/test_load.py --host=http://localhost:8000
    pass
```

______________________________________________________________________

## Implementation Checklist

### Week 9

- [ ] Circuit breakers for all external calls
- [ ] Retry logic with exponential backoff
- [ ] OpenTelemetry tracing setup
- [ ] Prometheus metrics endpoints
- [ ] Structured logging with context

### Week 10

- [ ] Kubernetes deployment manifests
- [ ] HPA configuration
- [ ] Health check endpoints
- [ ] Load testing with Locust
- [ ] Performance profiling and optimization
- [ ] Rollout procedures

______________________________________________________________________

## Success Criteria

Phase 3 is complete when:

- [ ] Circuit breakers prevent cascading failures
- [ ] All operations have distributed tracing
- [ ] Prometheus metrics are exposed
- [ ] System can handle 1000 req/min
- [ ] P99 latency < 500ms for searches
- [ ] Zero-downtime deployments tested
- [ ] Runbook documentation complete

______________________________________________________________________

## Next: Phase 4 (Scale Preparation)

Phase 4 adds:

- Milvus cluster for 100M-1B embeddings
- TimescaleDB for advanced time-series
- Neo4j for complex graph queries
- Multi-region disaster recovery

See: `docs/PHASE_4_SCALE_PREPARATION.md`
