"""Prometheus metrics for the OTLP receiver (spec v7 §11.3, issue #11).

Bound to akosha's shared ``CollectorRegistry``, so they are exported by the
``/metrics`` route already live on the MCP port (:8682,
``akosha/mcp/server.py:1636``) — not on a new port.

``OTLPReceiver.PROMETHEUS_PORT = 9464`` is dead code. It was never bound, and
9464 is Grafana Alloy's ``otelcol.exporter.prometheus`` default endpoint, so
binding it would collide the moment that exporter is given an ``endpoint``.
The receiver has no business owning a second port when a metrics route is
already served for it.

The receiver keeps its own plain ``int`` counters as the in-process source of
truth (tests and the heartbeat watcher read those); these mirror them into the
registry. Both move at the same call sites so they cannot drift.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

from akosha.observability.prometheus_metrics import get_metrics_registry

_REGISTRY = get_metrics_registry()

# ── ingest counters ─────────────────────────────────────────────────────

spans_received_total: Counter = Counter(
    name="akosha_otlp_spans_received_total",
    documentation="Spans accepted off the wire, before validation",
    registry=_REGISTRY,
)

spans_inserted_total: Counter = Counter(
    name="akosha_otlp_spans_inserted_total",
    documentation="Span rows written to spans_substrate",
    registry=_REGISTRY,
)

spans_deduped_total: Counter = Counter(
    name="akosha_otlp_spans_deduped_total",
    documentation="Spans skipped because span_id was already present",
    registry=_REGISTRY,
)

spans_rejected_total: Counter = Counter(
    name="akosha_otlp_spans_rejected_total",
    documentation="Spans quarantined to _dlq_rows by pre_validate",
    registry=_REGISTRY,
)

dlq_inserts_total: Counter = Counter(
    name="akosha_otlp_dlq_inserts_total",
    documentation="Rows successfully written to _dlq_rows",
    registry=_REGISTRY,
)

dlq_insert_failed_total: Counter = Counter(
    name="akosha_otlp_dlq_insert_failed_total",
    documentation=(
        "DLQ writes that raised. Non-zero means rejected spans were dropped "
        "on the floor — the batch still committed, but the evidence is gone."
    ),
    registry=_REGISTRY,
)

http_body_undecodable_total: Counter = Counter(
    name="akosha_otlp_http_body_undecodable_total",
    documentation=(
        "HTTP requests whose body failed protobuf decode. A non-zero rate "
        "means something is POSTing JSON to /v1/traces, not OTLP protobuf."
    ),
    registry=_REGISTRY,
)

# ── retention sweep (§8.1 / §8.2) ───────────────────────────────────────

sweep_last_completed_unix_nano: Gauge = Gauge(
    name="akosha_otlp_sweep_last_completed_unix_nano",
    documentation=(
        "Unix nanos of the last sweep that completed cleanly. 0 means the "
        "most recent cycle did NOT complete — pair with "
        "sweep_first_started_unix_nano to tell 'frozen' from 'never ran'."
    ),
    registry=_REGISTRY,
)

sweep_first_started_unix_nano: Gauge = Gauge(
    name="akosha_otlp_sweep_first_started_unix_nano",
    documentation=(
        "Unix nanos of this pod's first sweep. Set once, never reset. "
        "0 means the sweep has never run, which is the state the "
        "sweep_retention_late alert is gated on so it cannot page on it."
    ),
    registry=_REGISTRY,
)

sweep_duration_seconds: Histogram = Histogram(
    name="akosha_otlp_sweep_duration_seconds",
    documentation="Wall-clock duration of one retention sweep",
    buckets=(0.01, 0.1, 0.5, 1.0, 5.0, 15.0, 60.0, 300.0),
    registry=_REGISTRY,
)

retention_sweep_failed_total: Counter = Counter(
    name="akosha_otlp_retention_sweep_failed_total",
    documentation="Retention sweep iterations that raised",
    labelnames=["table"],
    registry=_REGISTRY,
)

dlq_rows_deleted_total: Counter = Counter(
    name="akosha_otlp_dlq_rows_deleted_total",
    documentation="Rows removed from _dlq_rows by the §8.1 sweep",
    registry=_REGISTRY,
)


__all__ = [
    "dlq_insert_failed_total",
    "dlq_inserts_total",
    "dlq_rows_deleted_total",
    "http_body_undecodable_total",
    "retention_sweep_failed_total",
    "spans_deduped_total",
    "spans_inserted_total",
    "spans_received_total",
    "spans_rejected_total",
    "sweep_duration_seconds",
    "sweep_first_started_unix_nano",
    "sweep_last_completed_unix_nano",
]