"""Tests for akosha.observability.eventbridge_resolver.

The resolver is the production wiring entry point: given an
``AksoshaConfig`` and an optional bridge, it constructs an
``EventBridgePublisher`` (or None when the operator hasn't opted in)
and calls ``set_eventbridge_publisher`` to inject it as the
module-level global.

Tests verify the wiring is opt-in and never wires when the operator
has not enabled it.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from akosha.config import AkoshaConfig, EventBridgeConfig
from akosha.observability import eventbridge_publisher
from akosha.observability.eventbridge_adapter import EventBridgePublisher
from akosha.observability.eventbridge_resolver import wire_eventbridge_publisher


def _make_config(*, enabled: bool, dry_run: bool = False) -> AkoshaConfig:
    """Construct an AkoshaConfig with the given eventbridge settings."""
    return AkoshaConfig(eventbridge=EventBridgeConfig(enabled=enabled, dry_run=dry_run))


@pytest.fixture(autouse=True)
def _reset_module_publisher() -> None:
    """Guarantee the module-level publisher is reset around each test."""
    eventbridge_publisher.set_eventbridge_publisher(None)
    yield
    eventbridge_publisher.set_eventbridge_publisher(None)


def test_wire_returns_none_when_disabled() -> None:
    """Disabled (default) -> no publisher wired; module global stays None."""
    cfg = _make_config(enabled=False)
    bridge = MagicMock()
    result = wire_eventbridge_publisher(cfg, bridge=bridge)
    assert result is None
    assert eventbridge_publisher._get_publisher() is None


def test_wire_returns_none_when_dry_run() -> None:
    """dry_run=True is a safety override; never wire a real bridge."""
    cfg = _make_config(enabled=True, dry_run=True)
    bridge = MagicMock()
    result = wire_eventbridge_publisher(cfg, bridge=bridge)
    assert result is None
    assert eventbridge_publisher._get_publisher() is None


def test_wire_sets_publisher_when_enabled_and_live() -> None:
    """Enabled + dry_run=False + bridge -> EventBridgePublisher wired."""
    cfg = _make_config(enabled=True, dry_run=False)
    bridge = MagicMock()
    result = wire_eventbridge_publisher(cfg, bridge=bridge)
    assert isinstance(result, EventBridgePublisher)
    assert eventbridge_publisher._get_publisher() is result


def test_wire_returns_none_when_bridge_is_none() -> None:
    """No bridge provided (e.g. Oneiric runtime unavailable) -> no wiring."""
    cfg = _make_config(enabled=True, dry_run=False)
    result = wire_eventbridge_publisher(cfg, bridge=None)
    assert result is None
    assert eventbridge_publisher._get_publisher() is None


def test_wire_resets_existing_publisher_when_disabled() -> None:
    """When disabled, an existing module-level publisher is cleared."""
    cfg = _make_config(enabled=False)
    # Pre-populate the global with a dummy publisher
    existing = MagicMock()
    eventbridge_publisher.set_eventbridge_publisher(existing)
    assert eventbridge_publisher._get_publisher() is existing

    # Wire with disabled -> existing publisher cleared
    result = wire_eventbridge_publisher(cfg, bridge=MagicMock())
    assert result is None
    assert eventbridge_publisher._get_publisher() is None
