"""OpenTelemetry distributed tracing setup for Akasha.

This module provides:
- Automatic span creation for all operations
- Context propagation across services
- Metrics collection and export
- Integration with Prometheus and OTLP
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Callable

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.instrumentation.asyncio import AsyncioInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)

# Global tracer and meter
_tracer: trace.Tracer | None = None
_meter: metrics.Meter | None = None


def setup_telemetry(
    service_name: str = "akasha",
    environment: str = "development",
    otlp_endpoint: str | None = None,
    enable_console_export: bool = False,
    sample_rate: float = 1.0,
) -> tuple[trace.Tracer, metrics.Meter]:
    """Setup OpenTelemetry tracing and metrics.

    Args:
        service_name: Name of the service
        environment: Environment (development, production)
        otlp_endpoint: OTLP collector endpoint (e.g., http://localhost:4317)
        enable_console_export: Export spans to console for debugging
        sample_rate: Sampling rate (0.0 to 1.0, 1.0 = all traces)

    Returns:
        Tuple of (tracer, meter)
    """
    global _tracer, _meter

    # Create resource with service information
    resource = Resource.create({
        "service.name": service_name,
        "service.namespace": "akasha",
        "deployment.environment": environment,
    })

    # Setup tracing
    tracer_provider = TracerProvider(resource=resource)

    # Add OTLP exporter if endpoint provided
    if otlp_endpoint:
        otlp_exporter = OTLPSpanExporter(
            endpoint=otlp_endpoint,
            insecure=True,
        )
        tracer_provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
        logger.info(f"✅ OTLP tracing enabled: {otlp_endpoint}")

    # Add console exporter for debugging
    if enable_console_export:
        console_exporter = ConsoleSpanExporter()
        tracer_provider.add_span_processor(BatchSpanProcessor(console_exporter))
        logger.info("✅ Console span export enabled")

    # Set sampling
    sampler = TraceIdRatioBased(sample_rate)
    tracer_provider = TracerProvider(resource=resource, sampler=sampler)

    # Register tracer
    trace.set_tracer_provider(tracer_provider)
    _tracer = trace.get_tracer(__name__)

    # Setup metrics
    metric_reader = PrometheusMetricReader()
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)
    _meter = metrics.get_meter(__name__)

    # Instrument asyncio
    asyncio_instrumentor = AsyncioInstrumentor()
    asyncio_instrumentor.instrument()

    logger.info(f"✅ Telemetry initialized: {service_name} ({environment})")
    logger.info(f"   Sampling rate: {sample_rate:.0%}")

    return _tracer, _meter


def get_tracer() -> trace.Tracer:
    """Get global tracer instance.

    Returns:
        Tracer instance

    Raises:
        RuntimeError: If telemetry not initialized
    """
    if _tracer is None:
        raise RuntimeError("Telemetry not initialized. Call setup_telemetry() first.")
    return _tracer


def get_meter() -> metrics.Meter:
    """Get global meter instance.

    Returns:
        Meter instance

    Raises:
        RuntimeError: If telemetry not initialized
    """
    if _meter is None:
        raise RuntimeError("Telemetry not initialized. Call setup_telemetry() first.")
    return _meter


@contextmanager
def trace_operation(
    operation_name: str,
    attributes: dict[str, str] | None = None,
) -> Any:
    """Context manager for tracing an operation.

    Args:
        operation_name: Name of the operation
        attributes: Additional span attributes

    Yields:
        Span object

    Example:
        with trace_operation("generate_embedding", {"text_length": str(len(text))}):
            embedding = await generate_embedding(text)
    """
    tracer_instance = get_tracer()

    with tracer_instance.start_as_current_span(
        operation_name,
        attributes=attributes or {},
    ) as span:
        try:
            yield span
            span.set_status(trace.StatusCode.OK)
        except Exception as e:
            span.record_exception(e)
            span.set_status(trace.StatusCode.ERROR, str(e))
            raise


def add_span_attributes(attributes: dict[str, str | int | float]) -> None:
    """Add attributes to the current span.

    Args:
        attributes: Dictionary of attributes to add
    """
    current_span = trace.get_current_span()
    if current_span and current_span.is_recording():
        current_span.set_attributes(attributes)


def add_span_event(name: str, attributes: dict[str, str] | None = None) -> None:
    """Add an event to the current span.

    Args:
        name: Event name
        attributes: Event attributes
    """
    current_span = trace.get_current_span()
    if current_span and current_span.is_recording():
        current_span.add_event(name, attributes or {})


def record_counter(
    name: str,
    value: int = 1,
    attributes: dict[str, str] | None = None,
) -> None:
    """Record a counter metric.

    Args:
        name: Metric name
        value: Counter increment
        attributes: Metric attributes
    """
    meter = get_meter()
    counter = meter.create_counter(
        name,
        description=f"Counter for {name}",
    )
    counter.add(value, attributes or {})


def record_histogram(
    name: str,
    value: float,
    attributes: dict[str, str] | None = None,
) -> None:
    """Record a histogram metric (distribution).

    Args:
        name: Metric name
        value: Histogram value
        attributes: Metric attributes
    """
    meter = get_meter()
    histogram = meter.create_histogram(
        name,
        description=f"Histogram for {name}",
    )
    histogram.record(value, attributes or {})


def record_gauge(
    name: str,
    value: float,
    attributes: dict[str, str] | None = None,
) -> None:
    """Record a gauge metric.

    Args:
        name: Metric name
        value: Gauge value
        attributes: Metric attributes
    """
    meter = get_meter()
    gauge = meter.create_gauge(
        name,
        description=f"Gauge for {name}",
    )
    gauge.set(value, attributes or {})


# Decorator for automatic function tracing
def traced(
    operation_name: str | None = None,
    attributes: dict[str, str] | None = None,
) -> Callable:
    """Decorator to automatically trace a function.

    Args:
        operation_name: Name of the operation (defaults to function name)
        attributes: Additional span attributes

    Returns:
        Decorated function

    Example:
        @traced("generate_embedding")
        async def generate_embedding(text: str) -> np.ndarray:
            return model.encode(text)
    """

    def decorator(func: Callable) -> Callable:
        nonlocal operation_name

        if operation_name is None:
            operation_name = f"{func.__module__}.{func.__name__}"

        import functools

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any):
            tracer_instance = get_tracer()
            with tracer_instance.start_as_current_span(
                operation_name,
                attributes=attributes or {},
            ) as span:
                # Add function arguments as attributes (sanitized)
                span.set_attribute("function.name", func.__name__)
                span.set_attribute("function.module", func.__module__)

                try:
                    result = await func(*args, **kwargs)
                    span.set_status(trace.StatusCode.OK)
                    return result
                except Exception as e:
                    span.record_exception(e)
                    span.set_status(trace.StatusCode.ERROR, str(e))
                    raise

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any):
            tracer_instance = get_tracer()
            with tracer_instance.start_as_current_span(
                operation_name,
                attributes=attributes or {},
            ) as span:
                span.set_attribute("function.name", func.__name__)
                span.set_attribute("function.module", func.__module__)

                try:
                    result = func(*args, **kwargs)
                    span.set_status(trace.StatusCode.OK)
                    return result
                except Exception as e:
                    span.record_exception(e)
                    span.set_status(trace.StatusCode.ERROR, str(e))
                    raise

        # Return appropriate wrapper based on function type
        import asyncio
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator


async def shutdown_telemetry() -> None:
    """Shutdown telemetry providers gracefully.

    This should be called when shutting down the application.
    """
    global _tracer, _meter

    logger.info("Shutting down telemetry...")

    # Shutdown trace provider
    trace_provider = trace.get_tracer_provider()
    if trace_provider:
        await trace_provider.shutdown()

    # Shutdown meter provider
    meter_provider = metrics.get_meter_provider()
    if meter_provider:
        await meter_provider.shutdown()

    logger.info("✅ Telemetry shutdown complete")
