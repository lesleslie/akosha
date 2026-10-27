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
    documentation=(
        "Spans quarantined to _dlq_rows by pre_validate, labelled by the "
        "field that failed. Labelled because an unlabelled count is "
        "undiagnosable: '139 rejected' cannot be acted on, while "
        "'139 rejected, all field=service_namespace' names the defect "
        "immediately. The hot store is :memory:, so the DLQ table is not "
        "queryable from outside the process -- this label is the only way to "
        "see WHY rows were quarantined. Sum it for a total: "
        "sum(akosha_otlp_spans_rejected_total). Cardinality is bounded -- "
        "field names come from REQUIRED_FIELDS / ENUM_FIELDS, not values."
    ),
    labelnames=["field"],
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

# ── synthetic canary (§11.3) ───────────────────────────────────────────

canary_last_success_unix_nano: Gauge = Gauge(
    name="akosha_otlp_canary_last_success_unix_nano",
    documentation=(
        "Unix nanos of the last canary write that landed in the "
        "substrate. The P0-C alert measures staleness against this: a "
        "receiver that accepts spans but writes nothing leaves this "
        "frozen while /health stays green. 0 means the canary has never "
        "succeeded."
    ),
    registry=_REGISTRY,
)

canary_writes_attempted_total: Counter = Counter(
    name="akosha_otlp_canary_writes_attempted_total",
    documentation="Canary write attempts",
    registry=_REGISTRY,
)

canary_writes_succeeded_total: Counter = Counter(
    name="akosha_otlp_canary_writes_succeeded_total",
    documentation=(
        "Canary writes that inserted exactly one row with nothing "
        "deduped. Strict on purpose: a multi-pod race that dedups on "
        "the UNIQUE constraint is counted as a rejection, not a success."
    ),
    registry=_REGISTRY,
)

canary_writes_failed_total: Counter = Counter(
    name="akosha_otlp_canary_writes_failed_total",
    documentation=(
        "Canary writes that did not land. Labelled by reason: the "
        "exception class name, or `unexpected_result` when the write "
        "returned without inserting. Cardinality is bounded -- reasons "
        "are exception class names, not values."
    ),
    labelnames=["reason"],
    registry=_REGISTRY,
)

canary_writes_rejected_total: Counter = Counter(
    name="akosha_otlp_canary_writes_rejected_total",
    documentation=(
        "Canary writes the UNIQUE constraint rejected. A separate "
        "counter rather than a label on the failure counter, because a "
        "constraint rejection is not an exception: the write path "
        "worked, another pod got there first."
    ),
    registry=_REGISTRY,
)

canary_rows_deleted_total: Counter = Counter(
    name="akosha_otlp_canary_rows_deleted_total",
    documentation=(
        "Canary rows removed by the sweep. Canaries are swept at 24h, "
        "not at the substrate's retention horizon: at one row per 30 "
        "seconds the canary alone would add millions of rows to an "
        "in-memory DuckDB inside a month."
    ),
    registry=_REGISTRY,
)


__all__ = [
    "canary_last_success_unix_nano",
    "canary_rows_deleted_total",
    "canary_writes_attempted_total",
    "canary_writes_failed_total",
    "canary_writes_rejected_total",
    "canary_writes_succeeded_total",
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
