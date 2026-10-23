"""R-6 wiring: the analyzer reads the substrate the receiver writes.

PR-A3 gave the lifespan an OTLP receiver; R-6 had already rewritten the
analyzer to call ``SpanQuery.summary``. These tests pin the seam between
them, which is where both halves were individually correct and jointly
inert:

- The analyzer constructed with no ``SpanQuery`` did nothing.
- The receiver populated no ``task_class``/``selector``/``outcome``
  columns, so even a correctly-wired analyzer would have found nothing.

Either half alone looks healthy. The pair was the problem, which is why
these go end to end rather than testing the seam twice.
"""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.resource.v1 import resource_pb2
from opentelemetry.proto.trace.v1 import trace_pb2

from akosha.mcp.tools import group_registers as gr
from akosha.observability.otlp_receiver import OTLPReceiver
from akosha.processing.fitness_analyzer import FitnessAnalyzer
from akosha.storage.hot_store import HotStore


@pytest.fixture
async def store() -> HotStore:
    hs = HotStore(database_path=":memory:", embedding_dim=384)
    await hs.initialize()
    return hs


def _sv(value: str) -> common_pb2.AnyValue:
    return common_pb2.AnyValue(string_value=value)


def _request(
    span_id: bytes,
    *,
    task_class: str,
    selector: str,
    outcome: str = "success",
    duration_ms: str = "10",
) -> Any:
    return trace_service_pb2.ExportTraceServiceRequest(
        resource_spans=[
            trace_pb2.ResourceSpans(
                resource=resource_pb2.Resource(
                    attributes=[
                        common_pb2.KeyValue(key="service.name", value=_sv("mahavishnu")),
                        common_pb2.KeyValue(
                            key="service.namespace", value=_sv("bodai")
                        ),
                    ]
                ),
                scope_spans=[
                    trace_pb2.ScopeSpans(
                        spans=[
                            trace_pb2.Span(
                                trace_id=b"\x01" * 16,
                                span_id=span_id,
                                name="dispatch",
                                start_time_unix_nano=1_000_000_000,
                                end_time_unix_nano=2_000_000_000,
                                attributes=[
                                    common_pb2.KeyValue(
                                        key="task_class", value=_sv(task_class)
                                    ),
                                    common_pb2.KeyValue(
                                        key="selector", value=_sv(selector)
                                    ),
                                    common_pb2.KeyValue(
                                        key="outcome", value=_sv(outcome)
                                    ),
                                    common_pb2.KeyValue(
                                        key="duration_ms", value=_sv(duration_ms)
                                    ),
                                ],
                            )
                        ]
                    )
                ],
            )
        ]
    )


async def _ingest(store: HotStore, *requests: Any) -> OTLPReceiver:
    receiver = OTLPReceiver(store, grpc_port=0, http_port=0)
    for request in requests:
        await receiver._handle_batch(request)
    return receiver


# ── the seam ─────────────────────────────────────────────────────────────


async def test_a_wired_analyzer_reports_a_pair_that_emitted(
    store: HotStore,
) -> None:
    """The whole point: spans in, signal out.

    Every earlier state of this feature failed here while looking fine —
    no exception, no log line, just ``run_fitness_analysis`` returning
    ``{}`` forever.
    """
    await _ingest(
        store,
        _request(b"\x01" * 8, task_class="code_generation", selector="least_loaded"),
    )
    analyzer = FitnessAnalyzer(
        span_query=SpanQueryFor(store),
        declared_pairs=[("code_generation", "least_loaded")],
    )
    await analyzer._analyze_and_persist()
    signals = await analyzer.run_fitness_analysis()
    assert signals
    signal = signals["code_generation"]["least_loaded"]
    assert signal.samples == 1
    assert signal.failure_rate == 0.0


async def test_a_declared_pair_that_went_quiet_reports_zero_not_absence(
    store: HotStore,
) -> None:
    """The distinction the declared inventory exists to preserve.

    An undeclared pair cannot appear. A declared pair with no rows must
    still appear, carrying zero — otherwise "the producer migrated and
    stopped" and "nobody ever configured this" look identical.
    """
    await _ingest(
        store,
        _request(b"\x01" * 8, task_class="code_generation", selector="least_loaded"),
    )
    analyzer = FitnessAnalyzer(
        span_query=SpanQueryFor(store),
        declared_pairs=[
            ("code_generation", "least_loaded"),
            ("debugging", "round_robin"),  # declared, never emitted
        ],
    )
    await analyzer._analyze_and_persist()
    signals = await analyzer.run_fitness_analysis()
    assert "debugging" in signals, "a silent declared pair must still be reported"
    assert signals["debugging"]["round_robin"].samples == 0


async def test_an_error_outcome_shows_up_as_a_failure(store: HotStore) -> None:
    """The number the whole subsystem exists to produce is not always zero."""
    await _ingest(
        store,
        _request(b"\x01" * 8, task_class="code_generation", selector="least_loaded"),
        _request(
            b"\x02" * 8, task_class="code_generation", selector="least_loaded",
            outcome="error",
        ),
    )
    analyzer = FitnessAnalyzer(
        span_query=SpanQueryFor(store),
        declared_pairs=[("code_generation", "least_loaded")],
    )
    await analyzer._analyze_and_persist()
    signal = (await analyzer.run_fitness_analysis())["code_generation"]["least_loaded"]
    assert signal.samples == 2
    assert signal.failure_rate == pytest.approx(0.5)


async def test_latency_reaches_the_signal(store: HotStore) -> None:
    """``latency_ms`` must be promoted, or p99 is permanently NULL.

    The producer's ``duration_ms`` attribute is what carries the number;
    the substrate's ``latency_ms`` column is what the analyzer reads.
    """
    await _ingest(
        store,
        _request(
            b"\x01" * 8, task_class="code_generation", selector="least_loaded",
            duration_ms="250",
        ),
    )
    analyzer = FitnessAnalyzer(
        span_query=SpanQueryFor(store),
        declared_pairs=[("code_generation", "least_loaded")],
    )
    await analyzer._analyze_and_persist()
    signal = (await analyzer.run_fitness_analysis())["code_generation"]["least_loaded"]
    assert signal.p99_latency_ms == pytest.approx(250.0)


# ── the registration seam ────────────────────────────────────────────────


async def test_registration_borrows_the_shared_store(
    store: HotStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store must be the lifespan's, never a fresh one.

    ``_try_create_hot_store`` falls back to constructing a new in-memory
    DuckDB. That fallback is correct for the search tools and catastrophic
    for the analyzer: a different database from the one the receiver writes
    to means every cycle reports zero against an empty substrate, forever,
    with nothing in the logs.
    """
    monkeypatch.setattr("akosha.mcp.server.get_shared_hot_store", lambda: store)
    monkeypatch.setattr(gr, "_declared_pairs", lambda: [("code_generation", "least_loaded")])

    await _ingest(
        store,
        _request(b"\x01" * 8, task_class="code_generation", selector="least_loaded"),
    )

    captured: dict[str, Any] = {}

    import akosha.mcp.tools.fitness_tools as ft

    monkeypatch.setattr(ft, "register_fitness_tools", lambda _app: None)
    monkeypatch.setattr(ft, "init_fitness_analyzer", lambda a: captured.setdefault("a", a))

    class _App:
        pass

    gr.register_fitness_group(_App())  # type: ignore[arg-type]

    analyzer = captured["a"]
    assert analyzer._span_query is not None
    assert analyzer._span_query._store is store, (
        "the analyzer must hold the lifespan's HotStore, not a fresh one"
    )
    assert analyzer._declared_pairs == [("code_generation", "least_loaded")]


async def test_registration_without_a_lifespan_is_inert_not_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outside the lifespan there is no shared store, and that is fine.

    The group must still register so ``run_fitness_analysis`` exists and can
    explain itself. Raising here would take down every tool registration in
    the profile — a much worse failure than a tool that reports no data.
    """
    monkeypatch.setattr("akosha.mcp.server.get_shared_hot_store", lambda: None)
    monkeypatch.setattr(gr, "_declared_pairs", lambda: [])

    captured: dict[str, Any] = {}
    import akosha.mcp.tools.fitness_tools as ft

    monkeypatch.setattr(ft, "register_fitness_tools", lambda _app: None)
    monkeypatch.setattr(ft, "init_fitness_analyzer", lambda a: captured.setdefault("a", a))

    class _App:
        pass

    gr.register_fitness_group(_App())  # type: ignore[arg-type]

    assert captured["a"]._span_query is None
    assert captured["a"]._declared_pairs == []


# ── declared-pair parsing ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["code_generation:least_loaded"], [("code_generation", "least_loaded")]),
        # Split on the LAST colon so a namespaced task_class still works.
        (["dispatch:pool:least_loaded"], [("dispatch:pool", "least_loaded")]),
        (["a:b", "c:d"], [("a", "b"), ("c", "d")]),
    ],
)
def test_declared_pairs_parse(
    raw: list[str], expected: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from akosha.config import SpansSubstrateConfig

    class _Cfg:
        spans_substrate = SpansSubstrateConfig(declared_pairs=raw)

    monkeypatch.setattr("akosha.config.get_config", lambda: _Cfg())
    assert gr._declared_pairs() == expected


@pytest.mark.parametrize("bad", ["no-colon", "", ":only_selector", "only_task_class:"])
def test_a_malformed_entry_is_skipped_not_fatal(
    bad: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One typo must not take down every tool registration.

    The whole profile shares this code path, so raising on a malformed
    entry would turn a bad line of YAML into a server that will not boot.
    """
    from akosha.config import SpansSubstrateConfig

    class _Cfg:
        spans_substrate = SpansSubstrateConfig(
            declared_pairs=["code_generation:least_loaded", bad]  # type: ignore[list-item]
        )

    monkeypatch.setattr("akosha.config.get_config", lambda: _Cfg())
    assert gr._declared_pairs() == [("code_generation", "least_loaded")]


def test_an_unset_inventory_is_empty_not_a_cross_product() -> None:
    """No inventory means no pairs — never the 15x5 product.

    A cross product yields 75 rows of "configured but never emitted", which
    is the same healthy-looking-zero defect as an empty inventory, only
    louder and harder to spot.
    """
    from akosha.config import SpansSubstrateConfig

    assert SpansSubstrateConfig().declared_pairs == []


def SpanQueryFor(store: HotStore) -> Any:
    """Small indirection so the tests read as analyzer wiring, not plumbing."""
    from akosha.storage.span_query import SpanQuery

    return SpanQuery(store)