"""Tests for the IPython shell stub surface.

Per audit C3: five shell commands used to return ``status="success"``
with empty results — operators couldn't distinguish "no results" from
"command not implemented". These tests pin the new explicit stub
envelope and check the feature-tracking pointer is present so anyone
hitting one of these in production can find the roadmap entry.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from akosha.shell.adapter import AkoshaShell


@pytest.fixture
def shell() -> AkoshaShell:
    """Construct an AkoshaShell with a mock app — no real AkoshaApplication boot."""
    return AkoshaShell(app=MagicMock())


_STUB_COMMANDS: list[tuple[str, Callable[[AkoshaShell], Awaitable[dict[str, Any]]]]] = [
    ("aggregate", lambda s: s._aggregate(query="foo", filters=None, limit=10)),
    ("search", lambda s: s._search(query="foo", index="all", limit=5)),
    ("detect", lambda s: s._detect(metric="cpu", threshold=0.9, window=60)),
    ("graph", lambda s: s._graph(query="foo", node_type="system", depth=2)),
    ("trends", lambda s: s._trends(metric="latency", window=300, granularity=30)),
]


@pytest.mark.parametrize(
    ("command_name", "invoker"),
    _STUB_COMMANDS,
    ids=[c[0] for c in _STUB_COMMANDS],
)
async def test_stub_command_returns_stub_envelope(
    shell: AkoshaShell,
    command_name: str,
    invoker: Callable[[AkoshaShell], Awaitable[dict[str, Any]]],
) -> None:
    """Each shell command returns ``status="stub"`` with the right command name."""
    result = await invoker(shell)
    assert result["status"] == "stub"
    assert result["command"] == command_name


@pytest.mark.parametrize(
    ("command_name", "invoker"),
    _STUB_COMMANDS,
    ids=[c[0] for c in _STUB_COMMANDS],
)
async def test_stub_command_message_references_feature_tracking(
    shell: AkoshaShell,
    command_name: str,
    invoker: Callable[[AkoshaShell], Awaitable[dict[str, Any]]],
) -> None:
    """The stub message points to the feature-tracking roadmap."""
    result = await invoker(shell)
    assert "feature-tracking/2026-09-05-akosha-hardening" in result["message"]


@pytest.mark.parametrize(
    ("command_name", "invoker"),
    _STUB_COMMANDS,
    ids=[c[0] for c in _STUB_COMMANDS],
)
async def test_stub_command_does_not_claim_success(
    shell: AkoshaShell,
    command_name: str,
    invoker: Callable[[AkoshaShell], Awaitable[dict[str, Any]]],
) -> None:
    """Status must NOT be ``success`` — the bug we're fixing is operators being told
    things worked when they didn't."""
    result = await invoker(shell)
    assert result["status"] != "success"


@pytest.mark.parametrize(
    ("command_name", "invoker"),
    _STUB_COMMANDS,
    ids=[c[0] for c in _STUB_COMMANDS],
)
async def test_stub_command_preserves_input_args(
    shell: AkoshaShell,
    command_name: str,
    invoker: Callable[[AkoshaShell], Awaitable[dict[str, Any]]],
) -> None:
    """Caller-provided args are echoed back so operators can confirm what they sent."""
    result = await invoker(shell)
    # Every stub retains at least the original query/metric key.
    assert any(key in result for key in ("query", "metric"))


def test_stub_envelope_shape_is_consistent(shell: AkoshaShell) -> None:
    """All five stubs share the same envelope shape (status, command, message)."""
    import asyncio

    async def collect() -> list[dict[str, Any]]:
        return [
            await shell._aggregate("x"),
            await shell._search("x"),
            await shell._detect("x"),
            await shell._graph("x"),
            await shell._trends("x"),
        ]

    results = asyncio.run(collect())
    for r in results:
        assert set(r.keys()) >= {"status", "command", "message"}
        assert r["status"] == "stub"
