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
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from oneiric.core.lifecycle import LifecycleError

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

    def __init__(
        self,
        hot_store: HotStore,
        *,
        grpc_port: int | None = None,
        http_port: int | None = None,
    ) -> None:
        self._hot_store = hot_store
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

    # ── lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> None:
        """Bind the OTLP listeners. Raise if the substrate is missing.

        The substrate assertion is the whole reason this method exists in this
        shape. A receiver that binds happily against an absent table looks
        healthy — it accepts connections, returns 200s, and stores nothing —
        and the only symptom is a read path that quietly returns empty forever.
        """
        if not await self._hot_store.table_exists(self._hot_store.spans_substrate_table):
            raise LifecycleError("spans_substrate_missing")

        await self._start_grpc()
        await self._start_http()
        self.receiver_started_total += 1
        logger.info(
            "akosha.otlp_receiver.started grpc_port=%s http_port=%s",
            self._grpc_port,
            self._http_port,
        )

    async def _start_grpc(self) -> None:
        """Bind the OTLP/gRPC TraceService.

        Imports are local to this method so that importing this module — which
        the lifespan does unconditionally — does not drag in gRPC and the proto
        descriptors. A missing proto package should surface as a bind failure at
        boot, not as an ImportError during collection of an unrelated test.
        """
        import grpc  # type: ignore[import-not-found]
        from opentelemetry.proto.collector.trace.v1 import (  # type: ignore[import-not-found]
            trace_service_pb2_grpc,
        )

        self._grpc_server = grpc.aio.server()
        trace_service_pb2_grpc.add_TraceServiceServicer_to_server(
            _TraceServiceServicer(self), self._grpc_server
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
            except (TimeoutError, asyncio.CancelledError):
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
                return RowValidationError(
                    field="attributes", reason="json", value=str(exc)
                )
        return None

    async def _handle_batch(self, batch: Any) -> dict[str, int]:
        """Validate every row; INSERT the good ones; quarantine the bad.

        Per §15, a DLQ failure must NOT abort the batch: good rows still land,
        and the DLQ error is counted so the heartbeat watcher can surface it.
        The DLQ write is therefore wrapped, and the counters move either way.
        """
        spans = list(_extract_spans(batch))
        good: list[dict[str, Any]] = []
        bad: list[tuple[RowValidationError, dict[str, Any]]] = []
        for span in spans:
            error = self.pre_validate(span)
            if error is None:
                good.append(span)
            else:
                bad.append((error, span))

        inserted, deduped = (0, 0)
        if good:
            inserted, deduped = await self._hot_store.insert_spans_substrate(good)

        if bad:
            await self._dlq_insert(bad)

        self.rows_inserted_total += inserted
        self.rows_deduped_total += deduped
        self.rows_rejected_total += len(bad)
        return {"inserted": inserted, "deduped": deduped, "rejected": len(bad)}

    async def _handle_http(self, body: bytes) -> dict[str, int]:
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
        except Exception as exc:
            self.dlq_insert_failed_total += 1
            logger.error(
                "akosha.otlp_receiver.dlq_write_failed rows=%s reason=%s exc=%s",
                len(rows),
                bad[0][0].reason if bad else "unknown",
                exc,
            )

    # ── lifespan tasks ──────────────────────────────────────────────────

    async def run_canary_emitter(self, stop_event: asyncio.Event) -> None:
        """PR-A3 stub. PR-A4 swaps the body to emit one canary span per §11.3.

        The stub exists so the §6 lifespan ordering compiles at PR-A3; it is a
        real task awaiting the shutdown event rather than a bare ``return``, so
        the lifespan's cancel/gather sequence behaves identically before and
        after PR-A4 replaces the body.
        """
        await stop_event.wait()

    async def run_retention_sweep(self, stop_event: asyncio.Event) -> None:
        """PR-A3 stub. PR-A4 lands the §8.1 sweep body.

        Also a real task awaiting the event, for the same reason as the canary.
        """
        await stop_event.wait()


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

    async def Export(self, request: Any, context: Any) -> Any:
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
                error_message="",
            )
        )


# ── helpers ──────────────────────────────────────────────────────────────


def _extract_spans(batch: Any) -> list[dict[str, Any]]:
    """Yield row dicts from an ``ExportTraceServiceRequest`` or a plain list.

    Both shapes are supported because the tests drive this with lists while
    production only ever sees the protobuf. Supporting one shape in the helper
    and the other in a test double would let the test pass against a path
    production never takes.
    """
    if isinstance(batch, list):
        return list(batch)
    rows: list[dict[str, Any]] = []
    for resource_spans in getattr(batch, "resource_spans", None) or []:
        resource_attrs = {
            _attr_key(a): _attr_value(a)
            for a in getattr(resource_spans.resource, "attributes", None) or []
        }
        for scope_spans in getattr(resource_spans, "scope_spans", None) or []:
            for span in getattr(scope_spans, "spans", None) or []:
                rows.append(_proto_to_dict(span, resource_attrs))
    return rows


def _proto_to_dict(
    span: Any, resource_attrs: dict[str, str]
) -> dict[str, Any]:
    """Map a protobuf Span onto a substrate row.

    ``resource_spans``-level attributes are passed in rather than read off the
    span: in OTLP the service identity lives on the *resource*, not the span,
    so a Span message has no ``resource`` attribute at all. Reaching for one
    (as a first draft here did) silently yields the "unknown" defaults below
    for every row, and every span in the system looks like it came from a
    service called "unknown".
    """
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
        "attributes": json.dumps(
            {_attr_key(a): _attr_value(a) for a in span.attributes}
        ),
        "resource": json.dumps(resource_attrs),
        "events": json.dumps(
            [
                {"name": e.name, "time_unix_nano": int(e.time_unix_nano)}
                for e in span.events
            ]
        ),
    }
    # No "unknown" default for service_name / service_namespace / span_name.
    # A sentinel here would make REQUIRED_FIELDS unreachable: the ingest path
    # would satisfy every required field on every row, so pre_validate could
    # never reject anything and the DLQ would stay permanently empty while
    # misconfigured producers quietly wrote rows no read-path filter could
    # ever match. Leaving them absent routes them to the DLQ, where an
    # operator sees them.
    if "service.name" in resource_attrs:
        row["service_name"] = resource_attrs["service.name"]
    if "service.namespace" in resource_attrs:
        row["service_namespace"] = resource_attrs["service.namespace"]
    row["deployment_environment"] = resource_attrs.get("deployment.environment")
    row["duration_ms"] = (
        (int(span.end_time_unix_nano) - int(span.start_time_unix_nano)) / 1_000_000
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
    value = getattr(attr, "value", None)
    if value is None:
        return None
    which = value.WhichOneof("value")
    if which is None:
        return None
    return getattr(value, which)


__all__ = ["OTLPReceiver", "RowValidationError"]