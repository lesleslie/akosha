"""Characterisation tests for ``FitnessAnalyzer._compute_signal``.

These pin the arithmetic that exists **today**, before read-path R-6 replaces
the HTTP fan-out with ``SpanQuery.summary``. R-6 is an internal swap that must
not change any reported number, and this file is the only thing that can catch
it if it does.

That matters more than usual here, because the fan-out is currently dead.
``FitnessAnalyzer.add_component`` has zero call sites fleet-wide, so
``_component_endpoints`` is empty, ``_analyze_and_persist`` short-circuits, and
``run_fitness_analysis`` returns ``{}`` every cycle. There is no production
traffic and therefore no empirical baseline — these tests *are* the baseline.
A rewrite with no reference implementation to diff against can silently move a
number that nothing else in the system can detect.

So: assert what the code does, including the parts that look like bugs. The
p99 index below is the clearest example — for any sample count of 100 or fewer
it evaluates to the last element, so "p99" is really "max". Pinning that is
the point. If R-6 wants real percentile semantics, that is a *behaviour change*
to be made deliberately and separately, not something to discover afterwards.
"""

from __future__ import annotations

import pytest

from akosha.processing.fitness_analyzer import FitnessAnalyzer, FitnessSignal


def _trace(**kwargs: object) -> dict[str, object]:
    """One trace record as ``query_local_traces`` emits it."""
    base: dict[str, object] = {
        "outcome": "success",
        "duration_ms": 10.0,
        "selector": "pool_route_execute",
        "task_class": "mcp_tool_call",
        "component_name": "mahavishnu",
    }
    base.update(kwargs)
    return base


@pytest.fixture
def analyzer() -> FitnessAnalyzer:
    return FitnessAnalyzer()


# --- empty input ----------------------------------------------------------


def test_empty_traces_yields_the_default_signal(analyzer: FitnessAnalyzer) -> None:
    """Zero rows is not an error and not a 1.0 score — it is the *default*
    FitnessSignal, so score reads 0.0. A rewrite that returned
    ``score=1.0`` for silence would report a broken producer as perfect."""
    assert analyzer._compute_signal("t", "s", []) == FitnessSignal()


# --- the failure-rate denominator ----------------------------------------


def test_failure_rate_denominator_is_every_returned_row(
    analyzer: FitnessAnalyzer,
) -> None:
    """``error_count / len(traces)`` -- the denominator is ALL rows returned,
    not the rows that carry an outcome, and not just the errors.

    R-6 replaces this with ``SpanSummary.failure_count / span_count``. Those
    are the same population only if ``span_count`` stays the all-rows count.
    Filtering to ``latency_ms IS NOT NULL`` on the substrate side would
    quietly redefine the reported fitness number, and nothing downstream knows
    the definition moved.
    """
    traces = [_trace(outcome="error")] + [_trace(outcome="success")] * 3

    signal = analyzer._compute_signal("t", "s", traces)

    assert signal.samples == 4
    assert signal.failure_rate == pytest.approx(0.25)
    assert signal.score == pytest.approx(0.75)


def test_non_error_outcomes_are_successes_not_errors(
    analyzer: FitnessAnalyzer,
) -> None:
    """Only the literal string ``"error"`` counts. Anything else —
    ``"timeout"``, ``"cancelled"``, ``""`` from a missing field — is scored as
    a success, because the check is equality rather than membership.

    This is worth pinning precisely because it looks wrong. If R-6's
    ``outcome IN ('success', 'error')`` filter were copied naively into a
    rewrite, non-scored outcomes would *vanish* rather than being counted as
    successes, and the failure rate would change for every producer that
    emits them.
    """
    traces = [
        _trace(outcome="success"),
        _trace(outcome="timeout"),
        _trace(outcome="cancelled"),
        {},
    ]

    signal = analyzer._compute_signal("t", "s", traces)

    assert signal.samples == 4, "all four rows are in the denominator"
    assert signal.failure_rate == 0.0


def test_missing_duration_defaults_to_zero(
    analyzer: FitnessAnalyzer,
) -> None:
    """``float(t.get("duration_ms", 0.0))`` — a row with no duration is a 0.0,
    which lands at the *bottom* of the sorted distribution and drags p99 down
    when it reaches the selected index."""
    traces = [_trace(duration_ms=50.0), {}]

    signal = analyzer._compute_signal("t", "s", traces)

    assert signal.p99_latency_ms == 50.0
    assert signal.p99_latency_ms != 0.0


# --- the p99 index, which is not a p99 -----------------------------------


def test_p99_index_selects_the_maximum_for_small_samples(
    analyzer: FitnessAnalyzer,
) -> None:
    """``min(int(n * 0.99), n - 1)`` equals ``n - 1`` for every n <= 100, so
    below 101 samples "p99 latency" is the maximum.

    Derivation: ``int(n*0.99) >= n-1`` while ``n <= 100``, and ``min`` clamps
    it to ``n-1``. So with 100 or fewer traces the reported p99 is the worst
    single trace, not a tail percentile. Pinning this so a rewrite cannot
    quietly turn "worst-case" into "genuinely 99th-percentile" — the two give
    very different fitness numbers on the same data.
    """
    traces = [_trace(duration_ms=float(i)) for i in range(1, 11)]

    signal = analyzer._compute_signal("t", "s", traces)

    assert signal.p99_latency_ms == 10.0, "the max, not the 99th of 1..10"


def test_p99_index_becomes_a_real_percentile_above_100_samples(
    analyzer: FitnessAnalyzer,
) -> None:
    """Past n=100 the clamp stops binding and the index moves into the tail.
    1000 durations of 0..999 select index 990, not 999."""
    traces = [_trace(duration_ms=float(i)) for i in range(1000)]

    signal = analyzer._compute_signal("t", "s", traces)

    assert signal.samples == 1000
    assert signal.p99_latency_ms == 990.0


def test_p99_is_computed_over_sorted_durations(
    analyzer: FitnessAnalyzer,
) -> None:
    """Input order must not matter — the sort happens before the index is
    applied. An unsorted input that happens to work is order-dependent luck."""
    forwards = [_trace(duration_ms=float(i)) for i in range(1, 21)]
    backwards = list(reversed(forwards))

    assert (
        analyzer._compute_signal("t", "s", forwards).p99_latency_ms
        == analyzer._compute_signal("t", "s", backwards).p99_latency_ms
    )


# --- component accounting -------------------------------------------------


def test_component_count_is_distinct_component_names(
    analyzer: FitnessAnalyzer,
) -> None:
    traces = [
        _trace(component_name="mahavishnu"),
        _trace(component_name="mahavishnu"),
        _trace(component_name="akosha"),
    ]

    assert analyzer._compute_signal("t", "s", traces).component_count == 2


def test_missing_component_names_collapse_to_one(
    analyzer: FitnessAnalyzer,
) -> None:
    """``{t.get("component_name", "") for t in traces}`` — every row without a
    name contributes the same empty string, so N nameless rows count as ONE
    component, not N. Another definition a rewrite must carry over (or
    deliberately replace) rather than stumble into."""
    traces = [{}, {}, {}]

    assert analyzer._compute_signal("t", "s", traces).component_count == 1


# --- window ---------------------------------------------------------------


def test_window_start_is_sixty_minutes_before_updated_at(
    analyzer: FitnessAnalyzer,
) -> None:
    """Both timestamps are stamped from ``datetime.now(UTC)``, so they are not
    a real observation window — they are "now" and "now minus 60 minutes"
    regardless of when the traces actually arrived. Pinning it so R-6 does
    not assume the timestamps describe the data."""
    from datetime import datetime

    signal = analyzer._compute_signal("t", "s", [_trace()])

    assert signal.updated_at is not None
    assert signal.window_start is not None
    delta = datetime.fromisoformat(signal.updated_at) - datetime.fromisoformat(signal.window_start)
    assert delta.total_seconds() == pytest.approx(3600, abs=1.0)


def test_single_trace_is_perfect_score(analyzer: FitnessAnalyzer) -> None:
    """One successful trace: failure_rate 0.0, score 1.0, samples 1. The
    degenerate case most likely to divide by zero in a rewrite."""
    signal = analyzer._compute_signal("t", "s", [_trace()])

    assert (signal.score, signal.failure_rate, signal.samples) == (1.0, 0.0, 1)


def test_all_errors_scores_zero(analyzer: FitnessAnalyzer) -> None:
    signal = analyzer._compute_signal("t", "s", [_trace(outcome="error")] * 4)

    assert signal.failure_rate == pytest.approx(1.0)
    assert signal.score == pytest.approx(0.0)


# --- the dead fan-out, pinned so it cannot rot silently -------------------


def test_no_registered_components_short_circuits_the_cycle(
    analyzer: FitnessAnalyzer,
) -> None:
    """Documents *why* the signal cache is always empty in production.

    ``add_component`` has zero call sites fleet-wide, so both construction
    sites produce an analyzer with no endpoints and every cycle returns
    early. If this test ever starts failing because something began
    registering components, that is a real change and this file's
    characterisation of "no traffic" no longer holds.
    """
    import asyncio

    assert analyzer._component_endpoints == []
    asyncio.run(analyzer._analyze_and_persist())
    assert analyzer._signals == {}
