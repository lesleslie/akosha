"""End-to-end health aggregator contract for the Akosha ``/health`` endpoint.

Implements plan §5 Phase 4 task 1 — the per-repo integration test that
pins the contract between ``mcp_common.health.aggregator.aggregate_feed_states``
and the Akosha ``/health`` HTTP route.

Contract under test
-------------------

* **Aggregator verdict drives the HTTP code**: 200 for healthy/warming_up,
  503 for degraded/failed. This is what the launchd wrapper script
  (and any orchestrator probing the endpoint) sees — the body carries
  the per-feed detail but the HTTP code is the operator-facing signal.

* **Time-bounded decay**: errors within ``halflife_seconds`` flip the
  per-feed status to ``degraded``; errors outside the halflife decay back
  to ``healthy``. The default halflife is 300s (5 minutes).

* **HNSW hardening**: a feed whose producer reports
  ``ingester_running=True`` but ``cycles_total == 0`` is
  ``degraded`` with ``feed_never_populated``, NOT ``warming_up``.
  This is the fix for the HNSW-on-DuckDB bug where a producer
  failing on its first cycle was masked as healthy.

* **Reason codes populate**: every non-healthy feed has at least one
  :class:`mcp_common.health.feed.ReasonCode` in the per-feed
  ``reason_codes`` array and at the top-level ``_aggregate.reason_codes``.

* **All four mandatory wire fields populate per feed**:
  ``feed_entities_count``, ``feed_last_updated_timestamp``,
  ``cycles_total``, ``errors_total``.

These tests use the public ``set_health_probe`` swap pattern so they
don't have to spin up the full lifespan. The aggregator itself is
exercised in mcp-common's unit suite; this file pins how Akosha
*consumes* the aggregator's output through ``/health``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.testclient import TestClient

from akosha.mcp.server import create_app, set_health_probe
from mcp_common.health.feed import HealthFeedState, StatusValue
from mcp_common.health.aggregator import aggregate_feed_states


@pytest.fixture(autouse=True)
def _reset_probe() -> None:
    """Each test starts and ends with no probe registered (test isolation)."""
    set_health_probe(None)
    yield
    set_health_probe(None)


@pytest.fixture
def http_client() -> TestClient:
    """Wrap the FastMCP app for HTTP testing without spinning up the lifespan."""
    return TestClient(create_app().http_app())


def _probe_returning(per_feed: dict[str, dict[str, Any]]) -> Any:
    """Build a probe that returns ``per_feed`` + the aggregator verdict.

    Each entry of ``per_feed`` becomes a HealthFeedState input; the
    aggregator computes the verdict and the probe stitches the result
    into the same dict shape the production probe emits. This lets the
    integration test exercise the route handler + aggregator coupling
    without driving the lifespan.
    """

    async def probe() -> dict[str, dict[str, Any]]:
        states = {
            name: HealthFeedState(
                entities_count=entry.get("entities_count", 0),
                last_updated_timestamp=entry.get("last_updated_timestamp"),
                cycles_total=entry.get("cycles_total", 0),
                errors_total=entry.get("errors_total", 0),
                last_error_at=entry.get("last_error_at"),
                ingester_running=entry.get("ingester_running", False),
            )
            for name, entry in per_feed.items()
        }
        snap = aggregate_feed_states(states)

        checks: dict[str, dict[str, Any]] = {}
        for name, entry in per_feed.items():
            verdict = snap["checks"][name]
            checks[name] = {
                "ok": verdict["healthy"],
                "status": verdict["status"].value,
                "reason_codes": [c.value for c in verdict["reason_codes"]],
                "feed_entities_count": entry.get("entities_count", 0),
                "feed_last_updated_timestamp": entry.get("last_updated_timestamp"),
                "cycles_total": entry.get("cycles_total", 0),
                "errors_total": entry.get("errors_total", 0),
            }

        # Infrastructure checks all-ok so the route handler's infra-failure
        # gate doesn't interfere with these tests.
        checks["hot_store"] = {"ok": True}
        checks["embeddings"] = {"ok": True}
        checks["cold_storage"] = {"ok": True}

        checks["_aggregate"] = {
            "status": snap["status"].value,
            "reason_codes": [c.value for c in snap["reason_codes"]],
            "data_feeds_ok": all(s["healthy"] for s in snap["checks"].values()),
            "halflife_seconds": 300,
        }
        return checks

    return probe


# ---------------------------------------------------------------------------
# Plan §5 Phase 4 task 1 — required assertions
# ---------------------------------------------------------------------------


def test_akosha_health_returns_200_when_errors_outside_halflife(
    http_client: TestClient,
) -> None:
    """REQ-002 + REQ-007: errors aged past halflife → healthy → 200.

    Pin the integration contract test from plan §5:
    ``/health`` returns 200 when the only data-feed error is older than
    ``HEALTH_FEED_HALFLIFE_SECONDS``. The aggregator's time-bounded
    decay is the mechanism; this test pins it end-to-end through the
    HTTP route.
    """
    import time

    probe = _probe_returning(
        {
            "local_traces": {
                "entities_count": 100,
                "cycles_total": 10,
                "errors_total": 1,
                "last_error_at": time.time() - 600.0,  # well outside 300s halflife
                "ingester_running": True,
            }
        }
    )
    set_health_probe(probe)

    response = http_client.get("/health")
    body = response.json()

    # 200 because the only error is outside the halflife → healthy.
    assert response.status_code == 200
    feed = body["checks"]["local_traces"]
    assert feed["status"] == "healthy"
    assert feed["ok"] is True
    # The aggregator's reason code for "error decayed out" surfaces.
    assert "error_outside_halflife" in feed["reason_codes"]


def test_akosha_health_returns_503_when_recent_error(
    http_client: TestClient,
) -> None:
    """REQ-007: error inside halflife → degraded → 503.

    The mirror of the previous test. A fresh error inside the halflife
    window escalates the feed to ``degraded`` and the route handler
    returns 503 so operators see the regression immediately.
    """
    import time

    probe = _probe_returning(
        {
            "local_traces": {
                "entities_count": 100,
                "cycles_total": 10,
                "errors_total": 1,
                "last_error_at": time.time() - 5.0,  # inside 300s halflife
                "ingester_running": True,
            }
        }
    )
    set_health_probe(probe)

    response = http_client.get("/health")
    body = response.json()

    assert response.status_code == 503
    feed = body["checks"]["local_traces"]
    assert feed["status"] == "degraded"
    assert feed["ok"] is False
    assert "error_within_halflife" in feed["reason_codes"]


def test_akosha_health_returns_200_during_warming_up(
    http_client: TestClient,
) -> None:
    """Phase 4 contract: warming_up is NOT a 503 — the launchd wrapper
    relies on 200 during normal startup.

    A running producer that has cycled at least once but has no data
    yet (warming_up) returns 200 — operators see the per-feed
    ``status: warming_up`` + ``reason_codes`` for nuance, but the HTTP
    code stays 200 so the launchd wrapper doesn't take down the
    service during a few-second warm-up window.
    """
    probe = _probe_returning(
        {
            "local_traces": {
                "entities_count": 0,
                "cycles_total": 1,
                "errors_total": 0,
                "ingester_running": True,
            }
        }
    )
    set_health_probe(probe)

    response = http_client.get("/health")
    body = response.json()

    assert response.status_code == 200
    feed = body["checks"]["local_traces"]
    assert feed["status"] == "warming_up"
    assert feed["ok"] is False  # warming_up is not "healthy" per strict contract
    assert "warming_up_empty_feed" in feed["reason_codes"]


def test_akosha_health_hnsw_hardening_never_cycled_degraded(
    http_client: TestClient,
) -> None:
    """Plan §5 task 6: producer alive but never cycled → degraded (NOT warming_up).

    Regression test for the HNSW-on-DuckDB bug — when the very first
    ingest attempt raises (e.g. HNSW index creation fails), the producer
    reports ``cycles_total=0`` and ``ingester_running=True``. The aggregator
    must surface this as DEGRADED with ``feed_never_populated`` so operators
    see a broken-before-first-success producer.
    """
    probe = _probe_returning(
        {
            "code_graphs": {
                "entities_count": 0,
                "cycles_total": 0,  # ← never completed a single cycle
                "errors_total": 0,
                "ingester_running": True,
            }
        }
    )
    set_health_probe(probe)

    response = http_client.get("/health")
    body = response.json()

    # HNSW hardening: the route returns 503 because the aggregator says
    # the producer is broken-before-first-success.
    assert response.status_code == 503
    feed = body["checks"]["code_graphs"]
    assert feed["status"] == "degraded"
    assert feed["ok"] is False
    assert "feed_never_populated" in feed["reason_codes"]


def test_akosha_health_returns_503_when_aggregator_failed(
    http_client: TestClient,
) -> None:
    """A feed whose producer is dead → failed → 503.

    Complement to the warming_up + degraded tests. A crashed producer
    (``ingester_running=False`` + ``entities=0``) is the most severe
    state — operators need to know the producer isn't coming back
    without manual intervention.
    """
    probe = _probe_returning(
        {
            "local_traces": {
                "entities_count": 0,
                "cycles_total": 5,
                "errors_total": 0,
                "ingester_running": False,  # ← producer dead
            }
        }
    )
    set_health_probe(probe)

    response = http_client.get("/health")
    body = response.json()

    assert response.status_code == 503
    feed = body["checks"]["local_traces"]
    assert feed["status"] == "failed"
    assert feed["ok"] is False
    assert "feed_never_populated" in feed["reason_codes"]
    assert "ingester_not_running" in feed["reason_codes"]


def test_akosha_health_top_level_reason_codes_populate(
    http_client: TestClient,
) -> None:
    """The top-level ``_aggregate.reason_codes`` carries the worst feed's codes.

    Operators reading ``/health`` from a launchd wrapper see only the
    top-level signal (status, reason_codes). The aggregator must
    surface the worst feed's reason_codes at the top level so operators
    can see WHY the aggregate is degraded/failed without parsing each
    per-feed dict.
    """
    import time

    probe = _probe_returning(
        {
            "local_traces": {
                "entities_count": 100,
                "cycles_total": 10,
                "errors_total": 1,
                "last_error_at": time.time() - 5.0,
                "ingester_running": True,
            },
            "code_graphs": {
                "entities_count": 100,
                "cycles_total": 5,
                "errors_total": 0,
                "ingester_running": True,
            },
        }
    )
    set_health_probe(probe)

    response = http_client.get("/health")
    body = response.json()

    assert response.status_code == 503
    aggregate = body["checks"]["_aggregate"]
    assert aggregate["status"] == "degraded"
    # Top-level reason_codes includes the worst feed's codes.
    assert "error_within_halflife" in aggregate["reason_codes"]


def test_akosha_health_four_mandatory_fields_present_per_feed(
    http_client: TestClient,
) -> None:
    """Every data-feed dict carries the four mandatory wire fields.

    Per ``mcp-backend-wiring-discipline.md``: ``feed_entities_count``,
    ``feed_last_updated_timestamp``, ``cycles_total``, ``errors_total``
    are mandatory on every feed.
    """
    probe = _probe_returning(
        {
            "local_traces": {
                "entities_count": 42,
                "cycles_total": 7,
                "errors_total": 0,
                "ingester_running": True,
            },
            "code_graphs": {
                "entities_count": 5,
                "cycles_total": 5,
                "errors_total": 0,
                "ingester_running": True,
            },
            "skills_signer": {
                "entities_count": 3,
                "cycles_total": 1,
                "errors_total": 0,
                "ingester_running": True,
            },
        }
    )
    set_health_probe(probe)

    response = http_client.get("/health")
    body = response.json()
    checks = body["checks"]

    for feed_name in ("local_traces", "code_graphs", "skills_signer"):
        feed = checks[feed_name]
        assert "feed_entities_count" in feed, feed_name
        assert "feed_last_updated_timestamp" in feed, feed_name
        assert "cycles_total" in feed, feed_name
        assert "errors_total" in feed, feed_name
        assert "status" in feed, feed_name
        assert "reason_codes" in feed, feed_name
        assert "ok" in feed, feed_name


def test_akosha_health_worst_case_rollup_mixed_states(
    http_client: TestClient,
) -> None:
    """Multiple feeds at mixed severities → worst status wins.

    Pin the aggregator's worst-case roll-up: across feeds at varying
    severities, the top-level status is the maximum. Operators see a
    single, unambiguous signal even when individual feeds are at
    different stages.
    """
    probe = _probe_returning(
        {
            # healthy
            "code_graphs": {
                "entities_count": 10,
                "cycles_total": 5,
                "errors_total": 0,
                "ingester_running": True,
            },
            # warming_up
            "knowledge_graph": {
                "entities_count": 0,
                "cycles_total": 1,
                "errors_total": 0,
                "ingester_running": True,
            },
            # failed
            "local_traces": {
                "entities_count": 0,
                "cycles_total": 5,
                "errors_total": 0,
                "ingester_running": False,
            },
        }
    )
    set_health_probe(probe)

    response = http_client.get("/health")
    body = response.json()

    # Worst status is failed → 503.
    assert response.status_code == 503
    assert body["checks"]["_aggregate"]["status"] == "failed"
    assert body["checks"]["code_graphs"]["status"] == "healthy"
    assert body["checks"]["knowledge_graph"]["status"] == "warming_up"
    assert body["checks"]["local_traces"]["status"] == "failed"


def test_akosha_health_code_graphs_warming_up_after_first_cycle(
    http_client: TestClient,
) -> None:
    """CodeGraphIngester's first cycle flips status from DEGRADED → WARMING_UP.

    Regression for the HNSW-on-DuckDB hardening gap: before
    CodeGraphIngester wired ``cycles_total`` in its polling loop, the
    feed was always reported as ``degraded`` with
    ``feed_never_populated`` (cycles_total == 0 + ingester_running).
    After wiring, the first cycle bumps ``cycles_total`` to 1, the
    aggregator reports WARMING_UP (entities=0 + alive + cycled), and
    /health stays 200.
    """
    probe = _probe_returning(
        {
            # After one cycle, the ingester has bumped cycles_total
            # but hasn't ingested any graphs yet → warming_up.
            "code_graphs": {
                "entities_count": 0,
                "cycles_total": 1,
                "errors_total": 0,
                "ingester_running": True,
            },
        }
    )
    set_health_probe(probe)

    response = http_client.get("/health")
    body = response.json()

    assert response.status_code == 200
    feed = body["checks"]["code_graphs"]
    assert feed["status"] == "warming_up"
    assert feed["ok"] is False
    assert "warming_up_empty_feed" in feed["reason_codes"]
    # The DEGRADED HNSW reason should NOT surface once cycles_total > 0.
    assert "feed_never_populated" not in feed["reason_codes"]
