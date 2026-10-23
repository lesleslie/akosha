"""Superseded by R-6. See the two files this points at.

This module characterised ``FitnessAnalyzer._compute_signal`` *before* read-path
R-6 replaced the per-component HTTP fan-out with ``SpanQuery.summary``. R-6
deleted ``_compute_signal`` along with ``_collect_traces`` and
``_fetch_traces_from_component`` — all three had no production callers once the
analyzer started reading the substrate, and shipping them would have left an
orphan behind, which ``docs/decisions/wire-up-contract.md`` treats as an
undelivered feature.

The characterisation did its job. Of the three hazards it named:

- *Silence scoring as perfect* — **occurred**, fixed, pinned in
  ``test_fitness_analyzer_rewired.py::test_declared_pair_with_no_spans_is_still_reported``.
- *The failure-rate denominator* — **occurred**, as an untested
  ``outcome IN ('success', 'error')`` predicate in ``SpanQuery.summary``. Fixed
  by removing the predicate, pinned in
  ``test_fitness_analyzer_rewired.py::test_non_scored_outcomes_stay_in_the_denominator``.
- *The p99 index* — **not** a bug; a deliberate behaviour change, now pinned in
  ``test_fitness_p99_semantics.py``.

Every assertion worth keeping now lives in one of those two files. This module
is intentionally empty of tests: keeping assertions against a deleted method
would fail the suite, and deleting the file without a pointer would lose the
reasoning that caught two real defects.
"""

from __future__ import annotations

# Intentionally contains no tests. See the module docstring.
