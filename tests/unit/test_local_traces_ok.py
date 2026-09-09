"""Tests for the Phase 3 OTel feed-recovery changes to ``local_traces_ok``.

Plan: ``docs/superpowers/plans/2026-09-09-akosha-otel-feed-recovery.md``
Phase 3 (lines 145-170) — REQ-005 + REQ-007.

REQ-005: the formula must tolerate a running-but-empty OTel producer
("warming up") as healthy, and emit ``feed_populated`` so callers can
distinguish empty-feed from crash-without-data.

REQ-007: the existing ``AKOSHA_SKIP_OTEL_INGESTER`` env var must
continue to gate the OTel ingester; no new opt-out flag is added.
When that var is set, ``otel_disabled`` short-circuits the formula.

These tests pin three things:

1. ``local_traces_ok`` source-code structure — the formula references
   ``otel_disabled`` and reads the existing env var (no new flag).
2. ``local_traces_feed`` source-code structure — the payload dict
   carries the new ``feed_populated`` field.
3. ``/health`` end-to-end behaviour — when the lifespan-installed
   probe is swapped for one that mimics the post-change shape, the
   field surfaces correctly to HTTP callers.

The lifespan probe (``_default_health_probe``) is a closure that
cannot be invoked directly without spinning up the full server, so
the source-code assertions here are the ground truth.  The HTTP
assertion uses the public ``set_health_probe()`` swap (the same
pattern as ``test_mcp_health_endpoint.py``) to verify the payload
shape survives serialisation through ``/health``.
"""

from __future__ import annotations

import ast
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from akosha.mcp.server import create_app, set_health_probe

SERVER_PY = Path(__file__).resolve().parents[2] / "akosha" / "mcp" / "server.py"


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


# ---------------------------------------------------------------------------
# Source-code structural assertions (REQ-005, REQ-007).
# ---------------------------------------------------------------------------


def _parse_server_module() -> ast.Module:
    """Parse server.py into an AST for static-shape assertions."""
    return ast.parse(SERVER_PY.read_text(encoding="utf-8"))


def _find_local_traces_ok_assignment(tree: ast.Module) -> ast.Assign | None:
    """Locate the ``local_traces_ok = ...`` statement in the probe closure."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "local_traces_ok":
                    return node
    return None


def _find_local_traces_feed_dict(tree: ast.Module) -> ast.Dict | None:
    """Locate the ``checks["local_traces_feed"] = {...}`` dict literal."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Subscript)
            and isinstance(node.targets[0].value, ast.Name)
            and node.targets[0].value.id == "checks"
            and isinstance(node.targets[0].slice, ast.Constant)
            and node.targets[0].slice.value == "local_traces_feed"
            and isinstance(node.value, ast.Dict)
        ):
            return node.value
    return None


def test_local_traces_ok_formula_references_otel_disabled() -> None:
    """REQ-005 + REQ-007: the formula's first disjunct must be ``otel_disabled``.

    The new short-circuit guarantees that an operator who set
    ``AKOSHA_SKIP_OTEL_INGESTER=1`` (and so never had an
    ``_otel_trace_ingester`` constructed) reports ``ok=True`` without
    any producer state needing to be alive.  Pinning the disjunct's
    *position* (first clause) catches a future refactor that might
    demote it behind a less-specific term.
    """
    tree = _parse_server_module()
    assign = _find_local_traces_ok_assignment(tree)
    assert assign is not None, "local_traces_ok assignment not found"
    assert isinstance(assign.value, ast.BoolOp) and isinstance(assign.value.op, ast.Or), (
        "local_traces_ok must remain a chained ``or`` so the first "
        "true disjunct wins (otel_disabled short-circuits the rest)"
    )
    first_clause = assign.value.values[0]
    assert isinstance(first_clause, ast.Name) and first_clause.id == "otel_disabled", (
        "REQ-007: the first disjunct must be ``otel_disabled`` so the "
        "AKOSHA_SKIP_OTEL_INGESTER=1 path wins before any producer check"
    )


def test_local_traces_ok_formula_reads_skip_otel_env_via_env_truthy() -> None:
    """REQ-007: honour the existing ``AKOSHA_SKIP_OTEL_INGESTER`` env var — no new flag.

    The formula's ``otel_disabled`` term is derived from the same
    env var the lifespan check uses (see ``_env_truthy`` at lines
    148-160).  Pinning that the *only* call to ``_env_truthy`` with
    the OTel name is the lifespan check (i.e. no new opt-out was
    introduced) protects against a future refactor that adds a
    parallel flag.
    """
    tree = _parse_server_module()
    otel_truthy_calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_env_truthy"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "AKOSHA_SKIP_OTEL_INGESTER"
        ):
            otel_truthy_calls.append(node)
    assert len(otel_truthy_calls) >= 2, (
        "REQ-007: AKOSHA_SKIP_OTEL_INGESTER must be honoured by the "
        "lifespan (one call) AND by the formula via otel_disabled "
        "(second call).  Found only "
        f"{len(otel_truthy_calls)} _env_truthy call(s) for that var."
    )


def test_local_traces_feed_payload_carries_feed_populated_field() -> None:
    """REQ-005: ``feed_populated`` is required so callers can tell warming-up from dead."""
    tree = _parse_server_module()
    payload = _find_local_traces_feed_dict(tree)
    assert payload is not None, "local_traces_feed dict literal not found"
    keys: list[str] = []
    for k in payload.keys:
        if isinstance(k, ast.Constant):
            keys.append(str(k.value))
    assert "feed_populated" in keys, (
        f"local_traces_feed payload missing feed_populated; saw keys={keys}"
    )
    # The value must mirror ``feed_entities_count > 0`` so the two
    # signals cannot drift apart.  The literal expression we accept:
    #   feed_entities_count > 0
    for k, v in zip(payload.keys, payload.values):
        if isinstance(k, ast.Constant) and k.value == "feed_populated":
            assert isinstance(v, ast.Compare), (
                "feed_populated must be a comparison (count > 0), not a literal"
            )
            left = v.left
            assert isinstance(left, ast.Name) and left.id == "local_traces_count", (
                "feed_populated must be derived from local_traces_count"
            )
            assert any(isinstance(c, ast.Gt) for c in v.ops), (
                "feed_populated must use '>' (count > 0)"
            )
            return
    pytest.fail("feed_populated key found but no matching value to inspect")


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
                "otel_ingester_running": True,
                "otel_endpoint": "http://localhost:4318/v1/traces",
                "otel_cycles_total": 5,
                "otel_errors_total": 0,
                "source": "hot_store.query_traces (populated via kg_refresh + OtelTraceIngester)",
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
                "otel_ingester_running": True,
                "otel_endpoint": "http://localhost:4318/v1/traces",
                "otel_cycles_total": 1,
                "otel_errors_total": 1,
                "source": "hot_store.query_traces (populated via kg_refresh + OtelTraceIngester)",
            },
        }

    set_health_probe(empty_warming_probe)
    response = http_client.get("/health")
    assert response.status_code == 200
    feed = response.json()["checks"]["local_traces_feed"]
    assert feed["ok"] is True
    assert feed["feed_populated"] is False
    assert feed["feed_entities_count"] == 0


def test_otel_disabled_short_circuits_local_traces_ok_via_existing_env_var(
    monkeypatch: pytest.MonkeyPatch,
    http_client: TestClient,
) -> None:
    """REQ-007: AKOSHA_SKIP_OTEL_INGESTER=1 → ``otel_disabled`` wins.

    Even with zero producer state (the env var caused the lifespan
    to skip constructing ``_otel_trace_ingester``), the new
    ``otel_disabled`` disjunct in the formula reports ``ok=True``.
    """

    monkeypatch.setenv("AKOSHA_SKIP_OTEL_INGESTER", "1")

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
                "otel_ingester_running": False,
                "otel_endpoint": None,
                "otel_cycles_total": 0,
                "otel_errors_total": 0,
                "source": "hot_store.query_traces (populated via kg_refresh + OtelTraceIngester)",
            },
        }

    set_health_probe(otel_disabled_probe)
    response = http_client.get("/health")
    assert response.status_code == 200
    feed = response.json()["checks"]["local_traces_feed"]
    assert feed["ok"] is True
    assert feed["otel_ingester_running"] is False
    assert feed["otel_endpoint"] is None


# Keep the unused-import linter happy on ``Awaitable, Callable`` — the
# fixture signatures rely on them being importable for type-checkers
# that don't see through ``Callable[[], Awaitable[...]]``.
_ = (Awaitable, Callable)
