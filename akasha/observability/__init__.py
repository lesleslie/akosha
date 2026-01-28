"""Observability module for OpenTelemetry tracing and metrics."""

from akasha.observability.tracing import (
    add_span_attributes,
    add_span_event,
    get_meter,
    get_tracer,
    record_counter,
    record_gauge,
    record_histogram,
    setup_telemetry,
    shutdown_telemetry,
    traced,
    trace_operation,
)

__all__ = [
    "setup_telemetry",
    "get_tracer",
    "get_meter",
    "trace_operation",
    "traced",
    "add_span_attributes",
    "add_span_event",
    "record_counter",
    "record_histogram",
    "record_gauge",
    "shutdown_telemetry",
]
