"""R-6: ``FitnessAnalyzer`` reads the substrate instead of polling MCP endpoints.

The distinction under test is the one read-path spec §5.7 / Q1 turns on, and
which ``docs/decisions/2026-10-21-fitness-analyzer-pair-inventory.md``
resolves: **a declared pair that emitted nothing must be visible in the report,
and must be distinguishable from a pair that was never declared.**

The code this replaces could not express either half. ``_analyze_and_persist``
did::

    for task_class in task_classes:          # hardcoded, not declared
        traces = await self._collect_traces(task_class)
        if not traces:
            continue                          # <-- silence is indistinguishable
        ...                                    #     from non-existence

and ``_collect_traces`` fanned out over ``self._component_endpoints``, a
registration-time list with zero writers in production (``add_component`` has
no call sites), so the whole loop was a no-op that reported nothing.

The signal under test rides on ``FitnessSignal.samples`` rather than on
``p99_latency_ms``: that field is typed ``float`` and cannot carry ``None``, so
"no rows" has to be a present-but-empty row rather than an absent one.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from akosha.processing.fitness_analyzer import FitnessAnalyzer
from akosha.storage.hot_store import HotStore
from akosha.storage.span_query import SpanQuery
from tests._support.spans_ddl import SPANS_SUBSTRATE_DDL

if TYPE_CHECKING:
    from pathlib import Path

BASE_RECEIVED = 1_700_000_000_000_000_000
WINDOW_START = BASE_RECEIVED - 1


@pytest.fixture
async def store(tmp_path: Path) -> HotStore:
    """A HotStore carrying the shared DDL.

    ``embedding_dim`` is required: omitting it makes ``__init__`` call the live
    ``resolve_embedding_dim(get_embedding_service())`` path.
    """
    hs = HotStore(database_path=tmp_path / "r6.duckdb", embedding_dim=384)
    await hs.initialize()
    hs.conn.execute(SPANS_SUBSTRATE_DDL)
    return hs


def _insert(
    hs: HotStore,
    span_id: str,
    *,
    selector: str | None,
    task_class: str | None,
    outcome: str = "success",
    latency: float | None = 10.0,
) -> None:
    """Insert one span. ``conn.execute`` is synchronous — never await it."""
    hs.conn.execute(
        "INSERT INTO spans_substrate (span_id, trace_id, service_name, "
        "service_namespace, span_name, start_time_unix_nano, "
        "received_at_unix_nano, outcome, selector, task_class, latency_ms, "
        "attributes, resource) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            span_id,
            f"trace-{span_id}",
            "mahavishnu",
            "bodai",
            "n",
            1,
            BASE_RECEIVED,
            outcome,
            selector,
            task_class,
            latency,
            json.dumps({"k": "v"}),
            json.dumps({"service.name": "mahavishnu"}),
        ],
    )


async def _analyze(
    store: HotStore, declared: list[tuple[str, str]]
) -> dict[str, dict]:
    """Run one analyzer cycle over ``store`` with ``declared`` pairs.

    Reads ``_signals`` directly rather than ``run_fitness_analysis()``, which
    would run a *second* cycle of its own.
    """
    analyzer = FitnessAnalyzer(
        declared_pairs=declared,
        span_query=SpanQuery(store),
        window_start_unix_nano=WINDOW_START,
    )
    await analyzer._analyze_and_persist()
    return analyzer._signals


# --- the distinction Q1 turns on ------------------------------------------


@pytest.mark.asyncio
async def test_declared_pair_with_no_spans_is_still_reported(
    store: HotStore,
) -> None:
    """A declared pair that emitted nothing appears, with samples == 0.

    This is the false negative §5.7 exists to prevent: the old code did
    ``if not traces: continue``, which made a silent pair vanish rather than
    report zero.
    """
    _insert(store, "s1", selector="least_loaded", task_class="code_generation")

    signals = await _analyze(
        store,
        declared=[
            ("code_generation", "least_loaded"),  # has data
            ("code_generation", "round_robin"),   # declared, silent
        ],
    )

    assert "round_robin" in signals["code_generation"], (
        "a declared-but-silent pair must still be listed — its absence is "
        "indistinguishable from it never having been declared"
    )
    silent = signals["code_generation"]["round_robin"]
    assert silent.samples == 0
    assert silent.failure_rate == 0.0
    # Silence must not score as perfect. ``1.0 - failure_rate`` with a zero
    # denominator yields 1.0, which would report a broken producer as healthy —
    # the exact "healthy-looking wrong number" Q1 exists to prevent.
    assert silent.score == 0.0


@pytest.mark.asyncio
async def test_undeclared_pair_is_absent_even_when_data_exists(
    store: HotStore,
) -> None:
    """A pair nobody declared does not appear, so drift is visible as absence."""
    _insert(store, "s1", selector="undeclared_selector", task_class="code_generation")

    signals = await _analyze(
        store, declared=[("code_generation", "least_loaded")]
    )

    assert "undeclared_selector" not in signals.get("code_generation", {})


@pytest.mark.asyncio
async def test_declared_with_data_and_declared_silent_are_distinguishable(
    store: HotStore,
) -> None:
    """The two declared states differ on ``samples`` — not on presence alone."""
    _insert(
        store,
        "s1",
        selector="least_loaded",
        task_class="code_generation",
        outcome="error",
    )
    _insert(store, "s2", selector="least_loaded", task_class="code_generation")

    signals = await _analyze(
        store,
        declared=[
            ("code_generation", "least_loaded"),
            ("code_generation", "affinity"),
        ],
    )

    populated = signals["code_generation"]["least_loaded"]
    silent = signals["code_generation"]["affinity"]
    assert populated.samples == 2
    assert silent.samples == 0
    assert populated.failure_rate == pytest.approx(0.5)
    assert silent.failure_rate == 0.0


# --- preserved signal semantics -------------------------------------------


@pytest.mark.asyncio
async def test_failure_rate_denominator_is_span_count(store: HotStore) -> None:
    """``failure_rate`` stays ``failure_count / span_count``.

    The old definition divided by ``len(traces)`` — every returned row, which
    is the same population ``SpanSummary.span_count`` covers. Reusing
    ``latency_ms IS NOT NULL`` rows here would silently redefine the reported
    number and nothing downstream would know.
    """
    _insert(
        store,
        "s1",
        selector="least_loaded",
        task_class="code_generation",
        outcome="error",
        latency=5.0,
    )
    _insert(
        store,
        "s2",
        selector="least_loaded",
        task_class="code_generation",
        outcome="success",
        latency=None,  # no latency, but still a row in the denominator
    )

    signals = await _analyze(
        store, declared=[("code_generation", "least_loaded")]
    )

    signal = signals["code_generation"]["least_loaded"]
    assert signal.samples == 2
    assert signal.failure_rate == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_non_scored_outcomes_stay_in_the_denominator(
    store: HotStore,
) -> None:
    """Only the literal ``"error"`` counts as a failure; everything else is a
    non-failure and still occupies the denominator.

    Regression guard for an ``outcome IN ('success', 'error')`` predicate in
    the substrate query, which would drop ``timeout`` / ``cancelled`` / NULL
    rows from ``span_count`` instead of scoring them as successes — changing
    the failure rate for every producer that emits them.
    """
    _insert(
        store,
        "s1",
        selector="least_loaded",
        task_class="code_generation",
        outcome="error",
    )
    _insert(
        store,
        "s2",
        selector="least_loaded",
        task_class="code_generation",
        outcome="timeout",
    )
    _insert(
        store,
        "s3",
        selector="least_loaded",
        task_class="code_generation",
        outcome="cancelled",
    )

    signals = await _analyze(store, declared=[("code_generation", "least_loaded")])

    signal = signals["code_generation"]["least_loaded"]
    assert signal.samples == 3, "all three rows are in the denominator"
    assert signal.failure_rate == pytest.approx(1 / 3)


@pytest.mark.asyncio
async def test_empty_declaration_yields_no_signals(store: HotStore) -> None:
    """No declared pairs means nothing to report — not a crash, not all-zeros."""
    _insert(store, "s1", selector="least_loaded", task_class="code_generation")

    signals = await _analyze(store, declared=[])

    assert signals == {}


@pytest.mark.asyncio
async def test_no_span_query_configured_is_a_no_op_not_a_crash(
    store: HotStore,
) -> None:
    """Without a SpanQuery the analyzer does nothing rather than raising."""
    analyzer = FitnessAnalyzer(declared_pairs=[("code_generation", "least_loaded")])

    await analyzer._analyze_and_persist()

    assert analyzer._signals == {}
