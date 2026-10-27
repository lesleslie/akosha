"""The synthetic canary: a span the receiver writes to prove it still writes.

§11.3. The heartbeat watcher proves the substrate is **queryable** (P0-A /
P0-B). It does not prove the receiver **writes**. A broken write path — a
serializer that raises, a DLQ that swallows the error, a column that no
longer matches the INSERT — lets ``/health`` stay green indefinitely while
every producer's spans silently drop. That is the failure this exists for:
no "observability of the observability".

So the receiver writes a marker span into its own substrate every 30
seconds, through the **same path a producer's span takes**: protobuf
decode → ``_extract_spans`` → ``_proto_to_dict`` → ``pre_validate`` →
``insert_spans_substrate``. Not a shortcut into the INSERT. A shortcut
would miss exactly the class of bug this is here to catch — the
serializer is where the ``ArrayValue`` defect lived, and a canary that
skipped it would have stayed green through that entire outage.

The success bar is deliberately strict. A write counts as **succeeded**
only when exactly one row was inserted and nothing was deduped. A
multi-pod deployment racing the same cycle dedups on the
``(trace_id, span_id)`` UNIQUE constraint, and that is counted as a
**rejection** — not a success, and not a failure. Collapsing the three is
how a canary ends up reporting a healthy fleet as broken, or a broken
fleet as healthy.

The canary's namespace is ``bodai``, the fleet default, because the
receiver's own resource validator would otherwise reject the marker
before it reached the INSERT — and a canary that fails on a namespace
the fleet legitimately uses would be a false alarm. The marker is
identifiable by its ``service_name`` (``_receiver_canary``), not by its
namespace.

**Canary rows are swept at 24h, not at the substrate's 30 days.** At one
row per 30 seconds that is 86,400 rows a day; on the substrate's normal
horizon they would accumulate into millions of rows inside an IN-MEMORY
DuckDB with a 1GB limit. A canary row is liveness evidence, not history
— the same reasoning §8.1 applies to ``_dlq_rows``.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from akosha.observability import otlp_metrics

if TYPE_CHECKING:
    from akosha.observability.otlp_receiver import OTLPReceiver

logger = logging.getLogger(__name__)

# ── the marker's identity ──────────────────────────────────────────────

CANARY_SERVICE_NAME = "_receiver_canary"
CANARY_SPAN_NAME = "_canary_substrate_write"
CANARY_NAMESPACE = "bodai"
CANARY_EMIT_INTERVAL_SECONDS = 30
CANARY_VERSION = "1"

# Constant across every cycle, so all the canary rows share one trace and
# an operator can pull the whole series with a single trace_id filter.
CANARY_TRACE_ID = hashlib.blake2b(b"_canary", digest_size=16).digest()


def canary_span_id(unix_nano: int) -> bytes:
    """A per-cycle span id, derived from the timestamp.

    Derived rather than random so the canary is reproducible: the same
    timestamp always produces the same id, which is what makes the
    multi-pod dedup path reachable in a test instead of being a race
    nobody can reproduce.
    """
    return hashlib.blake2b(f"_canary_{unix_nano}".encode(), digest_size=8).digest()


@dataclass
class CanaryState:
    """The canary's own liveness state.

    ``last_canary_success_unix_nano`` is the RECEIVER's clock at the
    moment the write succeeded — not the producer's, and not the span's
    start time. The P0-C alert measures staleness against it, so it has
    to be the time the receiver knows the write landed.
    """

    last_canary_success_unix_nano: int | None = None
    last_canary_failure_unix_nano: int | None = None
    canary_writes_attempted_total: int = 0
    canary_writes_succeeded_total: int = 0
    canary_writes_failed_total: int = 0
    canary_writes_rejected_total: int = 0


def build_canary_request(unix_nano: int) -> Any:
    """Build the canary's OTLP request.

    A real ``ExportTraceServiceRequest``, not a dict, so the marker goes
    through the same protobuf decode the gRPC and HTTP paths use.
    """
    from opentelemetry.proto.collector.trace.v1 import (  # type: ignore[import-not-found]
        trace_service_pb2,
    )
    from opentelemetry.proto.common.v1 import common_pb2  # type: ignore[import-not-found]
    from opentelemetry.proto.resource.v1 import resource_pb2  # type: ignore[import-not-found]
    from opentelemetry.proto.trace.v1 import trace_pb2  # type: ignore[import-not-found]

    return trace_service_pb2.ExportTraceServiceRequest(
        resource_spans=[
            trace_pb2.ResourceSpans(
                resource=resource_pb2.Resource(
                    attributes=[
                        common_pb2.KeyValue(
                            key="service.name",
                            value=common_pb2.AnyValue(string_value=CANARY_SERVICE_NAME),
                        ),
                        common_pb2.KeyValue(
                            key="service.namespace",
                            value=common_pb2.AnyValue(string_value=CANARY_NAMESPACE),
                        ),
                    ]
                ),
                scope_spans=[
                    trace_pb2.ScopeSpans(
                        spans=[
                            trace_pb2.Span(
                                trace_id=CANARY_TRACE_ID,
                                span_id=canary_span_id(unix_nano),
                                name=CANARY_SPAN_NAME,
                                start_time_unix_nano=unix_nano,
                                end_time_unix_nano=unix_nano,
                                status=trace_pb2.Status(
                                    code=trace_pb2.Status.STATUS_CODE_OK
                                ),
                                attributes=[
                                    common_pb2.KeyValue(
                                        key="_canary",
                                        value=common_pb2.AnyValue(bool_value=True),
                                    ),
                                    common_pb2.KeyValue(
                                        key="_canary_version",
                                        value=common_pb2.AnyValue(
                                            string_value=CANARY_VERSION
                                        ),
                                    ),
                                ],
                            )
                        ]
                    )
                ],
            )
        ]
    )


class CanaryEmitter:
    """Writes the canary marker and keeps the liveness state.

    The Prometheus gauges are updated HERE, at the moment the outcome is
    known, rather than by a separate bridge task copying the state. The
    bridge in §11.3 existed to copy a number between two places; a
    bridge that dies freezes the gauge, and the "bridge is dead" case
    needs its own alert (``bridge_failed_total``). Updating in place has
    no such gap: the only way the gauge goes stale is the emitter task
    dying, and the P0-C staleness alert catches exactly that.
    """

    def __init__(self, receiver: OTLPReceiver) -> None:
        self._receiver = receiver
        self.state = CanaryState()

    async def emit_once(self) -> str:
        """Write one canary span. Returns ``succeeded``/``rejected``/``failed``."""
        self.state.canary_writes_attempted_total += 1
        otlp_metrics.canary_writes_attempted_total.inc()

        unix_nano = time.time_ns()
        request = build_canary_request(unix_nano)

        try:
            result = await self._receiver._handle_batch(request)
        except Exception as exc:
            self._record_failure(type(exc).__name__)
            logger.exception(
                "akosha.canary.write_failed exc=%s", type(exc).__name__
            )
            return "failed"

        inserted = int(result.get("inserted", 0))
        deduped = int(result.get("deduped", 0))
        rejected = int(result.get("rejected", 0))

        if inserted == 1 and deduped == 0 and rejected == 0:
            self._record_success()
            return "succeeded"

        if inserted == 0 and deduped > 0 and rejected == 0:
            # The UNIQUE constraint rejected the row: another pod wrote
            # the same cycle. That is a REJECTION, not a failure -- the
            # write path demonstrably works.
            self.state.canary_writes_rejected_total += 1
            otlp_metrics.canary_writes_rejected_total.inc()
            logger.debug(
                "akosha.canary.duplicate_constraint_rejected deduped=%s", deduped
            )
            return "rejected"

        # Inserted nothing, inserted more than one, or the row was
        # quarantined. The write path is not doing what it should.
        self._record_failure("unexpected_result")
        logger.warning(
            "akosha.canary.unexpected_result inserted=%s deduped=%s rejected=%s",
            inserted,
            deduped,
            rejected,
        )
        return "failed"

    def _record_success(self) -> None:
        self.state.canary_writes_succeeded_total += 1
        self.state.last_canary_success_unix_nano = time.time_ns()
        otlp_metrics.canary_writes_succeeded_total.inc()
        otlp_metrics.canary_last_success_unix_nano.set(
            self.state.last_canary_success_unix_nano
        )

    def _record_failure(self, reason: str) -> None:
        self.state.canary_writes_failed_total += 1
        self.state.last_canary_failure_unix_nano = time.time_ns()
        otlp_metrics.canary_writes_failed_total.labels(reason=reason).inc()


__all__ = [
    "CANARY_EMIT_INTERVAL_SECONDS",
    "CANARY_NAMESPACE",
    "CANARY_SERVICE_NAME",
    "CANARY_SPAN_NAME",
    "CANARY_TRACE_ID",
    "CanaryEmitter",
    "CanaryState",
    "build_canary_request",
    "canary_span_id",
]
