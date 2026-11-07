"""OTLP receiver — the ingest path for ``spans_substrate``.

Per spec §6 (docs/specs/2026-10-19-bodai-substrate-otel-v7.md), the receiver:

- Binds the canonical OTLP ports (:4317 gRPC, :4318 HTTP), operator-settable
  via the ``grpc_port``/``http_port`` constructor arguments. The v7 port-owner
  amendment (``docs/decisions/2026-10-21-otel-substrate-port-ownership.md``)
  is what makes the canonical ports belong to the substrate at all: prior to
  that decision, Grafana Alloy held them and traces were discarded by its
  debug exporter.
- Validates every row against ``REQUIRED_FIELDS`` before INSERT. Bad rows go
  to ``_dlq_rows``, whose own write is wrapped so a DLQ outage cannot abort a
  batch that had good rows in it (§15).
- Relies on ``HotStore.insert_spans_substrate`` for replay-is-a-no-op. The
  conflict clause is untargeted; see that method for why §5.1's DDL makes the
  composite constraint unreachable.
- Fails loud at boot via ``LifecycleError("spans_substrate_missing")`` if the
  substrate is absent — rather than binding and silently producing nothing,
  which is the failure mode this whole substrate exists to prevent.

The receiver never opens its own connection. It borrows the lifespan-owned
``HotStore``, the same instance the read path queries through ``SpanQuery``,
published by the lifespan via ``set_shared_hot_store`` /
``set_shared_otel_receiver``.

Two deviations from the spec, both forced by the fleet's actual shape:

1. ``LifecycleError`` is imported from ``oneiric.core.lifecycle``, not
   ``mcp_common.errors``. There is no ``mcp_common.errors`` module; the class
   lives in oneiric's lifecycle module and subclasses ``RuntimeError``.
2. The HTTP listener is a Starlette app built here, not an opentelemetry
   exporter. ``opentelemetry.exporter.otlp.proto.http`` ships no servicer or
   ASGI app — it is a client-side exporter package. The receiver is the
   server, so it parses ``ExportTraceServiceRequest`` off the raw protobuf
   body itself.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import random
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from oneiric.core.lifecycle import LifecycleError

from akosha.observability import otlp_metrics
from akosha.observability.canary_watcher import CANARY_SERVICE_NAME
from akosha.observability.otlp_prometheus_bridge import OTLPPrometheusBridge
from akosha.observability.resource_validator import ResourceAttributeValidator

if TYPE_CHECKING:
    from akosha.storage.hot_store import HotStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RowValidationError:
    """A single-row rejection reason.

    Frozen so the same instance can serve as a guard return value and as a
    dedup key against repeated logging. The spec uses ``reason`` and ``field``
    as the ``row_rejected{reason,field}`` metric labels.
    """

    field: str
    reason: str
    value: object = None


class OTLPReceiver:
    """OTLP span receiver.

    Constructor takes the lifespan-owned ``HotStore`` rather than opening a
    connection. Two writers to one DuckDB file from one process is how you get
    a lock contention error under load, and the read path is already holding
    the connection this would duplicate.

    Ports are constructor arguments rather than a Pydantic settings model so
    the lifespan controls them explicitly — the settings key defaults live in
    the lifespan, where the rest of akosha's configuration resolves.
    """

    # Spec §6: tunables pinned at class level so they are discoverable in
    # tooling and patchable per-test.
    MAX_BATCH_SIZE: int = 512
    PROMETHEUS_PORT: int = 9464
    GRPC_PORT: int = 4317
    HTTP_PORT: int = 4318

    REQUIRED_FIELDS = frozenset(
        {
            "service_name",
            "service_namespace",  # v4 — see §7.1
            "span_name",
            "start_time_unix_nano",
        }
    )
    ENUM_FIELDS = {
        "status_code": {
            "STATUS_CODE_UNSET",
            "STATUS_CODE_OK",
            "STATUS_CODE_ERROR",
        }
    }

    # The metric path's required set is deliberately SMALLER than the span
    # path's, and the difference is load-bearing rather than an oversight:
    # ``service.namespace`` is NOT required for a metric point.
    #
    # oneiric's ``OTLPObservabilityAdapter`` builds its Resource with
    # ``service.name``, ``deployment.environment`` and (optionally)
    # ``service.version`` — and NO namespace at all
    # (``oneiric/adapters/monitoring/otlp.py``). Requiring a namespace
    # would quarantine every metric oneiric sends, which is the whole of
    # REQ-M07's second producer: the export would answer "accepted, zero
    # points stored", and nothing would say why.
    #
    # The namespace is still stored — as the empty string when the producer
    # declares none. An empty string says "not declared", which is a
    # different fact from "unknown", and it keeps the substrate's NOT NULL
    # constraint satisfied without inventing a value.
    METRICS_REQUIRED_FIELDS = frozenset(
        {
            "service_name",
            "metric_name",
            "ts",
            "data_type",
        }
    )
    METRICS_DATA_TYPES = frozenset(
        {
            "sum",
            "gauge",
            "histogram",
            "exponential_histogram",
            "summary",
        }
    )

    def __init__(
        self,
        hot_store: HotStore,
        *,
        grpc_port: int | None = None,
        http_port: int | None = None,
        resource_validator: ResourceAttributeValidator | None = None,
    ) -> None:
        self._hot_store = hot_store
        # PR-A6. One validator per receiver, not per row: it owns the drift
        # history, and a per-row instance would detect nothing.
        self._resource_validator = resource_validator or ResourceAttributeValidator()
        self._grpc_port = self.GRPC_PORT if grpc_port is None else grpc_port
        self._http_port = self.HTTP_PORT if http_port is None else http_port
        self._grpc_server: Any | None = None
        self._http_runner: Any | None = None
        self._http_task: asyncio.Task[None] | None = None
        # Counters the operator reads to tell "receiving" from "receiving
        # nothing new" apart. In-process for PR-A3; the Prometheus exporter
        # that publishes them is PR-A4's job.
        self.receiver_started_total = 0
        self.rows_inserted_total = 0
        self.rows_deduped_total = 0
        self.rows_rejected_total = 0
        self.dlq_insert_failed_total = 0
        # Batches whose substrate write raised. Distinct from
        # ``dlq_insert_failed_total``: that is the quarantine write
        # failing (the bad rows are lost), this is the STORE write
        # failing (the good rows are lost). Both mean data loss, and an
        # operator reading only one of them would miss half the outage.
        self.rows_insert_failed_total = 0
        # Ingestion-cycle bookkeeping for the /health feed. ``batches_total``
        # is the receiver's analogue of the retired poller's ``_cycles_total``:
        # a monotonically increasing count of ingest cycles, which is what the
        # health aggregator reads to distinguish "producer alive but has seen
        # no data" from "producer dead".
        self.batches_total = 0
        # The HEALTH cycle count, kept separate from ``batches_total``.
        #
        # ``batches_total`` counts Export RPCs received, so it is zero until a
        # producer actually sends something. But a push producer's first cycle
        # is its STARTUP: bind the port, assert the substrate, and it is
        # alive. If the /health feed reports the batch count as its cycle
        # count, the feed sits at ``cycles_total=0`` from boot until the first
        # export -- and the aggregator maps "alive but never cycled" to
        # ``feed_never_populated``, which is DEGRADED, which is a 503.
        #
        # That 503 is not cosmetic. The launchd wrapper polls /health with
        # ``curl -fsS``, and ``-f`` fails on any 4xx/5xx, so a degraded
        # /health makes the wrapper kill the server it was supposed to
        # start. The service then crash-loops: launchd restarts it, it comes
        # up with the same zero count, and /health is 503 again.
        #
        # So the receiver counts its startup as its first cycle. The
        # heartbeat watcher (PR-A5) is the instrument that answers "is the
        # producer ALIVE", and the canary (PR-A4) answers "is the receiver
        # WORKING"; the feed's cycle count only has to answer "has it
        # started".
        self.cycles_total = 0
        self.last_batch_at: float | None = None
        self.last_error_at: float | None = None
        # The canary emitter, once ``run_canary_emitter`` has started. Its
        # state is the receiver's own proof that it can still write.
        self.canary_emitter: Any | None = None
        # The operator's "sweep now" request.
        #
        # This is a SIGNAL, not a command. The sweep loop owns the single
        # DuckDB connection to the substrate; a caller that ran the sweep
        # itself would put a second DELETE against that connection, and two
        # coroutines deleting on one DuckDB handle is a race, not a feature.
        # The request wakes the loop, and the loop -- the sole writer --
        # performs the sweep.
        self._sweep_requested = asyncio.Event()
        # The interval the sweep loop is actually running on, published when
        # the loop starts. The operator tool reports THIS, not a re-read of
        # the settings: if the settings changed since boot, the running
        # receiver's interval is the one the operator is waiting out.
        self.sweep_interval_s: int | None = None
        # The metric path's Prometheus bridge. One per receiver: the
        # temporality state it keeps is per series, and a restarted
        # receiver re-baselines instead of adding a stale delta to
        # whatever the previous receiver left in the shared registry.
        self._prometheus_bridge = OTLPPrometheusBridge()
        # In-process counters for the metric path, mirroring the span
        # path's idiom: the in-process source of truth, mirrored into the
        # registry at the same call sites so the two cannot drift. The
        # tests read THESE; the registry carries the same numbers for
        # operators.
        self.metrics_batches_total = 0
        self.metrics_received_total = 0
        self.metrics_inserted_total = 0
        self.metrics_deduped_total = 0
        self.metrics_rejected_total = 0
        self.metrics_points_exported_total = 0
        self.metrics_insert_failed_total = 0
        # The metric path's HEALTH cycle count, kept apart from
        # ``metrics_batches_total`` for the same reason the span path keeps
        # ``cycles_total`` apart from ``batches_total``: the batch count is
        # zero until a producer actually exports, and a /health feed whose
        # cycle count sits at zero from boot is DEGRADED — a 503 that makes
        # the launchd wrapper kill the server it just started. The startup
        # counts as the first cycle, so a receiver that has simply not been
        # exported to yet reads as WARMING UP, not broken.
        self.metrics_cycles_total = 0
        # When a metric batch was last processed, and when the metric path
        # last failed to write. The health feed reads both.
        self.metrics_last_batch_at: float | None = None
        self.metrics_last_error_at: float | None = None
        # Whether the metrics substrate can be written to, and why not when
        # it cannot. ``None`` means "not probed yet".
        #
        # The probe is a BIND-TIME check that does NOT raise. The trace
        # receiver asserts ``spans_substrate`` because a trace receiver
        # without a substrate stores nothing and there is no second
        # service on the port to keep working. The metric path is
        # different: the MetricsService rides the SAME gRPC server as the
        # TraceService, so failing the bind because a METRICS dependency
        # is missing would take the TRACE path down with it — and would
        # break every deployment that has not yet upgraded oneiric. The
        # metric path therefore degrades: it keeps serving, keeps
        # exposing to Prometheus, and says loudly that the durable write
        # is not happening.
        self._metrics_substrate_ready: bool | None = None
        self._metrics_substrate_detail: str = ""

    # ── lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> None:
        """Bind the OTLP listeners. Raise if the substrate is missing.

        The substrate assertion is the whole reason this method exists in this
        shape. A receiver that binds happily against an absent table looks
        healthy — it accepts connections, returns 200s, and stores nothing —
        and the only symptom is a read path that quietly returns empty forever.

        The METRICS substrate is probed but NOT asserted, and the asymmetry is
        deliberate. The MetricsService rides the same gRPC server as the
        TraceService, so raising here would take the trace path down with it
        — and would break every deployment whose oneiric predates Phase 1's
        ``insert_metrics_substrate``. The metric path degrades instead: it
        keeps serving, keeps exposing to Prometheus, reports the substrate as
        unusable on ``/metrics`` and in the export's ``partial_success``, and
        the operator sees a 503 from ``/health`` rather than a green check
        over a metric that is thrown away.
        """
        if not await self._hot_store.table_exists(self._hot_store.spans_substrate_table):
            raise LifecycleError("spans_substrate_missing")

        await self._refresh_substrate_readiness(force=True)

        await self._start_grpc()
        await self._start_http()
        self.receiver_started_total += 1
        # The metric path's first cycle: the receiver is up and serving the
        # MetricsService, which is what the health feed's cycle count has to
        # answer. It does NOT mean a metric has been stored.
        self.metrics_cycles_total += 1
        # The startup cycle is complete: the substrate was asserted and both
        # listeners are bound. See ``cycles_total`` in ``__init__`` for why
        # this has to be counted.
        self.cycles_total += 1
        logger.info(
            "akosha.otlp_receiver.started grpc_port=%s http_port=%s",
            self._grpc_port,
            self._http_port,
        )

    async def _start_grpc(self) -> None:
        """Bind the OTLP/gRPC TraceService and MetricsService.

        Imports are local to this method so that importing this module — which
        the lifespan does unconditionally — does not drag in gRPC and the proto
        descriptors. A missing proto package should surface as a bind failure at
        boot, not as an ImportError during collection of an unrelated test.
        """
        import grpc  # type: ignore[import-not-found]
        from opentelemetry.proto.collector.metrics.v1 import (  # type: ignore[import-not-found]
            metrics_service_pb2_grpc,
        )
        from opentelemetry.proto.collector.trace.v1 import (  # type: ignore[import-not-found]
            trace_service_pb2_grpc,
        )

        self._grpc_server = grpc.aio.server()
        trace_service_pb2_grpc.add_TraceServiceServicer_to_server(
            _TraceServiceServicer(self), self._grpc_server
        )
        # The MetricsService rides the SAME server on the SAME port.
        #
        # One endpoint serving both signals is not a convenience, it is the
        # requirement: oneiric's ``OTLPObservabilityAdapter`` sends traces
        # and metrics to one endpoint (the plan's Finding 10), so a receiver
        # that serves only one of the two cannot satisfy that producer at
        # all. Splitting the endpoint would mean a second mechanism to
        # maintain for the same problem.
        metrics_service_pb2_grpc.add_MetricsServiceServicer_to_server(
            _MetricsServiceServicer(self), self._grpc_server
        )
        self._grpc_server.add_insecure_port(f"127.0.0.1:{self._grpc_port}")
        await self._grpc_server.start()

    async def _start_http(self) -> None:
        """Bind the OTLP/HTTP listener on ``/v1/traces``.

        uvicorn is driven as a task rather than run with ``server.run()`` so the
        receiver's own ``stop()`` owns the shutdown — a blocking call here would
        make the lifespan's reverse-order teardown impossible.
        """
        import uvicorn  # type: ignore[import-not-found]
        from starlette.applications import Starlette
        from starlette.responses import Response
        from starlette.routing import Route

        async def traces(request: Any) -> Response:
            body = await request.body()
            await self._handle_http(body)
            # OTLP/HTTP success is 200 with an ExportTraceServiceResponse.
            # Per the spec's partial-success semantics, rejected rows do NOT
            # make the export a failure — the producer has already handed the
            # data over and will not retry a 4xx. Rejections surface via
            # _dlq_rows and the counters instead.
            return Response(status_code=200, content=b"", media_type="application/x-protobuf")

        app = Starlette(routes=[Route("/v1/traces", traces, methods=["POST"])])
        self._http_runner = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=self._http_port,
                log_level=logging.WARNING,
                access_log=False,
            )
        )
        self._http_task = asyncio.create_task(self._http_runner.serve())

    async def stop(self) -> None:
        """Close both listeners. Idempotent.

        Shutdown order is HTTP then gRPC because the lifespan's reverse-order
        guarantee means the receiver stops last, after the tasks that could
        still be writing. Either listener may be absent if ``start()`` failed
        partway, so each is guarded rather than assumed.
        """
        if self._http_runner is not None:
            self._http_runner.should_exit = True
        if self._http_task is not None:
            try:
                await asyncio.wait_for(self._http_task, timeout=5.0)
            except TimeoutError, asyncio.CancelledError:
                self._http_task.cancel()
        if self._grpc_server is not None:
            await self._grpc_server.stop(grace=1.0)
        logger.info("akosha.otlp_receiver.stopped")

    # ── ingest path ─────────────────────────────────────────────────────

    def pre_validate(self, span: dict[str, Any]) -> RowValidationError | None:
        """Return a :class:`RowValidationError` if the row is unacceptable.

        Spec §6.1: ``REQUIRED_FIELDS`` must be neither NULL nor empty;
        ``ENUM_FIELDS`` must be in the allowed set; ``attributes`` must
        serialize to JSON.

        Iteration over ``REQUIRED_FIELDS`` is sorted so the reported ``field``
        is deterministic. ``frozenset`` iteration order varies with the hash
        seed, and a DLQ row whose ``field`` differs between two runs of the
        same batch is unreproducible from its own evidence.
        """
        # Checked FIRST: a span that could not be rendered is reported as
        # unrenderable, not as a missing required field. The stand-in row
        # carries placeholder values for the required fields, so without
        # this check the reason would be a misleading ``not_null`` on a
        # field the span may well have had.
        render_error = span.get("_render_error")
        if render_error:
            return RowValidationError(field="span", reason="unrenderable", value=render_error)
        for name in sorted(self.REQUIRED_FIELDS):
            value = span.get(name)
            if value is None or value == "":
                return RowValidationError(field=name, reason="not_null")
        for field, allowed in self.ENUM_FIELDS.items():
            value = span.get(field)
            if value is not None and value not in allowed:
                return RowValidationError(field=field, reason="enum", value=value)
        attributes = span.get("attributes")
        if attributes is not None:
            try:
                json.dumps(attributes)
            except (TypeError, ValueError) as exc:
                return RowValidationError(field="attributes", reason="json", value=str(exc))
        return None

    async def _handle_batch(self, batch: Any) -> dict[str, Any]:
        """Validate every row; INSERT the good ones; quarantine the bad.

        Per §15, a DLQ failure must NOT abort the batch: good rows still land,
        and the DLQ error is counted so the heartbeat watcher can surface it.
        The DLQ write is therefore wrapped, and the counters move either way.

        The substrate write is wrapped for the same reason and for one more:
        an exception escaping the servicer makes gRPC answer UNKNOWN, and the
        producer drops the ENTIRE batch — every span in it, not just the one
        that failed. The rows are lost either way; what wrapping changes is
        whether the loss takes the producer's retry budget with it.
        """
        spans = _extract_spans(batch).copy()
        self.batches_total += 1
        self.cycles_total += 1
        self.last_batch_at = time.time()
        good: list[dict[str, Any]] = []
        bad: list[tuple[RowValidationError, dict[str, Any]]] = []
        for span in spans:
            error = self.pre_validate(span)
            if error is None:
                # PR-A6: allowlist check, AFTER the required-field check.
                # The order matters: a span with no namespace must be
                # reported as the receiver's own ``not_null`` rather than as
                # the validator's ``namespace_missing``. Both quarantine the
                # row; only the reason label differs, and the reason is what
                # an operator reads.
                verdict = self._resource_validator.validate(span)
                if not verdict.accepted:
                    error = RowValidationError(
                        field="service_namespace",
                        reason=verdict.reason or "namespace_not_allowed",
                        value=verdict.seen_namespace,
                    )
                    logger.warning("akosha.otlp_receiver.namespace_drift %s", verdict.message)
            if error is None:
                good.append(span)
            else:
                bad.append((error, span))

        inserted, deduped = (0, 0)
        insert_error = ""
        if good:
            try:
                inserted, deduped = await self._hot_store.insert_spans_substrate(good)
            except Exception as exc:
                # The write failed, so the rows are gone. What must NOT
                # happen is the exception escaping: gRPC answers UNKNOWN and
                # the producer drops the whole batch — every span in it, not
                # just the one that failed. The DLQ write below is wrapped
                # for exactly this reason (§15), and the metric path's
                # insert is wrapped for the same reason.
                #
                # The failure is REPORTED, not swallowed. A
                # ``partial_success`` with an empty ``error_message`` says
                # "nothing to report" while the receiver is in fact throwing
                # the data away, and the producer has no way to tell the two
                # apart. ``rejected_spans`` deliberately stays at the
                # VALIDATION rejections: the two failure modes have
                # different remedies, and conflating them would make the
                # counter stop meaning "this field failed to validate".
                self.rows_insert_failed_total += 1
                self.last_error_at = time.time()
                otlp_metrics.spans_insert_failed_total.inc()
                insert_error = f"spans substrate write failed: {exc}"
                logger.exception("akosha.otlp_receiver.insert_failed rows=%s", len(good))

        if bad:
            await self._dlq_insert(bad)

        self.rows_inserted_total += inserted
        self.rows_deduped_total += deduped
        self.rows_rejected_total += len(bad)
        otlp_metrics.spans_received_total.inc(len(spans))
        otlp_metrics.spans_inserted_total.inc(inserted)
        otlp_metrics.spans_deduped_total.inc(deduped)
        # Tally by field before incrementing. The hot store is :memory:, so
        # _dlq_rows cannot be queried from outside the process -- this label is
        # the only externally-visible record of WHICH validation rule fired.
        # "139 rejected" is unactionable; "139 rejected, 139x service_namespace"
        # names the defect.
        for field, count in _count_by_field(bad).items():
            otlp_metrics.spans_rejected_total.labels(field=field).inc(count)
        return {
            "inserted": inserted,
            "deduped": deduped,
            "rejected": len(bad),
            "error_message": insert_error,
        }

    async def _handle_http(self, body: bytes) -> dict[str, Any]:
        """Decode an OTLP/HTTP protobuf body and run it through the batch path.

        An undecodable body returns the same counts-with-zero-rejected shape as
        a successful empty export. A malformed protobuf payload is the
        producer's bug and not a reason to answer 4xx — the producer would then
        retry a payload that can never parse.
        """
        from opentelemetry.proto.collector.trace.v1 import (  # type: ignore[import-not-found]
            trace_service_pb2,
        )

        request = trace_service_pb2.ExportTraceServiceRequest()
        try:
            request.ParseFromString(body)
        except Exception:
            logger.warning("akosha.otlp_receiver.http_body_undecodable bytes=%s", len(body))
            otlp_metrics.http_body_undecodable_total.inc()
            return {"inserted": 0, "deduped": 0, "rejected": 0}
        return await self._handle_batch(request)

    async def _dlq_insert(
        self,
        bad: list[tuple[RowValidationError, dict[str, Any]]],
    ) -> None:
        """Write rejected rows to ``_dlq_rows``.

        Wrapped per §15. The exception is logged once per batch rather than
        once per row: a DLQ outage rejects every row in the batch, and N
        identical tracebacks per batch is how a real outage gets lost in noise.
        """
        rows = [
            {
                "reason": error.reason,
                "field": error.field,
                "row_json": json.dumps(span, default=str),
                "trace_id": span.get("trace_id"),
                "span_id": span.get("span_id"),
            }
            for error, span in bad
        ]
        try:
            await self._hot_store.insert_dlq_rows(rows)
            otlp_metrics.dlq_inserts_total.inc(len(rows))
        except Exception as exc:
            self.dlq_insert_failed_total += 1
            self.last_error_at = time.time()
            otlp_metrics.dlq_insert_failed_total.inc()
            logger.error(
                "akosha.otlp_receiver.dlq_write_failed rows=%s reason=%s exc=%s",
                len(rows),
                bad[0][0].reason if bad else "unknown",
                exc,
            )

    # ── metric ingest path (plan Phase 2) ───────────────────────────────
    #
    # Deliberately PARALLEL to the span path above, not a branch of it.
    # ``_handle_batch`` walks ResourceSpans and writes spans_substrate; the
    # trace servicer's behaviour must not change because a second service
    # was added to the same port. Every rule the span path follows is
    # restated here rather than shared, because sharing the code would
    # couple the two paths' futures: a change to the span batch would
    # silently change the metric batch.

    async def _handle_metrics_batch(self, request: Any) -> dict[str, Any]:
        """Validate, persist, and expose every data point in an export.

        The same three rules the span path follows, restated:

        1. A point that fails validation is quarantined and the batch
           continues. It is counted in ``rejected_data_points``, so a
           producer reading the response can see that something was
           dropped without the export failing.
        2. A DLQ failure does not abort the batch.
        3. No exception escapes the servicer: gRPC would answer UNKNOWN
           and the producer would drop the ENTIRE batch — up to 512 spans'
           worth of telemetry from a single export.

        The returned dict also carries ``error_message``: the description a
        producer reads when something went wrong. An OTLP ``partial_success``
        with an empty message says "nothing to report" while the receiver
        is in fact throwing the data away — which is the failure this
        whole path exists to make visible.
        """
        # The walk is wrapped even though every per-point conversion inside
        # it is already guarded: an exception escaping the servicer makes
        # gRPC answer UNKNOWN, and the producer drops the ENTIRE batch —
        # every point in it, not just the one that failed. An export that
        # cannot be walked at all is therefore reported as empty, loudly,
        # rather than as an RPC failure.
        try:
            points = _extract_metric_points(request)
        except Exception:
            logger.exception("akosha.otlp_metrics.extract_failed")
            points = []
        self.metrics_batches_total += 1
        self.metrics_cycles_total += 1
        self.metrics_received_total += len(points)
        otlp_metrics.metrics_batches_total.inc()
        otlp_metrics.metrics_received_total.inc(len(points))

        good: list[dict[str, Any]] = []
        bad: list[tuple[RowValidationError, dict[str, Any]]] = []
        for point in points:
            error = self.pre_validate_metric(point)
            if error is None:
                good.append(point)
            else:
                bad.append((error, point))

        inserted, deduped = (0, 0)
        problems: list[str] = []
        if good:
            ready, detail = await self._refresh_substrate_readiness()
            if not ready:
                # The substrate cannot take the write. The points are NOT
                # lost from the Prometheus exposure — they are still
                # rendered on /metrics — but nothing durable backs them.
                # The producer is told, the operator is told, and the
                # export still succeeds: failing the RPC would make the
                # producer drop the batch, and the trace path on the same
                # port must not go down with it.
                self.metrics_insert_failed_total += 1
                otlp_metrics.metrics_insert_failed_total.labels(
                    reason="substrate_unavailable"
                ).inc()
                self.metrics_last_error_at = time.time()
                problems.append(f"metrics substrate unavailable: {detail}")
                logger.error(
                    "akosha.otlp_metrics.substrate_unavailable rows=%s detail=%s",
                    len(good),
                    detail,
                )
            else:
                try:
                    inserted, deduped = await self._hot_store.insert_metrics_substrate(good)
                except Exception:
                    # A substrate failure must not answer UNKNOWN. The
                    # producer would drop the whole batch, and the points
                    # that would have landed are gone with it. The failure
                    # is counted so an operator can see it, and the export
                    # still answers.
                    #
                    # The readiness is re-probed here: a table that was
                    # dropped after boot reads as "the substrate is gone",
                    # which is a different fault from "this write failed"
                    # and has a different remedy.
                    still_ready, detail = await self._refresh_substrate_readiness(force=True)
                    reason = "write_failed" if still_ready else "substrate_unavailable"
                    self.metrics_insert_failed_total += 1
                    otlp_metrics.metrics_insert_failed_total.labels(reason=reason).inc()
                    self.metrics_last_error_at = time.time()
                    problems.append(
                        f"metrics substrate unavailable: {detail}"
                        if reason == "substrate_unavailable"
                        else "metrics substrate write failed"
                    )
                    logger.exception("akosha.otlp_metrics.insert_failed rows=%s", len(good))

        if bad:
            await self._metrics_dlq_insert(bad)

        exported = 0
        dropped_before = self._prometheus_bridge.dropped_by_reason.copy()
        for point in good:
            try:
                if self._prometheus_bridge.export(point):
                    exported += 1
            except Exception:
                # One unexposable point must not take the export down with
                # it. The point is already in the substrate; losing the
                # EXPOSURE is bad, losing the batch is worse.
                logger.exception(
                    "akosha.otlp_metrics.expose_raised metric=%s",
                    point.get("metric_name"),
                )

        self.metrics_inserted_total += inserted
        self.metrics_deduped_total += deduped
        self.metrics_rejected_total += len(bad)
        self.metrics_points_exported_total += exported
        self.metrics_last_batch_at = time.time()
        otlp_metrics.metrics_inserted_total.inc(inserted)
        otlp_metrics.metrics_deduped_total.inc(deduped)
        otlp_metrics.metrics_points_exported_total.inc(exported)
        # Tally by field before incrementing, for the same reason the span
        # path does: "139 rejected" is unactionable, and the hot store is
        # :memory: so the DLQ table cannot be queried from outside.
        for field, count in _count_by_field(bad).items():
            otlp_metrics.metrics_rejected_total.labels(field=field).inc(count)
        for reason, count in self._dropped_since(dropped_before).items():
            otlp_metrics.metrics_exposure_dropped_total.labels(reason=reason).inc(count)
        if bad:
            problems.append(
                f"{len(bad)} data point(s) rejected: "
                + ", ".join(f"{field}x{count}" for field, count in _count_by_field(bad).items())
            )
        return {
            "received": len(points),
            "inserted": inserted,
            "deduped": deduped,
            "rejected": len(bad),
            "exported": exported,
            "error_message": "; ".join(problems),
        }

    @property
    def metrics_substrate_ready(self) -> bool:
        """Whether the metrics substrate can be written to.

        ``False`` only when the substrate has been positively determined to
        be unusable; ``True`` before the first probe, because "not known
        yet" is not "known broken". The health feed reads this: a metrics
        path that cannot persist is DEGRADED, not healthy.
        """
        return self._metrics_substrate_ready is not False

    async def _refresh_substrate_readiness(self, force: bool = False) -> tuple[bool, str]:
        """Whether the metrics substrate can be written to, and why not.

        The probe checks BOTH halves of the dependency:

        * the ``insert_metrics_substrate`` primitive, which exists only on a
          oneiric that carries Phase 1, and
        * the ``metrics_substrate`` table, which only exists once that
          oneiric's DDL has run.

        Either one missing means nothing can be persisted, and the two have
        different causes: a missing primitive is a dependency gap (the
        installed oneiric predates the release), a missing table is a
        substrate that was never initialised.

        The result is cached — the probe costs a query — and refreshed
        whenever a write fails, so a table dropped after boot is noticed.
        The gauge is published on the shared registry, so the state is on
        ``/metrics`` whether or not anybody is reading the logs.
        """
        if not force and self._metrics_substrate_ready is not None:
            return (self._metrics_substrate_ready, self._metrics_substrate_detail)

        detail = ""
        ready = True
        if not hasattr(self._hot_store, "insert_metrics_substrate"):
            ready = False
            detail = (
                "the hot store has no insert_metrics_substrate primitive "
                "(the installed oneiric predates the release that carries it)"
            )
        else:
            table = getattr(self._hot_store, "metrics_substrate_table", "metrics_substrate")
            try:
                exists = await self._hot_store.table_exists(table)
            except Exception as exc:
                exists = False
                detail = f"the substrate table probe raised: {exc}"
            if not exists and not detail:
                ready = False
                detail = f"the {table} table does not exist"

        self._metrics_substrate_ready = ready
        self._metrics_substrate_detail = detail
        otlp_metrics.metrics_substrate_ready.set(1 if ready else 0)
        if not ready:
            logger.error(
                "akosha.otlp_metrics.substrate_unavailable detail=%s "
                "(metric exports are accepted and exposed on /metrics, but "
                "NOT persisted)",
                detail,
            )
        return (ready, detail)

    def pre_validate_metric(self, point: dict[str, Any]) -> RowValidationError | None:
        """Return a :class:`RowValidationError` if the point is unacceptable.

        The metric counterpart of :meth:`pre_validate`, with the metric
        path's own rules:

        * ``METRICS_REQUIRED_FIELDS`` must be neither NULL nor empty.
        * ``data_type`` must be one of the five the substrate's CHECK
          constraint allows.
        * ``ts`` must be a positive epoch-nanosecond timestamp. A point
          with no timestamp cannot be placed on a time series, and the
          substrate's identity includes it — a point at ``ts=0`` would
          collide with every other point at ``ts=0``.
        * ``attributes`` must serialize to JSON.
        * The numeric payload must be finite. A NaN in a DOUBLE column
          poisons every aggregate DuckDB computes over the column, and
          Prometheus cannot render one either.
        """
        # Checked FIRST, for the same reason the span path checks it
        # first: a point that could not be rendered is reported as
        # unrenderable, not as a missing required field.
        render_error = point.get("_render_error")
        if render_error:
            return RowValidationError(field="metric", reason="unrenderable", value=render_error)
        for name in sorted(self.METRICS_REQUIRED_FIELDS):
            value = point.get(name)
            if value is None or value == "":
                return RowValidationError(field=name, reason="not_null")
        timestamp = point.get("ts")
        if not isinstance(timestamp, int) or timestamp <= 0:
            return RowValidationError(field="ts", reason="not_positive", value=timestamp)
        data_type = point.get("data_type")
        if data_type not in self.METRICS_DATA_TYPES:
            return RowValidationError(field="data_type", reason="enum", value=data_type)
        attributes = point.get("attributes")
        if attributes is not None:
            try:
                json.dumps(attributes)
            except (TypeError, ValueError) as exc:
                return RowValidationError(field="attributes", reason="json", value=str(exc))
        for field in ("value_sum", "value_min", "value_max"):
            value = point.get(field)
            if value is None:
                continue
            try:
                finite = math.isfinite(float(value))
            except TypeError, ValueError:
                finite = False
            if not finite:
                return RowValidationError(field=field, reason="not_finite", value=value)
        return None

    async def _metrics_dlq_insert(
        self,
        bad: list[tuple[RowValidationError, dict[str, Any]]],
    ) -> None:
        """Write rejected metric points to ``_dlq_rows``.

        Same discipline as the span DLQ: the write is wrapped, so a DLQ
        outage cannot abort a batch that carried good points. The DLQ
        table has no metric-specific columns, so the point's identity
        travels in ``row_json`` — the substrate's projection ignores the
        row's extra keys, and the DLQ row is the evidence.
        """
        rows = [
            {
                "reason": error.reason,
                "field": error.field,
                "row_json": json.dumps(point, default=str),
                "trace_id": None,
                "span_id": None,
            }
            for error, point in bad
        ]
        try:
            await self._hot_store.insert_dlq_rows(rows)
            otlp_metrics.dlq_inserts_total.inc(len(rows))
        except Exception as exc:
            self.dlq_insert_failed_total += 1
            self.last_error_at = time.time()
            otlp_metrics.dlq_insert_failed_total.inc()
            logger.error(
                "akosha.otlp_metrics.dlq_write_failed rows=%s reason=%s exc=%s",
                len(rows),
                bad[0][0].reason if bad else "unknown",
                exc,
            )

    def _dropped_since(self, before: dict[str, int]) -> dict[str, int]:
        """The bridge's exposure drops since ``before`` was snapshotted."""
        current = self._prometheus_bridge.dropped_by_reason
        return {
            reason: count - before.get(reason, 0)
            for reason, count in current.items()
            if count - before.get(reason, 0) > 0
        }

    # ── lifespan tasks ──────────────────────────────────────────────────

    async def run_canary_emitter(self, stop_event: asyncio.Event) -> None:
        """Emit one canary span every ``CANARY_EMIT_INTERVAL_SECONDS`` (§11.3).

        The canary is the receiver proving to itself that it can still
        WRITE. The heartbeat watcher proves the substrate is queryable; a
        broken write path — a serializer that raises, a DLQ that swallows
        the error — would leave /health green while every producer's
        spans silently drop.

        The marker goes through the FULL production path (protobuf decode
        → convert → validate → INSERT), not a shortcut into the INSERT. A
        shortcut would miss the class of bug this exists to catch.

        A failed emit does NOT kill the task: the next cycle retries, and
        the staleness of ``canary_last_success_unix_nano`` is what the
        P0-C alert reads. A task that died on the first transient error
        would look identical to a task that is still running, and would
        stop testing the thing it exists to test.
        """
        from akosha.observability.canary_watcher import (
            CANARY_EMIT_INTERVAL_SECONDS,
            CanaryEmitter,
        )

        emitter = CanaryEmitter(self)
        self.canary_emitter = emitter
        interval_s = max(1, CANARY_EMIT_INTERVAL_SECONDS)

        while not stop_event.is_set():
            # Emit BEFORE the first wait, not after it.
            #
            # The gauge starts at 0, and the P0 rule reads
            # `time() - canary_last_success > 60s`. If the first emit
            # waited a full interval, the gauge would sit at 0 for 30
            # seconds on every boot -- and the rule carries `for: 30s`,
            # so a receiver starting up perfectly normally could page.
            #
            # This is the same reasoning as ``cycles_total``: the startup
            # IS the first cycle. A receiver that has just written a
            # canary is healthy, and the gauge has to say so.
            try:
                await emitter.emit_once()
            except Exception:
                # emit_once already records its own failures; this is the
                # belt-and-braces around the whole call. The task must
                # survive, or the canary stops testing the write path.
                logger.exception("akosha.canary.emit_raised")

            # The ``return`` inside the ``suppress`` block is the "stop was
            # requested" exit; a TimeoutError is suppressed instead, so the
            # loop falls through to the next cycle.
            with suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
                return

    def request_retention_sweep(self) -> bool:
        """Ask the sweep loop to run a retention sweep now.

        Returns True when this call queued the request, False when one was
        already pending (the pending one will run; queueing a second would
        only make the loop sweep twice for one request).

        The request is a SIGNAL. The sweep loop performs the sweep, because
        the loop -- not the caller -- owns the single DuckDB connection to
        the substrate. A caller that ran the sweep itself would race the
        loop's own DELETE on that connection.

        Returns immediately; the sweep happens on the loop's next turn. The
        caller watches ``akosha_otlp_sweep_last_completed_unix_nano`` to see
        that it landed.
        """
        if self._sweep_requested.is_set():
            return False
        self._sweep_requested.set()
        return True

    async def _wait_for_sweep_wakeup(self, stop_event: asyncio.Event, timeout: float) -> str:
        """Wait for the stop event, a sweep request, or the timeout.

        Returns ``"stop"``, ``"requested"``, or ``"timeout"``.

        Stop wins over a request: a receiver that is shutting down must not
        start a sweep on the way out.
        """
        stop_task = asyncio.ensure_future(stop_event.wait())
        request_task = asyncio.ensure_future(self._sweep_requested.wait())
        try:
            done, _pending = await asyncio.wait(
                {stop_task, request_task},
                timeout=max(0.0, timeout),
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            # Cancel whichever waiter is still pending and let the
            # cancellation settle, so Python does not warn about a task
            # destroyed while pending.
            for task in (stop_task, request_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stop_task, request_task, return_exceptions=True)
        if stop_task in done:
            return "stop"
        if request_task in done:
            return "requested"
        if stop_event.is_set():
            # A zero timeout can return before the waiter task gets a turn,
            # so the event may be set without its task having completed.
            return "stop"
        return "timeout"

    async def run_retention_sweep(self, stop_event: asyncio.Event) -> None:
        """Sweep both substrate tables on the §8.1/§8.2 interval.

        Two sweeps, one cadence. ``spans_substrate`` ages out on
        ``retention_days`` (30); ``_dlq_rows`` ages out at a flat 24h because a
        quarantined row is debugging evidence, not history — by day two the
        operator has either acted on it or not, and the table only ever grows.

        Cardinal rule (§15): retention filters on ``received_at_unix_nano``,
        the receiver's clock. A producer can claim any ``start_time`` it likes,
        and driving deletion off that would let a skewed clock delete rows
        that just arrived.

        The first sweep runs one full interval after start, not at t=0.
        ``sweep_first_started_unix_nano`` stays 0 until then — exactly the
        signal ``sweep_retention_late`` is gated on, so a pod whose sweep has
        simply not come due yet cannot page anyone. The one exception is an
        operator request (``request_retention_sweep``), which makes the
        sweep run early; the gauge then means "a sweep has run on this
        pod", which is what it is for.

        No cold archival here. §8.2 permits ``archival_target: null`` (no cold
        sink) as the default, and that is what this ships: adding an operator-
        configured egress path for span rows is a separate change with its own
        review, and until it exists the sweep deletes rather than stalling on a
        write it cannot make.

        Returns on ``stop_event`` being set. The lifespan also cancels this
        task; both paths are safe because nothing here is non-idempotent.
        """
        from akosha.config import get_config

        cfg = get_config().spans_substrate
        interval_s = max(1, cfg.retention_sweep_interval_hours) * 3600
        self.sweep_interval_s = interval_s
        first_started = 0

        while not stop_event.is_set():
            # Wait for the interval, the stop event, or an operator request.
            # A request wakes the loop early so the operator does not wait
            # out the interval to reclaim substrate space.
            wake = await self._wait_for_sweep_wakeup(stop_event, interval_s)
            if wake == "stop":
                return
            if wake == "timeout":
                # The interval elapsed. Random jitter so a fleet of pods does
                # not DELETE in lockstep. A REQUESTED sweep skips the jitter
                # entirely: the operator named one pod, and there is no fleet
                # to desynchronise from.
                wake = await self._wait_for_sweep_wakeup(stop_event, random.uniform(0, 60))
                if wake == "stop":
                    return
            # Either the interval elapsed (and the jitter too), or an
            # operator asked for this sweep. Both mean "sweep now".
            self._sweep_requested.clear()

            sweep_started = time.time_ns()
            if first_started == 0:
                first_started = sweep_started
                otlp_metrics.sweep_first_started_unix_nano.set(sweep_started)

            clean = True
            cutoff_ns = sweep_started - cfg.retention_days * 86_400 * 1_000_000_000
            deleted = self._sweep_table(
                self._hot_store.spans_substrate_table,
                "WHERE received_at_unix_nano < ?",
                [cutoff_ns],
            )
            if deleted is None:
                clean = False

            # Canary rows get a 24h horizon, not the substrate's 30 days.
            # At one row per 30 seconds that is 86,400 rows a day, and the
            # substrate is an IN-MEMORY DuckDB with a 1GB limit — on the
            # normal horizon the canary alone would fill it with millions
            # of rows inside a month. A canary row is liveness evidence,
            # not history: the same reasoning §8.1 applies to _dlq_rows.
            canary_cutoff_ns = sweep_started - 24 * 3600 * 1_000_000_000
            canary_deleted = self._sweep_table(
                self._hot_store.spans_substrate_table,
                "WHERE service_name = ? AND received_at_unix_nano < ?",
                [CANARY_SERVICE_NAME, canary_cutoff_ns],
                label="spans_substrate_canary",
            )
            if canary_deleted is None:
                clean = False
            elif canary_deleted > 0:
                otlp_metrics.canary_rows_deleted_total.inc(canary_deleted)

            dlq_deleted = self._sweep_table(
                self._hot_store.dlq_rows_table,
                "WHERE rejected_at < CURRENT_TIMESTAMP - INTERVAL '24 hours'",
            )
            if dlq_deleted is None:
                clean = False
            elif dlq_deleted > 0:
                otlp_metrics.dlq_rows_deleted_total.inc(dlq_deleted)

            # ``metrics_substrate`` ages out on the SAME horizon as
            # ``spans_substrate``: it is a sibling table in the same
            # substrate file, and a table that is not swept is a table
            # that grows without bound (~575 bytes a row, so ~39 MB a day
            # at 50 instruments on a 60s interval). The horizon is the
            # SAME ``retention_days`` setting, not a second constant.
            #
            # The column is ``inserted_at``, NOT an epoch-nanos column.
            # Phase 1 gave the table ``inserted_at TIMESTAMP NOT NULL
            # DEFAULT CURRENT_TIMESTAMP`` and no ``received_at_unix_nano``,
            # and the schema lives in oneiric — the receiver cannot add a
            # column to a table it does not own. The cardinal rule (§15)
            # still holds: ``inserted_at`` is stamped by the DATABASE's
            # clock, not by the producer's, so a producer with a skewed
            # clock still cannot drive deletion of rows that just
            # arrived. That is the same reasoning the DLQ sweep's
            # ``rejected_at`` uses.
            #
            # The sweep is skipped when the metrics substrate is not
            # usable: there is no table to sweep, and a delete against a
            # missing table would report a sweep failure every cycle.
            metrics_deleted = 0
            metrics_table = getattr(self._hot_store, "metrics_substrate_table", None)
            if metrics_table is not None:
                ready, _detail = await self._refresh_substrate_readiness()
                if ready:
                    metrics_deleted = self._sweep_table(
                        metrics_table,
                        "WHERE inserted_at < CURRENT_TIMESTAMP - (? * INTERVAL '1 day')",
                        [cfg.retention_days],
                        label="metrics_substrate",
                    )
                    if metrics_deleted is None:
                        clean = False
                    elif metrics_deleted > 0:
                        otlp_metrics.metrics_rows_deleted_total.inc(metrics_deleted)

            # 0 means "the last cycle did not finish cleanly". first_started is
            # never reset — that pair is how an operator tells "sweep failed"
            # from "sweep has never run" (runbook §11.1.6).
            otlp_metrics.sweep_last_completed_unix_nano.set(sweep_started if clean else 0)
            otlp_metrics.sweep_duration_seconds.observe((time.time_ns() - sweep_started) / 1e9)
            logger.info(
                "akosha.retention_sweep_complete cutoff_unix_nano=%s spans_deleted=%s "
                "dlq_deleted=%s metrics_deleted=%s clean=%s",
                cutoff_ns,
                deleted,
                dlq_deleted,
                metrics_deleted,
                clean,
            )

    def _sweep_table(
        self,
        table: str,
        where: str,
        params: list[Any] | None = None,
        label: str | None = None,
    ) -> int | None:
        """DELETE expired rows from ``table``. Row count, or ``None`` on failure.

        Returns ``None`` rather than 0 for a failed delete: "deleted nothing"
        and "could not run" are different facts, and collapsing them is how a
        broken sweep reports itself as healthy for weeks.

        ``label`` names the sweep in the failure counter. Two sweeps can
        target the same table with different horizons (the substrate's 30
        days and the canary's 24h), and a single ``table=`` label would
        make them indistinguishable in an alert.
        """
        try:
            conn = self._hot_store.conn
            if conn is None:
                raise RuntimeError("hot store not initialized")
            return conn.execute(f"DELETE FROM {table} {where}", params or []).rowcount
        except Exception:
            otlp_metrics.retention_sweep_failed_total.labels(table=label or table).inc()
            logger.exception("akosha.retention_sweep_failed table=%s", label or table)
            return None


class _TraceServiceServicer:
    """gRPC servicer bridging TraceService.Export to :meth:`_handle_batch`.

    Nested concern of the receiver, but a module-level class because gRPC's
    ``add_TraceServiceServicer_to_server`` expects a type and instantiates it
    against a ``TraceServiceServicer`` base that must be imported from the
    generated stubs. Subclassing that inside ``start()`` would mean building a
    class per bind.
    """

    def __init__(self, receiver: OTLPReceiver) -> None:
        self._receiver = receiver

    async def Export(self, request: Any, _context: Any) -> Any:
        """Return an ``ExportTraceServiceResponse``.

        The counts ride on ``partial_success.rejected_spans`` rather than the
        response body because that is the only place the OTLP protobuf carries
        them, and a producer reading them is the signal that a validation
        regression is visible from outside the process.
        """
        from opentelemetry.proto.collector.trace.v1 import (  # type: ignore[import-not-found]
            trace_service_pb2,
        )

        result = await self._receiver._handle_batch(request)
        return trace_service_pb2.ExportTraceServiceResponse(
            partial_success=trace_service_pb2.ExportTracePartialSuccess(
                rejected_spans=result["rejected"],
                # Non-empty only when the substrate write failed: the
                # export was answered, but the rows were lost. An empty
                # message next to a non-zero ``rejected_spans`` is the
                # normal validation-rejection shape and stays as it was.
                error_message=result.get("error_message", ""),
            )
        )


class _MetricsServiceServicer:
    """gRPC servicer bridging MetricsService.Export to ``_handle_metrics_batch``.

    The twin of :class:`_TraceServiceServicer`, and deliberately a separate
    class rather than a mode flag on the trace one: the trace servicer's
    behaviour must not change because a second service was added to the
    same port, and a flag is exactly the kind of coupling that makes the
    two paths' futures inseparable.

    Module-level for the same reason the trace servicer is: gRPC's
    ``add_MetricsServiceServicer_to_server`` expects a type and
    instantiates it against a generated base class that must be imported
    from the proto stubs. Subclassing that inside ``start()`` would mean
    building a class per bind.
    """

    def __init__(self, receiver: OTLPReceiver) -> None:
        self._receiver = receiver

    async def Export(self, request: Any, _context: Any) -> Any:
        """Return an ``ExportMetricsServiceResponse``.

        The counts ride on ``partial_success.rejected_data_points`` rather
        than the response body because that is the only place the OTLP
        protobuf carries them, and a producer reading them is the signal
        that a validation regression is visible from outside the process.

        ``partial_success`` is set ONLY when there is something to report.
        The OTLP spec says a fully successful export MUST NOT carry it: a
        producer that learns to expect the field learns to ignore it, and
        an always-present empty ``partial_success`` is exactly the shape a
        "the receiver is throwing your data away" message would have if it
        were always empty. The OTel Python SDK tolerates its absence.
        """
        from opentelemetry.proto.collector.metrics.v1 import (  # type: ignore[import-not-found]
            metrics_service_pb2,
        )

        result = await self._receiver._handle_metrics_batch(request)
        response = metrics_service_pb2.ExportMetricsServiceResponse()
        rejected = int(result["rejected"])
        message = str(result.get("error_message") or "")
        if rejected or message:
            response.partial_success.rejected_data_points = rejected
            response.partial_success.error_message = message
        return response


# ── helpers ──────────────────────────────────────────────────────────────


def _count_by_field(
    bad: list[tuple[RowValidationError, dict[str, Any]]],
) -> dict[str, int]:
    """Tally rejected rows by the field that failed validation.

    Sorted output would be nicer for tests, but Counter's natural ordering is
    insertion-ordered and pre_validate iterates ``REQUIRED_FIELDS`` sorted, so
    a single-rule batch is deterministic anyway.
    """
    counts: dict[str, int] = {}
    for error, _ in bad:
        key = error.field or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return counts


# ── metric helpers (plan Phase 2) ─────────────────────────────────────────

#: ``AggregationTemporality`` enum values. UNSPECIFIED is treated as
#: cumulative: the OTel SDKs report cumulative for cumulative instruments,
#: and reading an unspecified temporality as delta would double-count
#: every export.
_AGGREGATION_TEMPORALITY_DELTA = 1


@dataclass(frozen=True)
class _MetricContext:
    """The identity every data point in one scope inherits.

    OTLP puts the service identity on the RESOURCE and the scope identity
    on the SCOPE, so both are read once per scope and carried into every
    point's row. Reading ``service.name`` off a data point would find
    nothing — a NumberDataPoint has no resource field at all — and every
    metric in the system would be attributed to a nameless service, which
    is the same mistake the span path's mapping was written to avoid.
    """

    service_name: str
    service_namespace: str
    scope_name: str
    scope_version: str
    resource: dict[str, Any]


def _extract_metric_points(request: Any) -> list[dict[str, Any]]:
    """Yield substrate rows from an ``ExportMetricsServiceRequest``.

    Walks ``resource_metrics → scope_metrics → metrics → data points``.

    A data point that cannot be rendered becomes a stand-in row carrying
    ``_render_error``, exactly as the span path does for an unrenderable
    span: one bad point must not fail the whole Export RPC, because gRPC
    would answer UNKNOWN and the producer would drop the ENTIRE batch.
    """
    points: list[dict[str, Any]] = []
    for resource_metrics in getattr(request, "resource_metrics", None) or []:
        resource_attrs = {
            _attr_key(a): _attr_value(a)
            for a in getattr(resource_metrics.resource, "attributes", None) or []
        }
        service_name = str(resource_attrs.get("service.name") or "")
        service_namespace = str(resource_attrs.get("service.namespace") or "")
        for scope_metrics in getattr(resource_metrics, "scope_metrics", None) or []:
            scope = getattr(scope_metrics, "scope", None)
            scope_name = str(getattr(scope, "name", "") or "")
            scope_version = str(getattr(scope, "version", "") or "")
            scope_attrs = {
                _attr_key(a): _attr_value(a) for a in getattr(scope, "attributes", None) or []
            }
            context = _MetricContext(
                service_name=service_name,
                service_namespace=service_namespace,
                scope_name=scope_name,
                scope_version=scope_version,
                resource=_resource_payload(resource_attrs, scope_version, scope_attrs),
            )
            for metric in getattr(scope_metrics, "metrics", None) or []:
                points.extend(_metric_point_rows(metric, context))
    return points


def _resource_payload(
    resource_attrs: dict[str, Any],
    scope_version: str,
    scope_attrs: dict[str, Any],
) -> dict[str, Any]:
    """The resource JSON a metric row carries.

    The substrate stores the resource but keeps it OUT of the point's
    identity — the identity is the series plus the instant — which makes
    the column the right home for the scope's metadata: the schema has no
    scope column of its own, so a scope's version and attributes would
    otherwise be dropped on the floor. They land under ``otel.scope``,
    beside the resource attributes they were published with.
    """
    payload = resource_attrs.copy()
    scope_payload: dict[str, Any] = {}
    if scope_version:
        scope_payload["version"] = scope_version
    if scope_attrs:
        scope_payload["attributes"] = scope_attrs
    if scope_payload:
        payload["otel.scope"] = scope_payload
    return payload


def _metric_point_rows(metric: Any, context: _MetricContext) -> list[dict[str, Any]]:
    """Map one OTLP Metric onto substrate rows.

    OTLP has no explicit type tag: the type is whichever of sum / gauge /
    histogram / exponential_histogram / summary is SET on the Metric, and
    a Metric with none of them set carries no data point at all.
    """
    metric_name = str(getattr(metric, "name", "") or "")
    description = str(getattr(metric, "description", "") or "")
    unit = str(getattr(metric, "unit", "") or "")
    if metric.HasField("sum"):
        return _number_rows(
            metric.sum.data_points,
            context,
            metric_name=metric_name,
            description=description,
            unit=unit,
            data_type="sum",
            is_monotonic=bool(metric.sum.is_monotonic),
            temporality=_temporality_name(metric.sum.aggregation_temporality),
        )
    if metric.HasField("gauge"):
        return _number_rows(
            metric.gauge.data_points,
            context,
            metric_name=metric_name,
            description=description,
            unit=unit,
            data_type="gauge",
            is_monotonic=None,
            temporality="cumulative",
        )
    if metric.HasField("histogram"):
        return _histogram_rows(
            metric.histogram.data_points,
            context,
            metric_name=metric_name,
            description=description,
            unit=unit,
            temporality=_temporality_name(metric.histogram.aggregation_temporality),
        )
    if metric.HasField("exponential_histogram"):
        return _exponential_histogram_rows(
            metric.exponential_histogram.data_points,
            context,
            metric_name=metric_name,
            description=description,
            unit=unit,
            temporality=_temporality_name(metric.exponential_histogram.aggregation_temporality),
        )
    if metric.HasField("summary"):
        return _summary_rows(
            metric.summary.data_points,
            context,
            metric_name=metric_name,
            description=description,
            unit=unit,
        )
    return []


def _number_rows(
    data_points: Any,
    context: _MetricContext,
    *,
    metric_name: str,
    description: str,
    unit: str,
    data_type: str,
    is_monotonic: bool | None,
    temporality: str,
) -> list[dict[str, Any]]:
    """Rows for a sum or a gauge.

    A sum/gauge point is ONE sample, so ``value_sum`` is the sample,
    ``value_count`` is 1, and min and max are the sample itself.
    """
    rows: list[dict[str, Any]] = []
    for data_point in data_points or []:
        try:
            value = _number_value(data_point)
            row = _base_metric_row(
                context,
                metric_name=metric_name,
                data_type=data_type,
                description=description,
                unit=unit,
                attributes=_point_attributes(data_point),
                timestamp=_point_timestamp(data_point),
                temporality=temporality,
            )
        except Exception as exc:
            rows.append(_unrenderable_metric_row(metric_name, context, exc))
            continue
        row["value_sum"] = value
        row["value_count"] = 1
        row["value_min"] = value
        row["value_max"] = value
        # ``is_monotonic`` is meaningful only for a sum and stays NULL for
        # every other type: "not applicable" and "false" are different
        # facts, and a gauge is not a non-monotonic sum.
        row["is_monotonic"] = is_monotonic if data_type == "sum" else None
        rows.append(row)
    return rows


def _histogram_rows(
    data_points: Any,
    context: _MetricContext,
    *,
    metric_name: str,
    description: str,
    unit: str,
    temporality: str,
) -> list[dict[str, Any]]:
    """Rows for an explicit-bucket histogram.

    The bucket boundaries travel in ``buckets_json``: the summary
    statistics do not determine them, so a stored histogram would not be
    reconstructible without them.
    """
    rows: list[dict[str, Any]] = []
    for data_point in data_points or []:
        try:
            row = _base_metric_row(
                context,
                metric_name=metric_name,
                data_type="histogram",
                description=description,
                unit=unit,
                attributes=_point_attributes(data_point),
                timestamp=_point_timestamp(data_point),
                temporality=temporality,
            )
            row["value_sum"] = float(data_point.sum)
            row["value_count"] = int(data_point.count)
            row["value_min"] = _optional_float(data_point, "min")
            row["value_max"] = _optional_float(data_point, "max")
            row["buckets_json"] = {
                "explicit_bounds": [float(bound) for bound in data_point.explicit_bounds],
                "bucket_counts": [int(count) for count in data_point.bucket_counts],
            }
        except Exception as exc:
            rows.append(_unrenderable_metric_row(metric_name, context, exc))
            continue
        rows.append(row)
    return rows


def _exponential_histogram_rows(
    data_points: Any,
    context: _MetricContext,
    *,
    metric_name: str,
    description: str,
    unit: str,
    temporality: str,
) -> list[dict[str, Any]]:
    """Rows for an exponential histogram.

    The scale/offset layout is stored verbatim rather than flattened into
    explicit bounds: the exponential buckets are the producer's own
    encoding, and re-expressing them as explicit bounds would lose the
    scale that makes them compact.
    """
    rows: list[dict[str, Any]] = []
    for data_point in data_points or []:
        try:
            row = _base_metric_row(
                context,
                metric_name=metric_name,
                data_type="exponential_histogram",
                description=description,
                unit=unit,
                attributes=_point_attributes(data_point),
                timestamp=_point_timestamp(data_point),
                temporality=temporality,
            )
            row["value_sum"] = float(data_point.sum)
            row["value_count"] = int(data_point.count)
            row["value_min"] = _optional_float(data_point, "min")
            row["value_max"] = _optional_float(data_point, "max")
            row["buckets_json"] = {
                "scale": int(data_point.scale),
                "zero_count": int(data_point.zero_count),
                "zero_threshold": float(data_point.zero_threshold),
                "positive": {
                    "offset": int(data_point.positive.offset),
                    "bucket_counts": [int(count) for count in data_point.positive.bucket_counts],
                },
                "negative": {
                    "offset": int(data_point.negative.offset),
                    "bucket_counts": [int(count) for count in data_point.negative.bucket_counts],
                },
            }
        except Exception as exc:
            rows.append(_unrenderable_metric_row(metric_name, context, exc))
            continue
        rows.append(row)
    return rows


def _summary_rows(
    data_points: Any,
    context: _MetricContext,
    *,
    metric_name: str,
    description: str,
    unit: str,
) -> list[dict[str, Any]]:
    """Rows for a summary.

    A summary's quantile values have no column: the substrate's
    ``buckets_json`` is reserved for the two histogram types, and inventing
    a use for it would make the column mean two different things. The
    count and the sum are stored; the quantiles are exposed in Prometheus
    only as far as ``prometheus_client``'s Summary supports them, which
    is not at all.
    """
    rows: list[dict[str, Any]] = []
    for data_point in data_points or []:
        try:
            row = _base_metric_row(
                context,
                metric_name=metric_name,
                data_type="summary",
                description=description,
                unit=unit,
                attributes=_point_attributes(data_point),
                timestamp=_point_timestamp(data_point),
                temporality="cumulative",
            )
            row["value_sum"] = float(data_point.sum)
            row["value_count"] = int(data_point.count)
        except Exception as exc:
            rows.append(_unrenderable_metric_row(metric_name, context, exc))
            continue
        rows.append(row)
    return rows


def _base_metric_row(
    context: _MetricContext,
    *,
    metric_name: str,
    data_type: str,
    description: str,
    unit: str,
    attributes: dict[str, Any],
    timestamp: int,
    temporality: str,
) -> dict[str, Any]:
    """The substrate row shared by every metric data point.

    The dict carries two keys beyond the substrate's column list —
    ``temporality`` and ``scope_version`` — which the substrate's
    projection ignores (it reads the column list, not the row's keys) and
    the Prometheus bridge needs. The DLQ's ``row_json`` keeps them, so a
    quarantined point is still diagnosable.

    ``attributes`` and ``resource`` are passed as DICTS, never as
    pre-serialized JSON. The substrate canonicalizes them itself (sorted
    keys, compact separators) and DuckDB compares a JSON column inside a
    UNIQUE constraint **as text**: a pre-serialized string in the
    producer's own key order would make the same point look like two, and
    the same metric would be stored twice.
    """
    return {
        "ts": timestamp,
        "service_name": context.service_name,
        "service_namespace": context.service_namespace,
        "scope_name": context.scope_name,
        "metric_name": metric_name,
        "data_type": data_type,
        "unit": unit or None,
        "description": description or None,
        "value_sum": None,
        "value_count": None,
        "value_min": None,
        "value_max": None,
        # OTLP does not carry a sum of squares, and one cannot be derived
        # from the bucket boundaries without guessing where inside each
        # bucket the observations fell. NULL says "not known"; a fabricated
        # number would poison every variance computed over the column.
        "value_sum_of_squares": None,
        "is_monotonic": None,
        "buckets_json": None,
        "attributes": attributes,
        "resource": context.resource,
        "temporality": temporality,
        "scope_version": context.scope_version,
    }


def _unrenderable_metric_row(
    metric_name: str,
    context: _MetricContext,
    exc: Exception,
) -> dict[str, Any]:
    """A stand-in row for a metric point that could not be rendered.

    Carries the point's identity so the DLQ row can be correlated with the
    producer's logs, and ``_render_error`` so validation reports the real
    reason rather than a misleading ``not_null`` on a field the point may
    well have had.
    """
    row = _base_metric_row(
        context,
        metric_name=metric_name,
        data_type="unknown",
        description="",
        unit="",
        attributes={},
        timestamp=0,
        temporality="cumulative",
    )
    row["_render_error"] = f"{type(exc).__name__}: {exc}"
    return row


def _number_value(data_point: Any) -> float:
    """The sample a NumberDataPoint carries.

    ``as_double`` and ``as_int`` are a oneof: exactly one is set. proto3
    gives an unset oneof member its default (0.0 / 0), so reading a
    member directly would turn "unset" into "a real zero".

    An unset value RAISES rather than falling back to a default. Every
    other render failure in this path becomes a stand-in row carrying
    ``_render_error``, which validation quarantines and the DLQ records;
    a fabricated zero would be indistinguishable from a real measurement
    of 0.0 — the one value a producer is most likely to have meant
    something else by.
    """
    which = data_point.WhichOneof("value")
    if which == "as_int":
        return float(data_point.as_int)
    if which == "as_double":
        return float(data_point.as_double)
    raise ValueError("NumberDataPoint carries neither as_int nor as_double")


def _point_timestamp(data_point: Any) -> int:
    """The point's timestamp, in epoch NANOSECONDS.

    ``time_unix_nano`` is the instant the sample describes;
    ``start_time_unix_nano`` is the start of the aggregation interval and
    is only a fallback. NANOSECONDS, not milliseconds: the sibling
    ``spans_substrate`` table stores ``start_time_unix_nano`` in the same
    DuckDB file, and two time units in one substrate is a silent trap —
    every read path that compares the two would be off by a factor of a
    million.
    """
    stamp = int(getattr(data_point, "time_unix_nano", 0) or 0)
    if stamp:
        return stamp
    return int(getattr(data_point, "start_time_unix_nano", 0) or 0)


def _point_attributes(data_point: Any) -> dict[str, Any]:
    """The data point's attributes, unwrapped to plain Python."""
    return {
        _attr_key(attribute): _attr_value(attribute)
        for attribute in getattr(data_point, "attributes", None) or []
    }


def _optional_float(data_point: Any, field: str) -> float | None:
    """An optional proto3 double, or None when the producer did not set it."""
    if not data_point.HasField(field):
        return None
    return float(getattr(data_point, field))


def _temporality_name(temporality: Any) -> str:
    """``cumulative`` or ``delta`` for an aggregation temporality enum."""
    try:
        value = int(temporality)
    except TypeError, ValueError:
        return "cumulative"
    if value == _AGGREGATION_TEMPORALITY_DELTA:
        return "delta"
    return "cumulative"


def _extract_spans(batch: Any) -> list[dict[str, Any]]:
    """Yield row dicts from an ``ExportTraceServiceRequest`` or a plain list.

    Both shapes are supported because the tests drive this with lists while
    production only ever sees the protobuf. Supporting one shape in the helper
    and the other in a test double would let the test pass against a path
    production never takes.
    """
    rows: list[dict[str, Any]] = []
    if isinstance(batch, list):
        rows.extend(batch)
        return rows
    for resource_spans in getattr(batch, "resource_spans", None) or []:
        resource_attrs = {
            _attr_key(a): _attr_value(a)
            for a in getattr(resource_spans.resource, "attributes", None) or []
        }
        for scope_spans in getattr(resource_spans, "scope_spans", None) or []:
            for span in getattr(scope_spans, "spans", None) or []:
                try:
                    rows.append(_proto_to_dict(span, resource_attrs))
                except Exception as exc:
                    # One unrenderable span must not fail the whole Export
                    # RPC. An exception here escapes the gRPC servicer, gRPC
                    # answers UNKNOWN, and the producer drops the ENTIRE
                    # batch — up to 512 spans from a BatchSpanProcessor. The
                    # span is quarantined instead: the DLQ row is the
                    # evidence, and the rest of the batch still lands.
                    rows.append(_unrenderable_row(span, exc))
    return rows


def _unrenderable_row(span: Any, exc: Exception) -> dict[str, Any]:
    """A stand-in row for a span that could not be rendered.

    Carries the span's identity so the DLQ row can be correlated with the
    producer's logs, and ``_render_error`` so ``pre_validate`` reports the
    real reason rather than a misleading ``not_null`` on a field the span
    may well have had.
    """
    return {
        "span_id": _hex(getattr(span, "span_id", b"")),
        "trace_id": _hex(getattr(span, "trace_id", b"")),
        "parent_span_id": _hex(getattr(span, "parent_span_id", b"")),
        "span_name": getattr(span, "name", "") or "",
        "start_time_unix_nano": int(getattr(span, "start_time_unix_nano", 0) or 0),
        "end_time_unix_nano": int(getattr(span, "end_time_unix_nano", 0) or 0),
        "received_at_unix_nano": time.time_ns(),
        "status_code": "STATUS_CODE_UNSET",
        "status_message": None,
        "attributes": "{}",
        "resource": "{}",
        "events": "[]",
        "_render_error": f"{type(exc).__name__}: {exc}",
    }


def _proto_to_dict(span: Any, resource_attrs: dict[str, str]) -> dict[str, Any]:
    """Map a protobuf Span onto a substrate row.

    ``resource_spans``-level attributes are passed in rather than read off the
    span: in OTLP the service identity lives on the *resource*, not the span,
    so a Span message has no ``resource`` attribute at all. Reaching for one
    (as a first draft here did) silently yields the "unknown" defaults below
    for every row, and every span in the system looks like it came from a
    service called "unknown".
    """
    attrs = {_attr_key(a): _attr_value(a) for a in span.attributes}
    computed_duration_ms = (
        int(span.end_time_unix_nano) - int(span.start_time_unix_nano)
    ) / 1_000_000
    row: dict[str, Any] = {
        "span_id": _hex(span.span_id),
        "trace_id": _hex(span.trace_id),
        "parent_span_id": _hex(span.parent_span_id),
        "span_name": span.name or "",
        "start_time_unix_nano": int(span.start_time_unix_nano),
        "end_time_unix_nano": int(span.end_time_unix_nano),
        # The RECEIVER's clock, never the producer's. Spec §15 calls this the
        # cardinal rule: retention sweeps on this column, and a producer whose
        # clock is skewed — or a batch replayed from a queue hours after the
        # fact — would otherwise have its spans deleted before they were ever
        # readable, or kept past retention. It is the one timestamp in the row
        # that does not come off the wire.
        "received_at_unix_nano": time.time_ns(),
        "status_code": _status_name(span.status.code),
        "status_message": span.status.message or None,
        "attributes": json.dumps(attrs),
        "resource": json.dumps(resource_attrs),
        "events": json.dumps(
            [{"name": e.name, "time_unix_nano": int(e.time_unix_nano)} for e in span.events]
        ),
    }
    # ── promote well-known attributes into their dedicated columns ──────
    # The substrate has first-class columns for these; SpanQuery.summary
    # filters on the COLUMNS, not on the attributes blob. Leaving them in
    # the JSON alone makes the entire read path return empty while ingest
    # looks perfectly healthy — rows are landing, the table is populated,
    # and every query misses.
    #
    # Vocabulary follows akosha's existing OtelTraceIngester._normalize_span
    # (akosha/ingestion/otel_ingester.py), which established the same
    # mapping for the conversations table. Keeping one vocabulary means a
    # producer's attributes mean the same thing on both paths.
    row["task_class"] = attrs.get("task_class") or attrs.get("task.class")
    row["selector"] = attrs.get("selector")
    row["outcome"] = attrs.get("outcome")
    row["latency_ms"] = _as_float(attrs.get("duration_ms"), computed_duration_ms)
    row["duration_ms"] = computed_duration_ms
    row["telemetry_source"] = resource_attrs.get("service.name")
    row["telemetry_source_type"] = resource_attrs.get("telemetry.sdk.language")

    gen_ai = {
        "gen_ai.provider.name": "gen_ai_provider_name",
        "gen_ai.request.model": "gen_ai_request_model",
        "gen_ai.response.model": "gen_ai_response_model",
        "gen_ai.usage.input_tokens": "gen_ai_usage_input_tokens",
        "gen_ai.usage.output_tokens": "gen_ai_usage_output_tokens",
    }
    has_gen_ai = False
    for attr_key, column in gen_ai.items():
        value = attrs.get(attr_key)
        if value is not None:
            row[column] = value
            has_gen_ai = True
    cost = attrs.get("gen_ai.usage.cost")
    if cost is not None:
        row["gen_ai_usage_cost"] = cost
        has_gen_ai = True
    row["has_gen_ai_attrs"] = has_gen_ai

    # A dispatch span carries the routing decision that produced it. The
    # boolean exists so a reader can find them with an index scan rather
    # than JSON-contains over the whole table.
    row["has_dispatch_attrs"] = any(
        key in attrs for key in ("effective_selector", "pool_id", "caller_kind")
    )
    # No "unknown" default for service_name / service_namespace / span_name.
    # A sentinel here would make REQUIRED_FIELDS unreachable: the ingest path
    # would satisfy every required field on every row, so pre_validate could
    # never reject anything and the DLQ would stay permanently empty while
    # misconfigured producers quietly wrote rows no read-path filter could
    # ever match. Leaving them absent routes them to the DLQ, where an
    # operator sees them.
    #
    # Resource first, then the span's own attributes. Resource is where OTel
    # semantics say service identity belongs, but mcp-common's
    # FastMCPOpenTelemetryMiddleware sets service.name/service.namespace as
    # SPAN attributes (mcp_common/server/telemetry.py:102-103) — reading
    # resource only would quarantine every mahavishnu-dispatched span even
    # though the producer declared its identity. Span-level wins for neither;
    # a producer that states the value has stated it.
    row["service_name"] = resource_attrs.get("service.name") or attrs.get("service.name")
    row["service_namespace"] = resource_attrs.get("service.namespace") or attrs.get(
        "service.namespace"
    )
    if row["service_name"] is None:
        del row["service_name"]
    if row["service_namespace"] is None:
        del row["service_namespace"]
    row["deployment_environment"] = resource_attrs.get("deployment.environment") or attrs.get(
        "deployment.environment"
    )
    return row


def _hex(value: Any) -> str | None:
    """Bytes to lowercase hex, or ``None`` for the empty message.

    proto3 scalar defaults are ``b""``, not ``None``. A root span's
    ``parent_span_id`` and a batch's outer fields are both empty-but-present,
    and mapping those to the string ``""`` would make them look like real
    identities to dedup on.
    """
    if not value:
        return None
    return bytes(value).hex()


def _status_name(code: Any) -> str:
    """Map the Status enum onto the substrate's stored names."""
    return {
        0: "STATUS_CODE_UNSET",
        1: "STATUS_CODE_OK",
        2: "STATUS_CODE_ERROR",
    }.get(int(code), "STATUS_CODE_UNSET")


def _as_float(value: Any, fallback: float) -> float:
    """Coerce an attribute to float, falling back to the span's own duration.

    Producers send ``duration_ms`` as a string often enough that a p99 over
    the ``latency_ms`` column would trip on a type mismatch rather than
    returning a number. Falling back to the computed span duration is better
    than NULL: the fitness analyzer treats a missing latency as "no data"
    for the whole pair, when in fact the span's own timestamps were right
    there.
    """
    if value is None:
        return fallback
    try:
        return float(value)
    except TypeError, ValueError:
        return fallback


def _attr_key(attr: Any) -> str:
    value = getattr(attr, "key", "")
    return value if isinstance(value, str) else value.decode("utf-8", "replace")


def _attr_value(attr: Any) -> Any:
    """Unwrap a ``KeyValue``'s oneof ``value``.

    Returns the native Python type rather than a string: ``int_value`` and
    ``bool_value`` stringify into something that round-trips back as text and
    loses its type, which matters because ``duration_ms`` and the gen_ai token
    counters are read as numbers by the fitness analyzer.
    """
    return _any_value_to_python(getattr(attr, "value", None))


def _any_value_to_python(value: Any) -> Any:
    """Unwrap an OTLP ``AnyValue`` into a JSON-serializable Python value.

    ``AnyValue`` is a oneof over seven value shapes. ``WhichOneof`` names the
    set field, and ``getattr(value, which)`` yields:

    ==================  ==================================  ==============
    shape               Python type                         JSON-safe
    ==================  ==================================  ==============
    ``string_value``    ``str``                             yes
    ``bool_value``      ``bool``                            yes
    ``int_value``       ``int``                             yes
    ``double_value``    ``float``                           yes
    ``bytes_value``     ``bytes``                           **no**
    ``array_value``     ``ArrayValue`` protobuf message     **no**
    ``kvlist_value``    ``KeyValueList`` protobuf message   **no**
    ==================  ==================================  ==============

    Returning the raw protobuf for the last three is what a first cut did,
    and it was a real production failure. ``json.dumps`` raised
    ``TypeError: Object of type ArrayValue is not JSON serializable``,
    the exception escaped the gRPC servicer, gRPC answered
    ``StatusCode.UNKNOWN`` for the **whole Export RPC**, and the producer
    dropped the entire batch — up to 512 spans per failed export from a
    ``BatchSpanProcessor``. The trigger was ordinary: oneiric's resolver
    emits a ``resolver.decision`` span whose ``ordered`` attribute is a
    list of dicts, and pydantic-ai emits
    ``gen_ai.response.finish_reasons`` as a list.

    So the array and kvlist shapes are unwrapped **recursively** into plain
    Python lists and dicts, and bytes become base64 — the same encoding
    OTLP/JSON uses. The retired JSON-poll path in
    ``akosha/ingestion/otel_ingester.py`` already did this; the gRPC
    receiver regressed it.
    """
    if value is None:
        return None
    which = value.WhichOneof("value")
    if which is None:
        return None
    if which == "array_value":
        return [_any_value_to_python(v) for v in getattr(value.array_value, "values", None) or []]
    if which == "kvlist_value":
        return {
            _attr_key(kv): _any_value_to_python(kv.value)
            for kv in getattr(value.kvlist_value, "values", None) or []
        }
    if which == "bytes_value":
        return base64.b64encode(value.bytes_value).decode("ascii")
    return getattr(value, which)


__all__ = ["OTLPReceiver", "RowValidationError"]
