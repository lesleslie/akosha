"""Observability module for OpenTelemetry tracing and metrics."""

from akosha.observability.tracing import (
    add_span_attributes,
    add_span_event,
    get_meter,
    get_tracer,
    record_counter,
    record_gauge,
    record_histogram,
    setup_telemetry,
    shutdown_telemetry,
    trace_operation,
    traced,
)

__all__ = [
    "add_span_attributes",
    "add_span_event",
    "get_meter",
    "get_tracer",
    "record_counter",
    "record_gauge",
    "record_histogram",
    "setup_telemetry",
    "shutdown_telemetry",
    "trace_operation",
    "traced",
]
