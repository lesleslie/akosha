"""Tests for the ``/health`` and ``/healthz`` aggregator endpoints.

These exercise the failure modes the MCP backend wiring discipline was
written to surface: a server with all its tooling wired up but no
underlying data feeds should NOT report ``status: ok``. /health must
return 503 when any feed is degraded or when no probe is registered.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from starlette.testclient import TestClient

from akosha.mcp.server import (
    APP_NAME,
    APP_VERSION,
    create_app,
    get_health_probe,
    set_health_probe,
)


@pytest.fixture(autouse=True)
def _reset_probe() -> None:
    """Each test starts with a clean probe so order doesn't matter."""
    set_health_probe(None)
    yield
    set_health_probe(None)


@pytest.fixture
def http_client() -> TestClient:
    """Build the FastMCP app and wrap it for HTTP testing."""
    app = create_app()
    return TestClient(app.http_app())


@pytest.fixture
def all_ok_probe() -> Callable[[], Awaitable[dict[str, dict[str, Any]]]]:
    async def probe() -> dict[str, dict[str, Any]]:
        return {
            "hot_store": {"ok": True},
            "embeddings": {"ok": True, "mode": "real"},
            "cold_storage": {"ok": True},
        }

    return probe


@pytest.fixture
def degraded_probe() -> Callable[[], Awaitable[dict[str, dict[str, Any]]]]:
    async def probe() -> dict[str, dict[str, Any]]:
        return {
            "hot_store": {"ok": False, "error": "connection refused"},
            "embeddings": {"ok": True},
        }

    return probe


def test_health_returns_200_when_all_feeds_ok(
    http_client: TestClient, all_ok_probe: Callable[[], Awaitable[dict[str, dict[str, Any]]]]
) -> None:
    """All-healthy probe → 200 + status=ok + per-feed breakdown."""
    set_health_probe(all_ok_probe)
    response = http_client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == APP_NAME
    assert body["version"] == APP_VERSION
    assert body["checks"]["hot_store"]["ok"] is True
    assert body["checks"]["embeddings"]["ok"] is True
    assert body["checks"]["cold_storage"]["ok"] is True


def test_health_returns_503_when_any_feed_unhealthy(
    http_client: TestClient, degraded_probe: Callable[[], Awaitable[dict[str, dict[str, Any]]]]
) -> None:
    """Degraded probe → 503 + status=degraded + the failing feed's reason."""
    set_health_probe(degraded_probe)
    response = http_client.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["hot_store"]["ok"] is False
    assert "connection refused" in body["checks"]["hot_store"]["error"]
    # Healthy feeds are still reported — caller can see *what* is up.
    assert body["checks"]["embeddings"]["ok"] is True


def test_health_returns_503_when_no_probe_registered(
    http_client: TestClient,
) -> None:
    """No probe at all → 503 + explicit reason. Fail-loud default."""
    response = http_client.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["probe"]["ok"] is False
    assert "no health probe registered" in body["checks"]["probe"]["error"]
    assert get_health_probe() is None


def test_health_returns_503_when_probe_raises(http_client: TestClient) -> None:
    """Probe that raises → 503 with the exception message captured."""

    async def raising_probe() -> dict[str, dict[str, Any]]:
        raise RuntimeError("dfeed exploded")

    set_health_probe(raising_probe)
    response = http_client.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["probe"]["ok"] is False
    assert "dfeed exploded" in body["checks"]["probe"]["error"]


def test_healthz_returns_200_regardless_of_probe_state(
    http_client: TestClient,
) -> None:
    """/healthz is process-liveness only — always 200.

    Per K8s convention: liveness should not depend on dependency state;
    otherwise a transient dep outage causes the pod to restart, which
    usually makes the outage worse.
    """
    set_health_probe(None)
    response = http_client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_metrics_endpoint_unchanged(http_client: TestClient) -> None:
    """/metrics still works (Prometheus exposition)."""
    response = http_client.get("/metrics")
    assert response.status_code == 200
    # Prometheus exposition starts with ``# HELP`` for the first metric.
    assert response.text.startswith("# HELP") or "version" in response.text


def test_set_health_probe_overrides_previous() -> None:
    """set_health_probe replaces the previous probe (no double-registration)."""

    async def first_probe() -> dict[str, dict[str, Any]]:
        return {"a": {"ok": False}}

    async def second_probe() -> dict[str, dict[str, Any]]:
        return {"b": {"ok": True}}

    set_health_probe(first_probe)
    assert get_health_probe() is first_probe
    set_health_probe(second_probe)
    assert get_health_probe() is second_probe

    set_health_probe(None)
    assert get_health_probe() is None


def test_empty_checks_dict_is_healthy(http_client: TestClient) -> None:
    """A probe that returns ``{}`` is treated as all-ok (vacuous truth)."""

    async def empty_probe() -> dict[str, dict[str, Any]]:
        return {}

    set_health_probe(empty_probe)
    response = http_client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize(
    "feed_status",
    [
        {"ok": False, "error": "timeout"},
        {"ok": False},  # missing 'error' key — still unhealthy
        {"error": "explicit reason", "ok": False},
    ],
)
def test_health_503_for_varied_unhealthy_shapes(
    http_client: TestClient, feed_status: dict[str, Any]
) -> None:
    """Any feed whose ``ok`` field is falsy → 503."""

    async def probe() -> dict[str, dict[str, Any]]:
        return {"thing": feed_status}

    set_health_probe(probe)
    response = http_client.get("/health")
    assert response.status_code == 503
    assert response.json()["checks"]["thing"]["ok"] is False
