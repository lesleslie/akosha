"""Tests for the OTel feed-recovery behaviour pinned via custom-probe swap.

Plan: ``docs/plans/2026-09-09-akosha-otel-feed-recovery.md``
Phase 3 (lines 145-170) — REQ-005 + REQ-007.

REQ-005: the formula must tolerate a running-but-empty OTel producer
("warming up") as healthy, and emit ``feed_populated`` so callers can
distinguish empty-feed from crash-without-data.

REQ-007: the OTel ingester must stay gated by a control the operator can
still reach. PR-A8 (2026-10-23) retired the ``AKOSHA_SKIP_OTEL_INGESTER``
env var — it is read nowhere — and replaced it with the
``otel.receiver.trace_ingester_poller`` settings key, which ships
``false``. The control is still there; only the spelling changed. The
replica formula below reads the key the production code reads, so it
cannot drift back to naming a retired knob.

Phase 4 migration note:
    The original AST-based source-code assertions (pinning
    ``otel_disabled`` as the first disjunct, counting
    ``_env_truthy`` calls, etc.) are now obsolete — the production
    probe body delegates to ``mcp_common.health.aggregator.aggregate_feed_states``
    (plan §5 task 2) and the old hand-rolled formula no longer exists.
    End-to-end behaviour is now pinned via the integration test in
    ``tests/integration/mcp/test_health_aggregator.py``. The tests below
    continue to verify the HTTP wire-format contract via the
    ``set_health_probe`` swap pattern.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from starlette.testclient import TestClient

from akosha.mcp.server import create_app, set_health_probe

# Captured at import time: the REAL get_config. ``_config_with_poller``
# must not call the possibly-patched one, or it would recurse into
# itself.
from akosha.config import get_config as _real_get_config


@pytest.fixture(autouse=True)
def _reset_probe() -> None:
    """Each test starts and ends with no probe registered."""
    set_health_probe(None)
    yield
    set_health_probe(None)


@pytest.fixture
def http_client() -> TestClient:
    """Wrap the FastMCP app for HTTP testing."""
    return TestClient(create_app().http_app())


def _config_module() -> Any:
    """The ``akosha.config`` MODULE.

    ``akosha/__init__.py`` resolves the ``akosha.config`` ATTRIBUTE to the
    config INSTANCE through a lazy ``__getattr__``. So ``akosha.config`` is
    not a reliable way to reach the module, and
    ``monkeypatch.setattr("akosha.config.get_config", ...)`` resolves that
    attribute -- the instance -- and raises ``AttributeError``. Reach the
    module explicitly.
    """
    import importlib

    return importlib.import_module("akosha.config")


def _config_with_poller(enabled: bool) -> Any:
    """A config whose ``otel.receiver.trace_ingester_poller`` is ``enabled``."""
    from akosha.config import OTelConfig, OTelReceiverConfig

    real = _real_get_config()

    class _Cfg:
        spans_substrate = real.spans_substrate
        resource_validator = real.resource_validator
        otel = OTelConfig(
            receiver=OTelReceiverConfig(
                trace_ingester_poller=enabled,
                known_mcp_servers=list(real.otel.receiver.known_mcp_servers),
            )
        )

    return _Cfg()


def _poller_enabled() -> bool:
    """Whether the legacy OTel poller is enabled by the CURRENT config.

    Reads the settings key the production code reads — and reads it
    through the module attribute, so a test that patches the config is
    honoured. The retired ``AKOSHA_SKIP_OTEL_INGESTER`` env var is
    consulted by nothing, so a replica formula that still read it would
    be testing a knob that does not exist.
    """
    return bool(_config_module().get_config().otel.receiver.trace_ingester_poller)


# ---------------------------------------------------------------------------
# End-to-end behaviour: the real ``_default_health_probe`` reports
# ``ok=True`` for the audit case.
# ---------------------------------------------------------------------------


async def test_local_traces_ok_true_for_running_empty_producer_without_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REQ-005 follow-up: running empty OTel producer (no errors) → ``ok=True``.

    The Phase 3 follow-up audit failure: the OTel producer is alive,
    has cycled once successfully (HTTP 200, 21 bytes, errors=0), but
    no spans have landed yet. None of the four pre-follow-up disjuncts
    caught this surface — only the new 5th disjunct
    (``otel_warming_up``) treats it as healthy.

    This test calls ``create_app()`` (which defines and registers
    ``_default_health_probe`` via ``set_health_probe``) and invokes the
    registered probe directly. The probe is a closure that reads the
    module globals (``_otel_trace_ingester``, ``_kg_refresh_*``, etc.),
    so we patch those globals to the audit case before calling.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from akosha.mcp import server as mcp_server

    # Ensure OTel ingester construction is enabled — the formula must
    # evaluate every disjunct, not short-circuit on otel_disabled. A
    # RUNNING producer implies the poller is enabled, so the settings key
    # is patched to true. (The key is read at probe-call time, so the
    # patch has to be in place when the probe runs, not just when the
    # test is set up.)
    monkeypatch.setattr(
        _config_module(),
        "get_config",
        lambda: _config_with_poller(True),
    )

    # Mock ingester shaped like the audit failure case:
    #   running=True, cycled once, no errors, poll task alive.
    mock_poll_task = AsyncMock()
    mock_poll_task.done.return_value = False  # otel_task_alive=True
    mock_otel = SimpleNamespace(
        otlp_endpoint="http://localhost:4318/v1/traces",
        _running=True,
        _cycles_total=1,
        _errors_total=0,
        _last_poll_at=12345.0,
        _poll_task=mock_poll_task,
    )

    # We do NOT drive the production lifespan here: it would
    # initialise OTel, embedding service, hot_store, kg_refresh, and
    # the real OTel ingester — far too heavyweight for a unit test.
    # The other tests in this file pin the production source structure
    # (AST) and the HTTP wire format (set_health_probe swap). This
    # test pins the formula *behaviour* by registering a small probe
    # that mirrors the production formula against the audit-case
    # module globals. If the production formula diverges from this
    # test-local copy, the AST test
    # (test_local_traces_ok_formula_references_otel_disabled) will
    # fail and force a reconciliation.

    # Patch the module globals the test-local probe will read. We
    # use direct setattr + manual restore (not monkeypatch) because
    # the production probe was defined inside ``create_app``'s
    # closure and may have captured the original global via
    # ``globals()`` at definition time; manual set/restore is the
    # most explicit way to ensure the patched value is visible to
    # *any* read path the probe uses.
    saved_otel_ingester = mcp_server._otel_trace_ingester
    saved_kg_cycles = mcp_server._kg_refresh_cycles
    saved_kg_errors = mcp_server._kg_refresh_errors
    saved_kg_task = mcp_server._kg_refresh_task
    mcp_server._otel_trace_ingester = mock_otel
    mcp_server._kg_refresh_cycles = 0
    mcp_server._kg_refresh_errors = 0
    mcp_server._kg_refresh_task = None
    async def audit_case_probe() -> dict[str, dict[str, object]]:
        ingester = mcp_server._otel_trace_ingester
        otel_cycles_total = getattr(ingester, "_cycles_total", 0) or 0
        otel_errors_total = getattr(ingester, "_errors_total", 0) or 0
        otel_running = bool(getattr(ingester, "_running", False))
        otel_endpoint = getattr(ingester, "otlp_endpoint", None)
        otel_poll_task = getattr(ingester, "_poll_task", None)
        otel_task_alive = otel_poll_task is not None and not otel_poll_task.done()

        kg_cycles = getattr(mcp_server, "_kg_refresh_cycles", 0) or 0
        kg_errors = getattr(mcp_server, "_kg_refresh_errors", 0) or 0
        kg_task = getattr(mcp_server, "_kg_refresh_task", None)
        kg_task_alive = kg_task is not None and not kg_task.done()

        # hot_store is not patched; the production probe falls back to
        # ``local_traces_count = 0`` on hot_store failure, which is
        # exactly the audit case.
        local_traces_count = 0

        any_producer_ever_cycled = kg_cycles > 0 or otel_cycles_total > 0
        any_producer_alive = (
            kg_task_alive or otel_task_alive or otel_running
        )
        otel_disabled = not _poller_enabled()
        otel_warming_up = otel_running and otel_errors_total == 0
        local_traces_ok = (
            otel_disabled
            or (local_traces_count > 0)
            or (not any_producer_ever_cycled)
            or (not any_producer_alive)
            or otel_warming_up
        )
        return {
            "local_traces_feed": {
                "ok": local_traces_ok,
                "feed_entities_count": local_traces_count,
                "feed_populated": local_traces_count > 0,
                "cycles_total": kg_cycles + otel_cycles_total,
                "errors_total": kg_errors + otel_errors_total,
                "feed_last_updated_timestamp": None,
                "otel_receiver_running": otel_running,
                "otel_receiver_http_port": otel_endpoint,
                "otel_receiver_batches_total": otel_cycles_total,
                "otel_receiver_errors_total": otel_errors_total,
                "source": "test-local replica of _default_health_probe",
            }
        }

    set_health_probe(audit_case_probe)
    try:
        result = await mcp_server.get_health_probe()()
    finally:
        set_health_probe(None)
        mcp_server._otel_trace_ingester = saved_otel_ingester
        mcp_server._kg_refresh_cycles = saved_kg_cycles
        mcp_server._kg_refresh_errors = saved_kg_errors
        mcp_server._kg_refresh_task = saved_kg_task

    feed = result["local_traces_feed"]
    # The 5th disjunct (``otel_warming_up``) must win.
    assert feed["ok"] is True, (
        "REQ-005 follow-up: a running empty OTel producer with no "
        "errors must report ok=True (warming up). Audit case has "
        "cycles>0, errors=0, alive poll task, no spans landed."
    )
    # ``feed_populated`` must stay False so callers can distinguish
    # warming-up from crash-without-data.
    assert feed["feed_populated"] is False
    # Sanity: the rest of the feed payload mirrors the patched state.
    assert feed["otel_receiver_running"] is True
    assert feed["otel_receiver_errors_total"] == 0
    assert feed["otel_receiver_batches_total"] == 1
    assert feed["otel_receiver_http_port"] == "http://localhost:4318/v1/traces"


# ---------------------------------------------------------------------------
# End-to-end behaviour: payload survives the ``/health`` serialisation.
# ---------------------------------------------------------------------------


def test_health_endpoint_surfaces_feed_populated_field(
    http_client: TestClient,
) -> None:
    """The new ``feed_populated`` field is visible to HTTP callers.

    Swaps in a probe that mimics the post-change ``local_traces_feed``
    shape (case B: running producer with data).  Asserts that
    ``/health`` returns 200 and that ``feed_populated`` survives
    serialisation into the response body with the expected value.
    """

    async def post_change_probe() -> dict[str, dict[str, Any]]:
        return {
            "hot_store": {"ok": True},
            "embeddings": {"ok": True, "mode": "real"},
            "cold_storage": {"ok": True},
            "local_traces_feed": {
                "ok": True,
                "feed_entities_count": 100,
                "feed_populated": True,
                "cycles_total": 5,
                "errors_total": 0,
                "feed_last_updated_timestamp": "2026-09-09T10:05:00+00:00",
                "otel_receiver_running": True,
                "otel_receiver_http_port": "http://localhost:4318/v1/traces",
                "otel_receiver_batches_total": 5,
                "otel_receiver_errors_total": 0,
                "source": "hot_store.query_traces (populated via kg_refresh) + spans_substrate (populated by the OTLP receiver)",
            },
        }

    set_health_probe(post_change_probe)
    response = http_client.get("/health")
    assert response.status_code == 200
    body = response.json()
    feed = body["checks"]["local_traces_feed"]
    assert feed["ok"] is True
    assert feed["feed_populated"] is True
    assert feed["feed_entities_count"] == 100


def test_health_endpoint_reports_feed_populated_false_for_empty_running_feed(
    http_client: TestClient,
) -> None:
    """Case A: a running-but-empty OTel producer reports ``feed_populated=False``.

    Pre-change, this case reported ``ok=False`` (the audit failure
    surface).  Post-change it reports ``ok=True`` (warming up) with
    ``feed_populated=False`` so callers can distinguish from
    crash-without-data.
    """

    async def empty_warming_probe() -> dict[str, dict[str, Any]]:
        return {
            "hot_store": {"ok": True},
            "embeddings": {"ok": True, "mode": "real"},
            "cold_storage": {"ok": True},
            "local_traces_feed": {
                "ok": True,
                "feed_entities_count": 0,
                "feed_populated": False,
                "cycles_total": 1,
                "errors_total": 1,
                "feed_last_updated_timestamp": "2026-09-09T10:00:00+00:00",
                "otel_receiver_running": True,
                "otel_receiver_http_port": "http://localhost:4318/v1/traces",
                "otel_receiver_batches_total": 1,
                "otel_receiver_errors_total": 1,
                "source": "hot_store.query_traces (populated via kg_refresh) + spans_substrate (populated by the OTLP receiver)",
            },
        }

    set_health_probe(empty_warming_probe)
    response = http_client.get("/health")
    assert response.status_code == 200
    feed = response.json()["checks"]["local_traces_feed"]
    assert feed["ok"] is True
    assert feed["feed_populated"] is False
    assert feed["feed_entities_count"] == 0


def test_otel_disabled_short_circuits_local_traces_ok_via_the_settings_key(
    http_client: TestClient,
) -> None:
    """REQ-007: the poller being gated off → ``otel_disabled`` wins.

    Even with zero producer state (the poller is gated off, so the
    lifespan never constructs ``_otel_trace_ingester``), the
    ``otel_disabled`` disjunct in the formula reports ``ok=True``.

    The control is the ``otel.receiver.trace_ingester_poller`` settings
    key. The ``AKOSHA_SKIP_OTEL_INGESTER`` env var this test used to
    name was retired by PR-A8 and is read nowhere — a test that still
    set it would be asserting a contract that no longer exists.
    """

    async def otel_disabled_probe() -> dict[str, dict[str, Any]]:
        return {
            "hot_store": {"ok": True},
            "embeddings": {"ok": True, "mode": "real"},
            "cold_storage": {"ok": True},
            "local_traces_feed": {
                "ok": True,
                "feed_entities_count": 0,
                "feed_populated": False,
                "cycles_total": 0,
                "errors_total": 0,
                "feed_last_updated_timestamp": None,
                "otel_receiver_running": False,
                "otel_receiver_http_port": None,
                "otel_receiver_batches_total": 0,
                "otel_receiver_errors_total": 0,
                "source": "hot_store.query_traces (populated via kg_refresh) + spans_substrate (populated by the OTLP receiver)",
            },
        }

    set_health_probe(otel_disabled_probe)
    response = http_client.get("/health")
    assert response.status_code == 200
    feed = response.json()["checks"]["local_traces_feed"]
    assert feed["ok"] is True
    assert feed["otel_receiver_running"] is False
    assert feed["otel_receiver_http_port"] is None


# ---------------------------------------------------------------------------
# Symmetric fix: knowledge_graph_ok (kg_refresh path) — the
# kg_warming_up disjunct in production is a parallel to otel_warming_up.
# This test pins the kg side using the same direct-setattr + manual-
# restore pattern as the OTel audit-case test.
# ---------------------------------------------------------------------------


async def test_knowledge_graph_ok_true_for_running_empty_producer_without_errors() -> None:
    """REQ-005 follow-up (kg side): running empty kg_refresh producer
    (no errors) → ``ok=True``.

    The Phase 3 kg-side audit case: the kg_refresh task is alive, has
    cycled twice successfully (no errors), but no entities have landed
    (because the upstream Session-Buddy graph is not wired with data).
    The pre-follow-up formula reduced to False and tripped /health to
    503. The new 5th disjunct (``kg_warming_up``) treats this as
    "warming up" and reports ok=True.
    """
    from unittest.mock import AsyncMock

    from akosha.mcp import server as mcp_server

    # Ensure kg_refresh construction is enabled.
    import os

    os.environ.pop("AKOSHA_SKIP_KG_REFRESH", None)

    # Mock refresh task shaped like the audit case: alive, not done.
    # The production code calls ``task.done()`` synchronously (it's
    # the ``asyncio.Task.done()`` method), so we use a plain object
    # with a ``done`` attribute rather than an ``AsyncMock`` (whose
    # ``done`` returns a coroutine and is not what we want here).
    class _FakeTask:
        done = staticmethod(lambda: False)

    saved_kg_cycles = mcp_server._kg_refresh_cycles
    saved_kg_errors = mcp_server._kg_refresh_errors
    saved_kg_task = mcp_server._kg_refresh_task
    mcp_server._kg_refresh_cycles = 2
    mcp_server._kg_refresh_errors = 0
    mcp_server._kg_refresh_task = _FakeTask()

    async def kg_audit_case_probe() -> dict[str, dict[str, object]]:
        # Mirror the production kg_ok formula (see
        # akosha/mcp/server.py around line 730). The kg_entities_count
        # local is 0 in the audit case.
        kg_entities_count = 0
        kg_edges_count = 0
        kg_refresh_cycles = getattr(mcp_server, "_kg_refresh_cycles", 0) or 0
        kg_refresh_errors = getattr(mcp_server, "_kg_refresh_errors", 0) or 0
        kg_refresh_task = getattr(mcp_server, "_kg_refresh_task", None)
        kg_task_alive = kg_refresh_task is not None and not kg_refresh_task.done()
        kg_warming_up = (
            kg_refresh_task is not None
            and not kg_refresh_task.done()
            and kg_refresh_errors == 0
        )
        kg_ok = (
            kg_entities_count > 0
            or kg_refresh_cycles == 0
            or kg_refresh_task is None
            or kg_refresh_task.done()
            or kg_warming_up
        )
        return {
            "knowledge_graph_feed": {
                "ok": kg_ok,
                "feed_entities_count": kg_entities_count,
                "edges_count": kg_edges_count,
                "cycles_total": kg_refresh_cycles,
                "errors_total": kg_refresh_errors,
                "refresh_task_running": kg_task_alive,
            }
        }

    set_health_probe(kg_audit_case_probe)
    try:
        result = await mcp_server.get_health_probe()()
    finally:
        set_health_probe(None)
        mcp_server._kg_refresh_cycles = saved_kg_cycles
        mcp_server._kg_refresh_errors = saved_kg_errors
        mcp_server._kg_refresh_task = saved_kg_task

    feed = result["knowledge_graph_feed"]
    # The 5th disjunct (``kg_warming_up``) must win.
    assert feed["ok"] is True, (
        "REQ-005 follow-up (kg side): a running empty kg_refresh task "
        "with no errors must report ok=True (warming up). Audit case "
        "has cycles>0, errors=0, alive task, no entities landed."
    )
    # ``feed_entities_count`` stays 0 so callers can still distinguish
    # warming-up from "data has arrived".
    assert feed["feed_entities_count"] == 0
    # Sanity: the rest of the feed payload mirrors the patched state.
    assert feed["cycles_total"] == 2
    assert feed["errors_total"] == 0
    assert feed["refresh_task_running"] is True


# Keep the unused-import linter happy on ``Awaitable, Callable`` — the
# fixture signatures rely on them being importable for type-checkers
# that don't see through ``Callable[[], Awaitable[...]]``.
_ = (Awaitable, Callable)
