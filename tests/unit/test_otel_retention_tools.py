"""The retention sweep trigger tool.

§11.4a's operator-interface table had one row marked "Not available": force
a retention sweep now. The sweep runs on a 6-hour interval, so an operator
under substrate pressure had up to six hours to wait.

This tool closes that row. Two properties are pinned here:

1. **It signals; it does not delete.** The sweep loop owns the single DuckDB
   connection to the substrate. A tool that ran the sweep itself would put a
   second ``DELETE`` against that connection. The tool sets an event and
   returns; the loop -- the sole writer -- performs the sweep.

2. **"Not running" is reported, not swallowed.** With no receiver there is
   no sweep loop to wake. A tool that returned a cheerful success there
   would tell the operator a sweep was coming when none was.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import pytest

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
def app() -> _FakeApp:
    return _FakeApp()


@pytest.fixture
def tool(app: _FakeApp) -> Any:
    register_otel_retention_tools(app)
    return app.tools["akosha_force_retention_sweep"]


@pytest.fixture
async def store() -> HotStore:
    hs = HotStore(database_path=":memory:", embedding_dim=384)
    await hs.initialize()
    return hs


def _insert_expired_row(receiver: OTLPReceiver) -> None:
    """Insert one row well past the 30-day retention horizon."""
    old_ns = time.time_ns() - 60 * 24 * 3600 * 1_000_000_000
    receiver._hot_store.conn.execute(  # type: ignore[union-attr]
        "INSERT INTO spans_substrate "
        "(span_id, trace_id, service_name, service_namespace, span_name, "
        " start_time_unix_nano, received_at_unix_nano, attributes, resource) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, '{}', '{}')",
        ["1" * 16, "2" * 32, "svc", "bodai", "ancient", old_ns, old_ns],
    )


def _row_count(receiver: OTLPReceiver) -> int:
    row = receiver._hot_store.conn.execute(  # type: ignore[union-attr]
        "SELECT COUNT(*) FROM spans_substrate"
    ).fetchone()
    return int(row[0])


class TestRegistration:
    """The tool is registered under the name the runbook will use."""

    def test_it_registers_under_the_documented_name(self, app: _FakeApp) -> None:
        register_otel_retention_tools(app)
        assert "akosha_force_retention_sweep" in app.tools
        assert callable(app.tools["akosha_force_retention_sweep"])

    def test_the_profile_map_lists_it(self) -> None:
        """The tool must be in the group's tool list, or the profile map drifts.

        ``REGISTRATION_TOOLS`` is what the profile machinery and the
        documentation read to know what a group exposes.
        """
        from akosha.mcp.tools.profiles import REGISTRATION_TOOLS

        assert "akosha_force_retention_sweep" in REGISTRATION_TOOLS["register_otel_span_tools"]


class TestTheToolSignals:
    """The tool asks; the loop does the work."""

    async def test_it_signals_the_receiver(
        self, tool: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        receiver = MagicMock(name="receiver")
        receiver.request_retention_sweep.return_value = True
        receiver.sweep_interval_s = 21600
        monkeypatch.setattr("akosha.mcp.server.get_shared_otel_receiver", lambda: receiver)

        result = await tool()

        receiver.request_retention_sweep.assert_called_once_with()
        assert result["requested"] is True
        assert result["already_pending"] is False
        assert result["interval_s"] == 21600

    async def test_it_reports_an_already_pending_request(
        self, tool: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second ask while one is pending does not queue a second sweep."""
        receiver = MagicMock(name="receiver")
        receiver.request_retention_sweep.return_value = False
        receiver.sweep_interval_s = 21600
        monkeypatch.setattr("akosha.mcp.server.get_shared_otel_receiver", lambda: receiver)

        result = await tool()

        assert result["requested"] is True
        assert result["already_pending"] is True

    async def test_it_names_the_gauge_that_confirms_the_sweep(
        self, tool: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The operator needs to know where to look for proof it landed."""
        receiver = MagicMock(name="receiver")
        receiver.request_retention_sweep.return_value = True
        receiver.sweep_interval_s = 21600
        monkeypatch.setattr("akosha.mcp.server.get_shared_otel_receiver", lambda: receiver)

        result = await tool()

        assert result["watch"] == "akosha_otlp_sweep_last_completed_unix_nano"


class TestNoReceiver:
    """With no receiver there is no loop to wake. Say so."""

    async def test_it_reports_that_the_receiver_is_not_running(
        self, tool: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("akosha.mcp.server.get_shared_otel_receiver", lambda: None)

        result = await tool()

        assert result["requested"] is False
        assert result["reason"] == "receiver_not_running"
        assert "detail" in result, "the operator needs to know what to check"


class TestTheToolDoesNotDelete:
    """The safety property at the tool boundary."""

    async def test_calling_the_tool_leaves_the_rows_alone(
        self, tool: Any, store: HotStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The tool must not remove a row even when one is eligible.

        The sweep loop is the sole writer. If the tool deleted too, an
        operator call and the loop's own cycle would race on one DuckDB
        connection.
        """
        receiver = OTLPReceiver(store, grpc_port=0, http_port=0)
        _insert_expired_row(receiver)
        assert _row_count(receiver) == 1

        monkeypatch.setattr("akosha.mcp.server.get_shared_otel_receiver", lambda: receiver)

        result = await tool()

        assert result["requested"] is True
        assert _row_count(receiver) == 1, (
            "the tool must signal only; the sweep loop owns the DELETE"
        )
