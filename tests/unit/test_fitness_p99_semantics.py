"""R-6 behaviour change: ``p99_latency_ms`` is now a real percentile.

This file replaces the characterisation tests that pinned
``FitnessAnalyzer._compute_signal``, which R-6 deleted along with the HTTP
fan-out it fed. Those tests did their job — two of the three hazards they named
actually occurred, and are now pinned in ``test_fitness_analyzer_rewired.py``:

- **Silence scoring as perfect.** ``_compute_signal`` returned the default
  signal (score 0.0) for empty input. The first R-6 implementation computed
  ``1.0 - failure_rate`` unconditionally, so a declared-but-silent pair scored
  **1.0** — reporting a broken producer as healthy. Fixed and pinned.
- **The failure-rate denominator.** An ``outcome IN ('success', 'error')``
  predicate in ``SpanQuery.summary`` dropped ``timeout`` / ``cancelled`` / NULL
  rows from ``span_count`` instead of counting them as non-failures. The
  predicate was untested and is now removed. Fixed and pinned.

The third hazard was the p99 index, and that one was **not** a bug to fix — it
was a deliberate change, recorded here.

``_compute_signal`` computed ``sorted_durations[min(int(n * 0.99), n - 1)]``,
which equals ``n - 1`` for every ``n <= 100``; below 101 samples "p99 latency"
was the *maximum*. The substrate uses ``PERCENTILE_CONT(0.99)``, which
interpolates. Same ten spans, different reported number:

    old: 10.0   (the max)
    new: 9.91   (a genuine 99th percentile)

This is a real change to a reported metric and downstream consumers may notice
it. It was made deliberately rather than discovered afterwards, which is what
the original characterisation file asked for.
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
    hs = HotStore(database_path=tmp_path / "p99.duckdb", embedding_dim=384)
    await hs.initialize()
    hs.conn.execute(SPANS_SUBSTRATE_DDL)
    return hs


def _insert_latencies(hs: HotStore, latencies: list[float]) -> None:
    for i, latency in enumerate(latencies):
        hs.conn.execute(
            "INSERT INTO spans_substrate (span_id, trace_id, service_name, "
            "service_namespace, span_name, start_time_unix_nano, "
            "received_at_unix_nano, outcome, selector, task_class, latency_ms, "
            "attributes, resource) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                f"s{i}",
                f"t{i}",
                "mahavishnu",
                "bodai",
                "n",
                1,
                BASE_RECEIVED,
                "success",
                "least_loaded",
                "code_generation",
                latency,
                json.dumps({}),
                json.dumps({}),
            ],
        )


async def _run(store: HotStore) -> float:
    analyzer = FitnessAnalyzer(
        declared_pairs=[("code_generation", "least_loaded")],
        span_query=SpanQuery(store),
        window_start_unix_nano=WINDOW_START,
    )
    await analyzer._analyze_and_persist()
    return analyzer._signals["code_generation"]["least_loaded"].p99_latency_ms


@pytest.mark.asyncio
async def test_p99_is_not_the_maximum_for_small_samples(store: HotStore) -> None:
    """Ten spans with latencies 1..10 report ~9.91, not 10.0.

    This is the change. The old index formula returned the maximum for any
    sample count at or below 100, so the same data reported 10.0.
    """
    _insert_latencies(store, [float(i) for i in range(1, 11)])

    assert await _run(store) == pytest.approx(9.91, abs=0.01)


@pytest.mark.asyncio
async def test_p99_is_above_the_median_for_a_skewed_window(
    store: HotStore,
) -> None:
    """A genuine tail percentile sits above the median of a skewed sample.

    The old max-based value would also satisfy this, so the test exists to
    pin that the change did not collapse the metric into a median.
    """
    _insert_latencies(store, [10.0] * 95 + [500.0] * 5)

    assert await _run(store) > 10.0


@pytest.mark.asyncio
async def test_p99_ignores_rows_without_a_latency(store: HotStore) -> None:
    """NULL latencies are excluded from the percentile but still counted as rows.

    ``PERCENTILE_CONT`` skips NULLs, so ``span_count`` and ``latency_p99_ms``
    cover different populations on purpose — see read-path spec §4.2.
    """
    _insert_latencies(store, [10.0, 20.0, 30.0])
    store.conn.execute(
        "INSERT INTO spans_substrate (span_id, trace_id, service_name, "
        "service_namespace, span_name, start_time_unix_nano, "
        "received_at_unix_nano, outcome, selector, task_class, latency_ms, "
        "attributes, resource) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            "null-latency",
            "t-null",
            "mahavishnu",
            "bodai",
            "n",
            1,
            BASE_RECEIVED,
            "success",
            "least_loaded",
            "code_generation",
            None,
            json.dumps({}),
            json.dumps({}),
        ],
    )

    analyzer = FitnessAnalyzer(
        declared_pairs=[("code_generation", "least_loaded")],
        span_query=SpanQuery(store),
        window_start_unix_nano=WINDOW_START,
    )
    await analyzer._analyze_and_persist()
    signal = analyzer._signals["code_generation"]["least_loaded"]

    assert signal.samples == 4, "the NULL-latency row still counts as a sample"
    # Interpolated over the three non-NULL latencies only:
    #   index 0.99 * (3 - 1) = 1.98 -> 20.0 + 0.98 * (30.0 - 20.0) = 29.8
    # Had the NULL been coerced to 0.0 it would sort to the bottom and drag the
    # percentile far lower, so this value is what proves it was excluded.
    assert signal.p99_latency_ms == pytest.approx(29.8, abs=0.01)
