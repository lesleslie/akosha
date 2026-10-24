"""``reset_all_metrics()`` must zero the metrics WITHOUT amputating them.

The reset used to replace the shared ``CollectorRegistry`` wholesale and
re-create only the metrics defined in ``prometheus_metrics.py`` itself.
Every other module that captured the registry at import time --
``otlp_metrics.py``, ``resource_validator.py``, ``heartbeat_watcher.py`` --
was left holding a discarded registry, and its collectors stopped appearing
in the ``/metrics`` exposition entirely. The receiver's whole metric
surface vanished from ``/metrics`` after a single reset.

These tests pin the invariant that closes that trap: **a reset may change a
metric's VALUE, never its PRESENCE.** A module written tomorrow cannot
reintroduce the bug by forgetting to re-register itself, because the
completeness test below fails the moment its metrics go missing.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry

from akosha.observability import otlp_metrics
from akosha.observability.prometheus_metrics import (
    get_metrics_registry,
    reset_all_metrics,
)


def _families(registry: CollectorRegistry) -> set[str]:
    """The names of every metric family currently in ``registry``."""
    return {family.name for family in registry.collect()}


def _samples(registry: CollectorRegistry, family: str) -> dict[str, float]:
    """The samples of one family, keyed by sample name.

    The label set is folded into the key so two children of the same
    labeled metric do not collide.
    """
    found: dict[str, float] = {}
    for metric in registry.collect():
        if metric.name != family:
            continue
        for sample in metric.samples:
            key = sample.name + repr(sorted(sample.labels.items()))
            found[key] = float(sample.value)
    return found


# ── the completeness invariant ──────────────────────────────────────────


def test_a_reset_keeps_every_metric_family() -> None:
    """No metric family may disappear across a reset.

    This is the test that makes the trap unwearable: if a new module binds
    metrics to the shared registry and a reset drops them, the family set
    shrinks and this fails.
    """
    registry = get_metrics_registry()
    before = _families(registry)
    assert before, "the shared registry is empty; the test proves nothing"

    reset_all_metrics()

    after = _families(registry)
    missing = before - after
    assert not missing, f"a reset dropped metric families: {sorted(missing)}"
    assert after == before, (
        f"a reset changed the family set: added {sorted(after - before)}, "
        f"removed {sorted(before - after)}"
    )


def test_a_reset_does_not_replace_the_registry_object() -> None:
    """The registry's identity must be stable.

    Everything else follows from this. A module that captured the registry
    at import time keeps working across any number of resets because the
    object it captured is still the live one.
    """
    before = get_metrics_registry()
    reset_all_metrics()
    after = get_metrics_registry()
    assert before is after, "the reset replaced the shared registry object"


# ── values are actually zeroed ──────────────────────────────────────────


def test_a_reset_zeroes_an_unlabeled_counter() -> None:
    """An unlabeled counter goes to zero and stays in the registry."""
    registry = get_metrics_registry()
    otlp_metrics.spans_received_total.inc(5)
    assert _samples(registry, "akosha_otlp_spans_received")[
        "akosha_otlp_spans_received_total[]"
    ] == 5.0

    reset_all_metrics()

    assert _samples(registry, "akosha_otlp_spans_received")[
        "akosha_otlp_spans_received_total[]"
    ] == 0.0


def test_a_reset_zeroes_a_gauge() -> None:
    """A gauge is set to zero, not left at its last value."""
    registry = get_metrics_registry()
    otlp_metrics.sweep_last_completed_unix_nano.set(12345)

    reset_all_metrics()

    assert _samples(registry, "akosha_otlp_sweep_last_completed_unix_nano")[
        "akosha_otlp_sweep_last_completed_unix_nano[]"
    ] == 0.0


def test_a_reset_zeroes_a_histogram() -> None:
    """A histogram's count and sum both go back to zero.

    A histogram keeps its state in per-bucket ``Value`` objects plus a
    separate ``_sum`` ``Value``, so it needs its own zeroing path -- and
    the count is derived from the buckets, so zeroing the buckets is what
    resets the count.
    """
    registry = get_metrics_registry()
    otlp_metrics.sweep_duration_seconds.observe(2.5)

    reset_all_metrics()

    samples = _samples(registry, "akosha_otlp_sweep_duration_seconds")
    assert samples["akosha_otlp_sweep_duration_seconds_count[]"] == 0.0
    assert samples["akosha_otlp_sweep_duration_seconds_sum[]"] == 0.0


def test_a_reset_drops_labelled_children_which_restart_at_zero() -> None:
    """A labeled metric's children are dropped, and the next child is zero.

    Dropping the labelsets is the only complete reset for a labeled
    metric: there is no "value" on the parent to zero, only children.
    """
    registry = get_metrics_registry()
    otlp_metrics.spans_rejected_total.labels(field="service_namespace").inc(3)

    reset_all_metrics()

    assert "akosha_otlp_spans_rejected_total[('field', 'service_namespace')]" not in _samples(
        registry, "akosha_otlp_spans_rejected"
    )

    otlp_metrics.spans_rejected_total.labels(field="service_namespace").inc(1)
    assert _samples(registry, "akosha_otlp_spans_rejected")[
        "akosha_otlp_spans_rejected_total[('field', 'service_namespace')]"
    ] == 1.0


# ── the metrics stay live ───────────────────────────────────────────────


def test_the_metrics_stay_writable_after_a_reset() -> None:
    """A reset must not leave the collectors pointing at a dead registry.

    This is the failure the old implementation actually produced: the
    collector object survived, so ``.inc()`` kept working and kept
    counting -- into a registry that nothing was reading any more.
    """
    registry = get_metrics_registry()
    reset_all_metrics()

    otlp_metrics.spans_inserted_total.inc(2)

    assert _samples(registry, "akosha_otlp_spans_inserted")[
        "akosha_otlp_spans_inserted_total[]"
    ] == 2.0
