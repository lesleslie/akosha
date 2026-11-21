"""Tests for the feed stall signal in the Akosha health payload.

A feed that has cycled without ever producing a row is not "warming up".
The Akosha event-ingestion pipeline was severed for roughly six weeks while
``/health`` reported ``status: warming_up`` with ``cycles_total: 3055``,
because the shared aggregator treats "cycled at least once, nothing yet" as
a warm-up regardless of age. These tests pin the local signal that tells
the two apart.
"""

from __future__ import annotations

import pytest

from akosha.mcp.server import (
    FEED_STALL_CYCLES_THRESHOLD,
    apply_stall_status,
    apply_stall_status_to_aggregate,
    feed_is_stalled,
)


class TestFeedIsStalled:
    """``feed_is_stalled`` separates a cold start from a permanent stall."""

    def test_a_fresh_feed_is_not_stalled(self) -> None:
        assert feed_is_stalled(cycles_total=1, entities_count=0) is False

    def test_a_populated_feed_is_not_stalled(self) -> None:
        assert feed_is_stalled(cycles_total=10_000, entities_count=42) is False

    def test_a_long_starved_feed_is_stalled(self) -> None:
        assert feed_is_stalled(cycles_total=3_055, entities_count=0) is True

    def test_the_threshold_is_the_boundary(self) -> None:
        below = FEED_STALL_CYCLES_THRESHOLD - 1
        at = FEED_STALL_CYCLES_THRESHOLD
        assert feed_is_stalled(cycles_total=below, entities_count=0) is False
        assert feed_is_stalled(cycles_total=at, entities_count=0) is True

    def test_zero_cycles_is_not_stalled(self) -> None:
        assert feed_is_stalled(cycles_total=0, entities_count=0) is False

    @pytest.mark.parametrize("entities", [0, 1])
    def test_entities_always_win_over_cycles(self, entities: int) -> None:
        assert feed_is_stalled(cycles_total=99_999, entities_count=entities) is (entities == 0)


class TestStalledStatusOverridesWarmingUp:
    """A stalled feed must report ``degraded``, not ``warming_up``.

    Adding a ``stalled`` boolean next to a ``status`` that still says
    ``warming_up`` changes nothing an operator or an aggregator can act
    on — the status is the field that is read.
    """

    def test_a_stalled_feed_reports_degraded(self) -> None:
        feed = {
            "ok": False,
            "status": "warming_up",
            "reason_codes": ["warming_up_empty_feed"],
            "entities_count": 0,
            "cycles_total": 3_133,
        }
        apply_stall_status(feed)
        assert feed["status"] == "degraded"
        assert "feed_stalled" in feed["reason_codes"]

    def test_a_healthy_feed_is_untouched(self) -> None:
        feed = {
            "ok": True,
            "status": "healthy",
            "reason_codes": [],
            "entities_count": 42,
            "cycles_total": 3_133,
        }
        apply_stall_status(feed)
        assert feed["status"] == "healthy"
        assert "feed_stalled" not in feed["reason_codes"]

    def test_a_warming_feed_below_the_threshold_is_untouched(self) -> None:
        feed = {
            "ok": False,
            "status": "warming_up",
            "reason_codes": ["warming_up_empty_feed"],
            "entities_count": 0,
            "cycles_total": 3,
        }
        apply_stall_status(feed)
        assert feed["status"] == "warming_up"


class TestStalledStatusReachesTheAggregate:
    """The roll-up must not mask a stall behind ``warming_up``.

    The shared aggregator scores "cycled at least once, nothing yet" as
    a warm-up regardless of age, and ``warming_up`` answers 200. So the
    stall signal is worthless unless it also moves the aggregate — the
    field the 503 rule reads.
    """

    @staticmethod
    def _checks(aggregate_status: str = "warming_up") -> dict[str, dict[str, object]]:
        return {
            "knowledge_graph_feed": {
                "ok": False,
                "status": "warming_up",
                "reason_codes": ["warming_up_empty_feed"],
                "entities_count": 0,
                "cycles_total": 3_055,
            },
            "code_graphs_feed": {
                "ok": True,
                "status": "healthy",
                "reason_codes": [],
                "entities_count": 7,
                "cycles_total": 40,
            },
            "_aggregate": {
                "status": aggregate_status,
                "reason_codes": ["warming_up_empty_feed"],
                "data_feeds_ok": True,
                "halflife_seconds": 300,
            },
        }

    def test_a_stalled_feed_degrades_the_aggregate(self) -> None:
        checks = self._checks()
        apply_stall_status(checks["knowledge_graph_feed"])

        apply_stall_status_to_aggregate(checks)

        aggregate = checks["_aggregate"]
        assert aggregate["status"] == "degraded"
        assert "feed_stalled" in aggregate["reason_codes"]

    def test_an_aggregate_with_no_stalled_feed_is_untouched(self) -> None:
        checks = self._checks()
        # No feed carries the stall signal.
        apply_stall_status_to_aggregate(checks)

        aggregate = checks["_aggregate"]
        assert aggregate["status"] == "warming_up"
        assert "feed_stalled" not in aggregate["reason_codes"]

    def test_a_degraded_aggregate_keeps_its_status_and_gains_the_reason(self) -> None:
        checks = self._checks(aggregate_status="degraded")
        apply_stall_status(checks["knowledge_graph_feed"])

        apply_stall_status_to_aggregate(checks)

        aggregate = checks["_aggregate"]
        assert aggregate["status"] == "degraded"
        assert "feed_stalled" in aggregate["reason_codes"]

    def test_the_reason_code_is_not_duplicated(self) -> None:
        checks = self._checks()
        apply_stall_status(checks["knowledge_graph_feed"])
        apply_stall_status_to_aggregate(checks)
        apply_stall_status_to_aggregate(checks)

        assert checks["_aggregate"]["reason_codes"].count("feed_stalled") == 1

    def test_data_feeds_ok_is_not_rewritten(self) -> None:
        """It reports what the aggregator scored; rewriting it would lie."""
        checks = self._checks()
        apply_stall_status(checks["knowledge_graph_feed"])

        apply_stall_status_to_aggregate(checks)

        assert checks["_aggregate"]["data_feeds_ok"] is True

    def test_a_checks_dict_without_an_aggregate_is_untouched(self) -> None:
        checks: dict[str, dict[str, object]] = {
            "knowledge_graph_feed": {"stalled": True, "status": "degraded"},
        }

        result = apply_stall_status_to_aggregate(checks)

        assert result is checks
        assert "_aggregate" not in checks


class TestTheStallSignalIsGatedOnTheProducer:
    """An empty feed whose producer is idle is not a stall.

    The first version judged a feed stalled from ``cycles_total`` and
    ``entities_count`` alone. Two false positives follow from that:

    * The committed default has the event subscriber OFF. The
      conversations table then stays empty while kg-refresh cycles once
      a minute -- so after 100 cycles a HEALTHY, correctly-configured,
      idle system reported "degraded" and /health answered 503.
    * The same applies to the OTLP receiver, which always runs: on a box
      where nobody sends spans, the local-traces feed cycles to
      thousands with zero rows and nothing wrong anywhere.

    The gate is the producer's own work: if the producer received input
    that should have landed and nothing landed, that is a stall. If it
    received nothing, the feed is empty because there was nothing to
    ingest.
    """

    def test_an_idle_producer_leaves_the_feed_alone(self) -> None:
        feed = {
            "ok": False,
            "status": "warming_up",
            "reason_codes": ["warming_up_empty_feed"],
            "entities_count": 0,
            "cycles_total": 3_055,
        }

        apply_stall_status(feed, producer_active=False)

        assert feed["status"] == "warming_up"
        assert "stalled" not in feed
        assert "feed_stalled" not in feed["reason_codes"]

    def test_a_producer_that_did_work_stalls_the_feed(self) -> None:
        feed = {
            "ok": False,
            "status": "warming_up",
            "reason_codes": ["warming_up_empty_feed"],
            "entities_count": 0,
            "cycles_total": 3_055,
        }

        apply_stall_status(feed, producer_active=True)

        assert feed["status"] == "degraded"
        assert feed["stalled"] is True
        assert "feed_stalled" in feed["reason_codes"]

    def test_the_default_is_to_apply_the_rule(self) -> None:
        """No producer signal means no producer context to excuse the feed."""
        feed = {
            "ok": False,
            "status": "warming_up",
            "reason_codes": [],
            "entities_count": 0,
            "cycles_total": 3_055,
        }

        apply_stall_status(feed)

        assert feed["status"] == "degraded"


class TestTheProducerGateIsWiredToRealWork:
    """The gate must read the producer's WORK, not a constant.

    The knowledge-graph feed's producer is the event subscriber, whose
    work is the entries that SHOULD have produced a row. The
    local-traces feed's producer is the OTLP receiver, whose work is
    the batches it ingested.

    The knowledge-graph gate reads ``entries_candidate``, not
    ``entries_total``: the hook bridge publishes six channels and only
    one of them is indexed, so gating on the raw read count would
    report a healthy pipeline that is correctly dropping traffic as
    stalled.
    """

    @staticmethod
    def _server_source() -> str:
        from pathlib import Path

        return (
            Path(__file__).resolve().parents[2] / "akosha" / "mcp" / "server.py"
        ).read_text()

    def test_the_knowledge_graph_gate_reads_the_subscribers_entries(self) -> None:
        import ast

        tree = ast.parse(self._server_source())
        gates: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", None) != "apply_stall_status":
                continue
            for keyword in node.keywords:
                if keyword.arg == "producer_active":
                    gates.append(ast.unparse(keyword.value))
        assert any("entries_candidate" in gate for gate in gates), (
            "the knowledge-graph feed's stall gate must read the "
            "subscriber's candidate-entry count; gates found: " f"{gates}"
        )

    def test_the_local_traces_gate_reads_the_receivers_batches(self) -> None:
        import ast

        tree = ast.parse(self._server_source())
        gates: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", None) != "apply_stall_status":
                continue
            for keyword in node.keywords:
                if keyword.arg == "producer_active":
                    gates.append(ast.unparse(keyword.value))
        assert any("batches_total" in gate for gate in gates), (
            "the local-traces feed's stall gate must read the OTLP "
            f"receiver's batch count; gates found: {gates}"
        )
