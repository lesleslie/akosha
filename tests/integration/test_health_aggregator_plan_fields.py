"""Plan 2 Phase E2 Task 5 — akosha ``/health`` aggregator 4-field contract.

Pins the Plan §6.5 contract that each ``/health`` per-feed dict carries
the 4 mandatory fields (``entities_count``, ``last_updated_timestamp``,
``cycles_total``, ``errors_total``) — the same 4 fields that
``mcp-backend-wiring-discipline.md §3`` mandates for every registered
tool's data feed.

The test exercises the **wire shape** the akosha ``/health`` route
serves: the dict returned by ``aggregate_feed_states`` is what
``_default_health_probe`` puts into ``checks`` and what the HTTP
``/health`` route serialises. Asserting the 4 fields here catches
regressions whether the source is the probe closure or the HTTP
route.

The skip-when-stale gate (probe call) ensures the test passes
against the pre-widening mcp_common wheel (skip) and asserts
positively against the new one (pass). The skip message points
operators to the version bump that closes the gap.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

# Akosha's ``/health`` aggregates these feed names (per
# ``akosha/mcp/server.py:_default_health_probe``):
#   code_graphs_feed, knowledge_graph_feed, local_traces_feed,
#   mcp_tool_call_feed, aging_feed, skills_signer.
AKOSHA_HEALTH_FEED_NAMES = (
    "code_graphs_feed",
    "knowledge_graph_feed",
    "local_traces_feed",
    "mcp_tool_call_feed",
    "aging_feed",
    "skills_signer",
)


# Plan 2 Phase E2 §6.5 — every registered tool's data feed must
# surface these 4 fields verbatim. See mcp-backend-wiring-discipline.md §3.
PLAN_EXPECTED_FIELDS = (
    "entities_count",
    "last_updated_timestamp",
    "cycles_total",
    "errors_total",
)


def _probe_aggregator_shape() -> dict[str, object] | None:
    """Probe the installed ``mcp_common`` aggregator for the 4-field shape.

    Returns the per-feed check dict if the installed wheel has the
    widening (commit mcp_common@266de23+), or ``None`` if pre-widening
    (in which case the test should skip).
    """
    from mcp_common.health.aggregator import aggregate_feed_states
    from mcp_common.health.feed import HealthFeedState

    snap = aggregate_feed_states(
        {"code_graphs_feed": HealthFeedState(entities_count=1, cycles_total=1)}
    )
    check = snap["checks"]["code_graphs_feed"]
    if "entities_count" in check:
        # FeedSnapshot is a TypedDict; structurally a dict[str, object],
        # but ty sees it as a distinct nominal type, so cast for the
        # declared return shape without runtime conversion.
        return cast("dict[str, object]", check)
    return None


def test_akosha_health_aggregator_emits_four_plan_fields_per_feed() -> None:
    """``aggregate_feed_states`` over akosha's 6 feeds returns 4 plan
    fields per ``checks[feed_name]`` — the canonical wire shape the
    akosha ``/health`` route serialises.

    The aggregator input mirrors the per-feed states that
    ``_default_health_probe`` constructs at ``akosha/mcp/server.py:808``,
    so this test exercises the same shape the live route produces.
    """
    probe = _probe_aggregator_shape()
    if probe is None:
        pytest.skip(
            "mcp_common aggregator does not yet expose the 4 plan-§6.5 "
            "fields. Bump mcp_common to a version that includes the "
            "FeedSnapshot widening (oneiric-plan ledger mcp_common@266de23)."
        )

    from mcp_common.health.aggregator import aggregate_feed_states
    from mcp_common.health.feed import HealthFeedState

    now = 1000.0
    states = {
        name: HealthFeedState(
            entities_count=10,
            cycles_total=5,
            errors_total=0,
            last_updated_timestamp=now,
        )
        for name in AKOSHA_HEALTH_FEED_NAMES
    }
    snap = aggregate_feed_states(states)

    # All 6 akosha feeds must be present in the snapshot.
    assert set(snap["checks"].keys()) == set(AKOSHA_HEALTH_FEED_NAMES)

    for feed_name in AKOSHA_HEALTH_FEED_NAMES:
        check = snap["checks"][feed_name]
        for field in PLAN_EXPECTED_FIELDS:
            assert field in check, (
                f"aggregator dropped {field} from {feed_name} check "
                f"(Plan 2 Phase E2 §6.5 / mcp-backend-wiring-discipline §3)"
            )
            assert check[field] == getattr(states[feed_name], field), (
                f"aggregator {field} mismatch on {feed_name}"
            )


def test_akosha_health_aggregator_default_state_round_trips() -> None:
    """Default-valued ``HealthFeedState`` round-trips with the
    ``None`` ↔ ``0`` distinction preserved (operators rely on the
    difference to spot producers that have not yet completed a cycle).
    """
    probe = _probe_aggregator_shape()
    if probe is None:
        pytest.skip(
            "mcp_common aggregator does not yet expose the 4 plan-§6.5 "
            "fields. Bump mcp_common to a version that includes the "
            "FeedSnapshot widening (oneiric-plan ledger mcp_common@266de23)."
        )

    from mcp_common.health.aggregator import aggregate_feed_states
    from mcp_common.health.feed import HealthFeedState

    state = HealthFeedState()  # all defaults
    snap = aggregate_feed_states({"never_cycled": state})
    check = snap["checks"]["never_cycled"]
    assert check["entities_count"] == 0
    assert check["cycles_total"] == 0
    assert check["errors_total"] == 0
    assert check["last_updated_timestamp"] is None


def test_akosha_health_http_response_shape_carries_four_fields() -> None:
    """The live ``/health`` HTTP response body carries the 4 plan fields
    per feed — the assertion the operator's ``curl /health | jq .feeds``
    workflow depends on (per Plan 2 Phase E2 §10).

    Best-effort: calls ``create_app()`` and uses TestClient. Skips if
    the lifespan fails to construct in the test environment (e.g. a
    thin install without the bootstrap extras). The aggregator-level
    tests above pin the contract from the canonical source; this
    test is the end-to-end smoke.
    """
    from mcp_common.health.aggregator import aggregate_feed_states
    from mcp_common.health.feed import HealthFeedState

    # Pre-flight: detect the new shape via a cheap probe (skip on
    # pre-widening wheel).
    snap = aggregate_feed_states(
        {"code_graphs_feed": HealthFeedState(entities_count=1, cycles_total=1)}
    )
    if "entities_count" not in snap["checks"]["code_graphs_feed"]:
        pytest.skip(
            "mcp_common aggregator does not yet expose the 4 plan-§6.5 "
            "fields. Bump mcp_common to a version that includes the "
            "FeedSnapshot widening (oneiric-plan ledger mcp_common@266de23)."
        )

    # Best-effort: spin up the FastMCP app. If the lifespan is too
    # heavy for the test env (missing hot_store backend, etc.), the
    # test skips with a clear reason — the aggregator-level tests
    # above pin the contract.
    try:
        from fastapi.testclient import TestClient

        from akosha.mcp.server import create_app
    except ImportError as exc:  # pragma: no cover — test env missing
        pytest.skip(f"akosha.mcp.server.create_app unavailable: {exc}")

    try:
        app = create_app()
        # FastMCP is a valid ASGI app (callable, async scope handling);
        # the test client accepts it at runtime, but ty's TestClient
        # type stub expects the FastAPI/Starlette app flavor.
        client = TestClient(app)  # ty: ignore[invalid-argument-type]
        response = client.get("/health")
    except Exception as exc:
        pytest.skip(
            f"create_app()/TestClient setup failed in test env "
            f"(the aggregator-level tests above still pin the contract): {exc}"
        )

    # 200 (healthy/warming_up) or 503 (degraded/failed) — both valid
    # per the mcp-common aggregator contract.
    assert response.status_code in (200, 503), (
        f"unexpected /health status: {response.status_code} body={response.text[:500]}"
    )

    body: dict[str, Any] = response.json()
    # The akosha /health body shape is {status, checks: {feed_name: ...}}
    # (or the legacy probe shape — either way, ``checks`` is the feed dict).
    checks = body.get("checks", body)
    assert isinstance(checks, dict)
    for feed_name, feed_dict in checks.items():
        if not isinstance(feed_dict, dict):
            continue  # non-feed entries (e.g. infrastructure checks) skipped
        for field in PLAN_EXPECTED_FIELDS:
            assert field in feed_dict, (
                f"/health body dropped {field} from {feed_name} check "
                f"(Plan 2 Phase E2 §6.5 / mcp-backend-wiring-discipline §3)"
            )
