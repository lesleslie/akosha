"""End-to-end: the operator asks for a sweep, and expired rows actually go.

This is the whole chain the tool exists to serve, exercised together:

    MCP tool -> receiver signal -> sweep loop wakes -> DELETE -> rows gone

A unit test can prove the tool sets an event and that the loop honours one.
Only this test proves the two are actually connected -- which is the part
that would silently rot if someone changed one side and not the other.

It also pins the SAFETY property at the same time: the tool alone removes
nothing. The row disappears because the LOOP deleted it, after the tool
returned. A tool that deleted on the caller's behalf would pass the
"rows are gone" assertion here while racing the loop's own DELETE on the
shared DuckDB connection.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from akosha.mcp.server import clear_shared_services, set_shared_otel_receiver
from akosha.mcp.tools.otel_retention_tools import register_otel_retention_tools
from akosha.observability.otlp_receiver import OTLPReceiver
from akosha.storage.hot_store import HotStore


class _FakeApp:
    """Minimal FastMCP stand-in: stores tools registered via ``@app.tool()``."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, name: str | None = None) -> Any:
        """Decorator factory matching FastMCP's ``@app.tool()`` shape."""

        def decorator(fn: Any) -> Any:
            key = name if name else fn.__name__
            self.tools[key] = fn
            return fn

        return decorator


@pytest.fixture
async def store() -> HotStore:
    hs = HotStore(database_path=":memory:", embedding_dim=384)
    await hs.initialize()
    return hs


@pytest.fixture
def receiver(store: HotStore) -> OTLPReceiver:
    return OTLPReceiver(store, grpc_port=0, http_port=0)


def _insert_expired_row(receiver: OTLPReceiver) -> None:
    """Insert one row 60 days old -- past the 30-day retention horizon."""
    old_ns = time.time_ns() - 60 * 24 * 3600 * 1_000_000_000
    receiver._hot_store.conn.execute(  # type: ignore[union-attr]
        "INSERT INTO spans_substrate "
        "(span_id, trace_id, service_name, service_namespace, span_name, "
        " start_time_unix_nano, received_at_unix_nano, attributes, resource) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, '{}', '{}')",
        ["9" * 16, "8" * 32, "svc", "bodai", "ancient", old_ns, old_ns],
    )


def _row_count(receiver: OTLPReceiver) -> int:
    row = receiver._hot_store.conn.execute(  # type: ignore[union-attr]
        "SELECT COUNT(*) FROM spans_substrate"
    ).fetchone()
    return int(row[0])


async def _wait_until(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def _patch_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the config MODULE's ``get_config`` with a year-long interval.

    ``akosha/__init__.py`` resolves the ``akosha.config`` ATTRIBUTE to the
    config INSTANCE through a lazy ``__getattr__``, so the string form
    ``"akosha.config.get_config"`` is not a reliable way to reach the
    module. Reach it explicitly.
    """
    import importlib

    module = importlib.import_module("akosha.config")

    def _fake_get_config(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            spans_substrate=SimpleNamespace(
                retention_sweep_interval_hours=24 * 365,
                retention_days=30,
            )
        )

    monkeypatch.setattr(module, "get_config", _fake_get_config)


async def test_the_operator_ask_actually_removes_the_expired_rows(
    receiver: OTLPReceiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full chain, from the MCP call to the DELETE."""
    # A year-long interval, so the sweep can only run because the operator
    # asked. If the request did not wake the loop, this test would hang and
    # then fail on the row count.
    _patch_config(monkeypatch)

    _insert_expired_row(receiver)
    assert _row_count(receiver) == 1

    set_shared_otel_receiver(receiver)
    app = _FakeApp()
    register_otel_retention_tools(app)
    tool = app.tools["akosha_force_retention_sweep"]

    stop_event = asyncio.Event()
    task = asyncio.create_task(receiver.run_retention_sweep(stop_event))
    try:
        # Give the loop a moment to settle into its interval wait, so the
        # request below is what wakes it.
        await asyncio.sleep(0.1)
        assert _row_count(receiver) == 1, "the loop must not self-fire"

        result = await tool()

        assert result["requested"] is True, result
        assert result["already_pending"] is False
        assert result["watch"] == "akosha_otlp_sweep_last_completed_unix_nano"

        assert await _wait_until(lambda: _row_count(receiver) == 0), (
            "the expired row must be gone once the requested sweep has run"
        )
    finally:
        stop_event.set()
        await asyncio.wait_for(task, timeout=15)
        clear_shared_services()


async def test_the_tool_returns_before_the_sweep_finishes(
    receiver: OTLPReceiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The call queues the sweep; it does not block on it.

    The sweep is a DELETE over an in-memory table -- fast, but not
    instant. The tool's contract is "requested", not "done", and the
    operator confirms completion on the metrics route.
    """
    _patch_config(monkeypatch)

    set_shared_otel_receiver(receiver)
    app = _FakeApp()
    register_otel_retention_tools(app)
    tool = app.tools["akosha_force_retention_sweep"]

    try:
        result = await tool()
        assert result["requested"] is True
        assert result["interval_s"] is None, (
            "no loop has started, so there is no interval to report yet"
        )
    finally:
        clear_shared_services()
