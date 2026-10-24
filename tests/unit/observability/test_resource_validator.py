"""PR-A6 resource-attribute validator (spec v7 §13.2).

Four behaviours the spec names, one test each:

* rate-based spike threshold
* allowlist honored
* start-up-burst rejection
* sustained-pressure path

The clock is injected, so the rolling window and the 24h sustained window
are exercised exactly rather than approximately. Nothing here sleeps.
"""

from __future__ import annotations

from typing import Any

import pytest

from akosha.observability.resource_validator import (
    ALLOWED_NAMESPACES,
    ROLLING_WINDOW_SECONDS,
    SPIKE_THRESHOLD,
    STARTUP_GRACE_SECONDS,
    SUSTAINED_THRESHOLD_HR,
    SUSTAINED_WINDOW_SECONDS,
    ResourceAttributeValidator,
    ResourceValidationResult,
)


class FakeClock:
    """A monotonic clock the test drives by hand."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _row(
    namespace: str | None,
    *,
    service_name: str = "mahavishnu",
) -> dict[str, Any]:
    """A substrate row shaped like the receiver's proto mapping produces."""
    row: dict[str, Any] = {
        "service_name": service_name,
        "span_name": "unit-test-span",
        "start_time_unix_nano": 1_000_000_000,
    }
    if namespace is not None:
        row["service_namespace"] = namespace
    return row


def _validator(clock: FakeClock, **kwargs: Any) -> ResourceAttributeValidator:
    """A validator with the start-up grace switched off unless asked for."""
    kwargs.setdefault("startup_grace_seconds", 0)
    return ResourceAttributeValidator(clock=clock, **kwargs)


def _drive(
    validator: ResourceAttributeValidator,
    namespaces: list[str],
    *,
    producer: str = "svc",
) -> list[ResourceValidationResult]:
    """Feed ``namespaces`` to the validator, one row each, in order."""
    return [validator.validate(_row(ns, service_name=producer)) for ns in namespaces]


# ── allowlist honored ────────────────────────────────────────────────────


def test_the_allowlist_carries_the_current_fleet_namespaces() -> None:
    """The spec's §13.2 bridge set is stale — PR-A7 already shipped.

    ``bodai`` is every MCP server (mcp-common 0.31.2+ defaults
    ``service_namespace`` to it) and ``cli-tools`` is the Claude Code / Codex
    / Qwen fleet from ``~/.zshrc``. ``mcp-ecosystem`` is the pre-flip default
    and is therefore *drift* now — keeping it allowed would mask a producer
    that regressed.
    """
    assert "bodai" in ALLOWED_NAMESPACES
    assert "cli-tools" in ALLOWED_NAMESPACES
    assert "mcp-ecosystem" not in ALLOWED_NAMESPACES


def test_an_allowlisted_namespace_is_accepted() -> None:
    validator = _validator(FakeClock())
    result = validator.validate(_row("bodai"))

    assert result.accepted is True
    assert result.reason is None
    assert result.seen_namespace == "bodai"
    assert result.allowed_namespaces == ALLOWED_NAMESPACES
    assert result.alert_class is None
    assert result.alert_raised is False
    assert validator.drift_events_total == 0


def test_a_namespace_outside_the_allowlist_is_rejected() -> None:
    validator = _validator(FakeClock())
    result = validator.validate(_row("mcp-ecosystem"))

    assert result.accepted is False
    assert result.reason == "namespace_not_allowed"
    assert result.seen_namespace == "mcp-ecosystem"
    assert result.allowed_namespaces == ALLOWED_NAMESPACES
    assert validator.drift_events_total == 1


def test_the_rejection_names_the_offending_value_and_the_allowed_set() -> None:
    """A rejection an operator cannot act on is a rejection in name only.

    "namespace drift" without the offending value leaves the operator to
    guess which producer to go and fix.
    """
    validator = _validator(FakeClock())
    result = validator.validate(_row("rogue-namespace"))

    assert "rogue-namespace" in result.message
    for allowed in ALLOWED_NAMESPACES:
        assert allowed in result.message


def test_a_span_without_a_namespace_is_rejected_as_missing() -> None:
    """proto3 cannot tell an unset string from an empty one — both are "".

    Both are "no namespace", which is a different defect from "the wrong
    namespace" and has to be distinguishable in the DLQ.
    """
    validator = _validator(FakeClock())

    absent = validator.validate(_row(None))
    assert absent.accepted is False
    assert absent.reason == "namespace_missing"
    assert absent.seen_namespace is None

    empty = validator.validate(_row(""))
    assert empty.accepted is False
    assert empty.reason == "namespace_missing"


def test_a_rejection_is_attributed_to_its_producer() -> None:
    validator = _validator(FakeClock())
    result = validator.validate(_row("rogue", service_name="crackerjack"))

    assert result.producer == "crackerjack"


# ── rate-based spike threshold ───────────────────────────────────────────


def test_a_spike_needs_more_distinct_values_than_the_threshold() -> None:
    """More than SPIKE_THRESHOLD distinct values in the rolling window.

    At the threshold is not over it: three distinct drifted namespaces is
    not yet a cardinality explosion.
    """
    clock = FakeClock()
    validator = _validator(clock)

    for index in range(SPIKE_THRESHOLD):
        result = validator.validate(_row(f"ns-{index}", service_name="svc"))
        assert result.alert_class is None
        assert result.alert_raised is False

    result = validator.validate(_row("ns-3", service_name="svc"))
    assert result.alert_class == "P1_spike"
    assert result.alert_raised is True
    assert validator.spike_alerts_total == 1


def test_the_same_number_of_values_spread_out_is_not_a_spike() -> None:
    """Rate, not count. Ten values at one per window is not an explosion."""
    clock = FakeClock()
    validator = _validator(clock)

    for index in range(10):
        validator.validate(_row(f"ns-{index}", service_name="svc"))
        clock.advance(ROLLING_WINDOW_SECONDS)

    assert validator.spike_alerts_total == 0
    assert validator.sustained_alerts_total == 0


def test_a_repeated_value_is_not_a_cardinality_explosion() -> None:
    """One bad namespace repeated is one bad value, not twenty.

    The rejection counter carries the volume; the drift tracks carry the
    cardinality. Counting the twenty rejections as twenty drift values
    would cross the daily threshold and report a one-off burst as sustained
    pressure.
    """
    clock = FakeClock()
    validator = _validator(clock)

    results = _drive(validator, ["same-ns"] * 20)
    result = results[-1]

    assert result.accepted is False
    assert result.alert_class is None
    assert validator.spike_alerts_total == 0
    assert validator.sustained_alerts_total == 0
    assert validator.drift_events_total == 20


def test_the_spike_alert_fires_once_per_window() -> None:
    """The spec: "Alerts once per window"."""
    clock = FakeClock()
    validator = _validator(clock)

    results = _drive(validator, [f"ns-{index}" for index in range(4)])
    assert results[-1].alert_raised is True

    # A fifth value inside the same window is still a spike, but the
    # notification already went out.
    result = validator.validate(_row("ns-4", service_name="svc"))
    assert result.alert_class == "P1_spike"
    assert result.alert_raised is False
    assert validator.spike_alerts_total == 1

    # The next window notifies again.
    clock.advance(ROLLING_WINDOW_SECONDS + 1)
    results = _drive(validator, [f"next-{index}" for index in range(4)])
    assert results[-1].alert_class == "P1_spike"
    assert results[-1].alert_raised is True
    assert validator.spike_alerts_total == 2


def test_a_spike_is_per_producer() -> None:
    """Four namespaces spread over four producers is four producers, not one
    producer with four namespaces."""
    clock = FakeClock()
    validator = _validator(clock)

    for index in range(4):
        result = validator.validate(_row(f"ns-{index}", service_name=f"svc-{index}"))
        assert result.alert_class is None

    assert validator.spike_alerts_total == 0


# ── start-up-burst rejection ─────────────────────────────────────────────


def test_a_startup_burst_does_not_page() -> None:
    """The receiver's own start-up is a legitimate burst.

    Every producer re-announces itself within seconds of the receiver
    binding, and a validator with no history reads that as a sudden
    explosion of new values. The spec's v2 rule was "misclassified by
    startup bursts"; the rate-based rule must not repeat that.

    The spans are still *rejected* by the allowlist — the grace suppresses
    the alarm, not the rejection.
    """
    clock = FakeClock()
    validator = ResourceAttributeValidator(clock=clock)

    for index in range(15):
        result = validator.validate(_row(f"ns-{index}", service_name=f"svc-{index}"))

        assert result.accepted is False
        assert result.reason == "namespace_not_allowed"
        assert result.alert_class is None
        assert result.alert_raised is False

    assert validator.spike_alerts_total == 0
    assert validator.sustained_alerts_total == 0
    # The burst is still visible in the raw counters.
    assert validator.drift_events_total == 15


def test_a_single_producer_bursting_during_the_grace_does_not_page() -> None:
    """Even one producer cannot page the operator out of the grace window.

    A misconfigured producer that emits a handful of different namespaces in
    the first seconds after the receiver binds would page on every restart.
    """
    clock = FakeClock()
    validator = ResourceAttributeValidator(clock=clock)

    for index in range(6):
        result = validator.validate(_row(f"ns-{index}", service_name="svc"))
        assert result.accepted is False
        assert result.alert_raised is False

    assert validator.spike_alerts_total == 0


def test_the_grace_expires() -> None:
    """After the grace, the same shape of burst does page."""
    clock = FakeClock()
    validator = ResourceAttributeValidator(clock=clock)

    for index in range(6):
        validator.validate(_row(f"ns-{index}", service_name="svc"))

    clock.advance(STARTUP_GRACE_SECONDS + 1)
    results = _drive(validator, [f"late-{index}" for index in range(4)])

    assert results[-1].alert_class == "P1_spike"
    assert results[-1].alert_raised is True
    assert validator.spike_alerts_total == 1


def test_a_burst_of_allowlisted_spans_is_accepted_and_silent() -> None:
    """The ordinary case: the fleet comes up, everything is allowlisted."""
    clock = FakeClock()
    validator = ResourceAttributeValidator(clock=clock)

    for index in range(50):
        result = validator.validate(_row("bodai", service_name=f"svc-{index}"))
        assert result.accepted is True

    assert validator.drift_events_total == 0
    assert validator.spike_alerts_total == 0
    assert validator.sustained_alerts_total == 0


# ── sustained-pressure path ──────────────────────────────────────────────


def test_sustained_pressure_is_p2_and_not_a_spike() -> None:
    """A slowly growing set is caught by the sustained track.

    v2's count-per-hour rule was blind to slow drift; the P2 track is the
    fix. Here the set grows by one value every 90 minutes for a day — no
    5-minute window ever holds more than one, so it is never a P1, but the
    24h rate crosses the sustained threshold.
    """
    clock = FakeClock()
    validator = _validator(clock)

    results: list[ResourceValidationResult] = []
    for index in range(14):
        results.append(validator.validate(_row(f"ns-{index}", service_name="svc")))
        clock.advance(90 * 60)

    final = results[-1]
    assert final.accepted is False
    assert final.alert_class == "P2_sustained"
    # Exactly one of the fourteen events notifies: the one that crossed the
    # threshold. The rest are the same condition, still open.
    assert [r.alert_raised for r in results].count(True) == 1
    assert validator.spike_alerts_total == 0
    assert validator.sustained_alerts_total == 1
    assert final.sustained_drift_values_per_hour > SUSTAINED_THRESHOLD_HR


def test_the_sustained_alert_fires_once_per_day() -> None:
    """The spec: "Alerts daily"."""
    clock = FakeClock()
    validator = _validator(clock)

    for index in range(16):
        validator.validate(_row(f"ns-{index}", service_name="svc"))
        clock.advance(90 * 60)

    assert validator.sustained_alerts_total == 1


def test_sustained_pressure_is_measured_over_the_24h_window() -> None:
    """Events that fall out of the 24h window stop counting.

    A producer that drifted all day yesterday is not still under sustained
    pressure today.
    """
    clock = FakeClock()
    validator = _validator(clock)

    for index in range(14):
        validator.validate(_row(f"ns-{index}", service_name="svc"))
        clock.advance(90 * 60)
    assert validator.sustained_alerts_total == 1

    # Push the whole history out of the 24h window.
    clock.advance(SUSTAINED_WINDOW_SECONDS)
    result = validator.validate(_row("ns-late", service_name="svc"))

    assert result.sustained_drift_values_per_hour <= SUSTAINED_THRESHOLD_HR
    assert result.alert_class is None
    assert validator.sustained_alerts_total == 1


def test_a_one_off_spike_and_sustained_pressure_are_classified_differently() -> None:
    """The two tracks answer different questions and must not be confused."""
    spike_clock = FakeClock()
    spike_validator = _validator(spike_clock)
    spike_results = _drive(spike_validator, [f"ns-{index}" for index in range(4)])

    sustained_clock = FakeClock()
    sustained_validator = _validator(sustained_clock)
    sustained_results: list[ResourceValidationResult] = []
    for index in range(14):
        sustained_results.append(
            sustained_validator.validate(_row(f"ns-{index}", service_name="svc"))
        )
        sustained_clock.advance(90 * 60)

    assert spike_results[-1].alert_class == "P1_spike"
    assert spike_validator.sustained_alerts_total == 0
    assert sustained_results[-1].alert_class == "P2_sustained"
    assert sustained_validator.spike_alerts_total == 0


# ── plumbing ─────────────────────────────────────────────────────────────


def test_reset_clears_the_history() -> None:
    clock = FakeClock()
    validator = _validator(clock)

    for index in range(4):
        validator.validate(_row(f"ns-{index}", service_name="svc"))
    assert validator.spike_alerts_total == 1

    validator.reset()
    results = _drive(validator, [f"after-{index}" for index in range(3)])

    # The history is gone: three values in the window is not a spike yet.
    assert results[-1].alert_class is None
    assert validator.spike_alerts_total == 1


@pytest.mark.parametrize(
    "row",
    [
        {},
        {"service_namespace": None},
        {"service_namespace": 42},
        {"service_name": None, "service_namespace": "bodai"},
    ],
)
def test_malformed_rows_do_not_raise(row: dict[str, Any]) -> None:
    """The validator runs on the ingest hot path; it must not throw."""
    validator = _validator(FakeClock())
    result = validator.validate(row)
    assert isinstance(result.accepted, bool)
