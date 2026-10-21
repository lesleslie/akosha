"""The ``spans_substrate`` DDL, shared by every test that needs the table.

Verbatim from spec §5.1 of ``docs/specs/2026-10-20-bodai-otel-substrate-read-path.md``
(the read-path spec) — which itself mirrors the v7 substrate spec.

There is exactly ONE writer of this table: the OTLP receiver, via
``_ensure_operational_tables`` (v7 plan, PR-A2), which is not built yet. Until
it lands, every test that reads or writes spans has to create the table itself,
and a copy that drifts from the receiver produces a read path that silently
returns nothing. So the DDL lives here once and both the R-1 and R-2 test
modules import it from here.

When the receiver lands, there will be two copies — this one and the
receiver's — and they must be updated together. Better two than five.
"""

from __future__ import annotations

SPANS_SUBSTRATE_DDL = """
CREATE TABLE IF NOT EXISTS spans_substrate (
    span_id                   TEXT PRIMARY KEY,
    trace_id                  TEXT,
    parent_span_id            TEXT,
    service_name              TEXT         NOT NULL,
    service_namespace         TEXT         NOT NULL,
    deployment_environment    TEXT,
    telemetry_source          TEXT,
    telemetry_source_type     TEXT,
    span_name                 TEXT         NOT NULL,
    start_time_unix_nano      BIGINT       NOT NULL,
    end_time_unix_nano        BIGINT,
    received_at_unix_nano     BIGINT       NOT NULL,
    duration_ms               REAL,
    outcome                   TEXT,
    selector                  TEXT,
    task_class                TEXT,
    latency_ms                REAL,
    gen_ai_provider_name      TEXT,
    gen_ai_request_model      TEXT,
    gen_ai_response_model     TEXT,
    gen_ai_usage_input_tokens INTEGER,
    gen_ai_usage_output_tokens INTEGER,
    gen_ai_usage_cost         NUMERIC(10, 6),
    status_code               TEXT,
    status_message            TEXT,
    attributes                JSON         NOT NULL,
    events                    JSON,
    resource                  JSON         NOT NULL,
    has_gen_ai_attrs          BOOLEAN      NOT NULL DEFAULT FALSE,
    has_dispatch_attrs        BOOLEAN      NOT NULL DEFAULT FALSE,
    inserted_at               TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at                TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_span_identity UNIQUE (trace_id, span_id)
);
"""

__all__ = ["SPANS_SUBSTRATE_DDL"]
