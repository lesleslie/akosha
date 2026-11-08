"""Tests for :mod:`akosha.mcp.tools.otel_tools`.

The ``register_otel_query_tools`` function decorates a FastMCP app with a
``query_local_traces`` tool. The tool delegates to ``hot_store.query_traces``
and reshapes the result rows into a smaller dict payload.

Coverage was at 35.71% before these tests. Missing branches:
- The decorator body (when the tool function is invoked)
- The success path (HotStore returns rows → rows reshaped)
- The error path (HotStore raises → empty list returned, exception logged)
- The "Registered OTel trace query tools" log line (called once per registration)
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from akosha.mcp.tools.otel_tools import register_otel_query_tools


class _FakeApp:
    """Minimal FastMCP stand-in: stores tools registered via ``@app.tool()``.

    The real FastMCP ``tool()`` decorator returns the function unchanged and
    attaches metadata. We mimic that just enough for these tests to verify
    that the right tool was registered with the right signature.
    """

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, name: str | None = None) -> Any:
        """Decorator factory matching FastMCP's ``@app.tool()`` shape."""

        def decorator(fn: Any) -> Any:
            key = name if name else fn.__name__
            self.tools[key] = fn
            return fn

        return decorator


class TestRegisterOtelQueryTools:
    """The registration decorator wires up the right tool on the app."""

    def test_registers_query_local_traces_tool(self) -> None:
        """``register_otel_query_tools`` must attach a ``query_local_traces`` callable."""
        app = _FakeApp()
        register_otel_query_tools(app=app, hot_store=MagicMock())
        assert "query_local_traces" in app.tools
        assert callable(app.tools["query_local_traces"])

    def test_registration_logs_info(self, caplog: pytest.LogCaptureFixture) -> None:
        """Successful registration emits an info-level log line."""
        app = _FakeApp()
        with caplog.at_level(logging.INFO, logger="akosha.mcp.tools.otel_tools"):
            register_otel_query_tools(app=app, hot_store=MagicMock())
        assert any("Registered OTel trace query tools" in rec.message for rec in caplog.records)

    def test_registration_works_with_any_hot_store(self) -> None:
        """The hot_store argument is duck-typed; a MagicMock is sufficient for registration."""
        # This pins the contract: the function does not introspect the
        # hot_store at registration time. It is only used when the tool
        # is invoked.
        app = _FakeApp()
        register_otel_query_tools(app=app, hot_store=MagicMock())  # no error
        # The registration must produce a registered tool even when the
        # store is a bare MagicMock (no async surface, no methods set).
        assert "query_local_traces" in app.tools


class TestQueryLocalTracesSuccess:
    """The tool reshapes HotStore rows into the documented payload shape."""

    @pytest.fixture
    def hot_store(self) -> AsyncMock:
        """An AsyncMock hot_store the test class can configure per-test."""
        return AsyncMock()

    @pytest.fixture
    def tool(self, hot_store: AsyncMock) -> Any:
        """Register the tool with the parameterized hot_store and return the inner callable.

        The ``register_otel_query_tools`` function captures ``hot_store`` in
        the tool function's closure, so each test gets a fresh closure over
        its own configured mock.
        """
        app = _FakeApp()
        register_otel_query_tools(app=app, hot_store=hot_store)
        return app.tools["query_local_traces"]

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_hot_store_returns_empty(
        self, tool: Any, hot_store: AsyncMock
    ) -> None:
        """An empty HotStore result must produce an empty list (not raise)."""
        hot_store.query_traces = AsyncMock(return_value=[])
        result = await tool(task_class="code_generation", system_id="akosha")
        assert result == []

    @pytest.mark.asyncio
    async def test_reshapes_row_fields_into_documented_payload(
        self, tool: Any, hot_store: AsyncMock
    ) -> None:
        """Each HotStore row must be projected to ``system_id``/``conversation_id``/``content``/``timestamp``/``metadata``."""
        input_row = {
            "system_id": "akosha",
            "conversation_id": "conv-123",
            "content": "trace payload",
            "timestamp": "2026-09-06T04:00:00Z",
            "metadata": {"trace_id": "abc"},
            # Extra fields must be dropped by the projection.
            "embedding": [0.1, 0.2, 0.3],
            "extra_field": "ignored",
        }
        hot_store.query_traces = AsyncMock(return_value=[input_row])
        result = await tool(task_class="code_generation", system_id="akosha")
        assert len(result) == 1
        row = result[0]
        # ``system_id`` is part of the projection so a row stays attributable
        # to its source system — the caller may have queried every system by
        # omitting the filter.
        assert row == {
            "system_id": "akosha",
            "conversation_id": "conv-123",
            "content": "trace payload",
            "timestamp": "2026-09-06T04:00:00Z",
            "metadata": {"trace_id": "abc"},
        }
        # Confirm we dropped the extra fields (no leakage of embedding/extra).
        assert "embedding" not in row
        assert "extra_field" not in row

    @pytest.mark.asyncio
    async def test_timestamp_defaults_to_empty_string_when_missing(
        self, tool: Any, hot_store: AsyncMock
    ) -> None:
        """A row with no ``timestamp`` field must still produce a valid payload."""
        input_row = {
            "conversation_id": "conv-456",
            "content": "no-timestamp trace",
            # timestamp is absent
            "metadata": {},
        }
        hot_store.query_traces = AsyncMock(return_value=[input_row])
        result = await tool(task_class="code_generation", system_id="akosha")
        assert result[0]["timestamp"] == ""

    @pytest.mark.asyncio
    async def test_metadata_defaults_to_empty_dict_when_missing(
        self, tool: Any, hot_store: AsyncMock
    ) -> None:
        """A row with no ``metadata`` field must default to an empty dict."""
        input_row = {
            "conversation_id": "conv-789",
            "content": "no-metadata trace",
            "timestamp": "2026-09-06T04:00:00Z",
            # metadata is absent
        }
        hot_store.query_traces = AsyncMock(return_value=[input_row])
        result = await tool(task_class="code_generation", system_id="akosha")
        assert result[0]["metadata"] == {}

    @pytest.mark.asyncio
    async def test_propagates_filter_arguments_to_hot_store(
        self, tool: Any, hot_store: AsyncMock
    ) -> None:
        """``start_time``/``end_time``/``task_class``/``limit`` are passed through verbatim."""
        hot_store.query_traces = AsyncMock(return_value=[])
        await tool(
            system_id="mahavishnu",
            start_time="2026-09-01T00:00:00Z",
            end_time="2026-09-06T00:00:00Z",
            task_class="code_review",
            limit=42,
        )
        hot_store.query_traces.assert_awaited_once_with(
            system_id="mahavishnu",
            start_time="2026-09-01T00:00:00Z",
            end_time="2026-09-06T00:00:00Z",
            task_class="code_review",
            limit=42,
        )

    @pytest.mark.asyncio
    async def test_default_limit_is_100(self, tool: Any, hot_store: AsyncMock) -> None:
        """When the caller omits ``limit``, the tool forwards ``limit=100``."""
        hot_store.query_traces = AsyncMock(return_value=[])
        await tool(task_class="code_generation", system_id="akosha")
        call_kwargs = hot_store.query_traces.await_args.kwargs
        assert call_kwargs["limit"] == 100


class TestQueryLocalTracesDryRun:
    """``dry_run`` is a shape switch, never a scope switch.

    This tool is unauthenticated, so the caller's filters are the only scope
    it has. A regression that restores ``dry_run`` disabling them would
    silently widen every query on a public tool — and would pass every other
    test in this file, so it needs its own.
    """

    @pytest.fixture
    def hot_store(self) -> AsyncMock:
        return AsyncMock()

    @pytest.fixture
    def tool(self, hot_store: AsyncMock) -> Any:
        app = _FakeApp()
        register_otel_query_tools(app=app, hot_store=hot_store)
        return app.tools["query_local_traces"]

    @pytest.mark.asyncio
    async def test_dry_run_keeps_filters_and_clamps_limit(
        self, tool: Any, hot_store: AsyncMock
    ) -> None:
        """``dry_run`` changes the output shape; the filters stay in force."""
        hot_store.query_traces = AsyncMock(return_value=[])
        await tool(
            task_class="code_generation",
            system_id="akosha",
            limit=500,
            dry_run=True,
        )
        call_kwargs = hot_store.query_traces.await_args.kwargs
        assert call_kwargs["system_id"] == "akosha"
        assert call_kwargs["task_class"] == "code_generation"
        assert call_kwargs["limit"] == 20

    @pytest.mark.asyncio
    async def test_dry_run_omits_the_bulky_content_blob(
        self, tool: Any, hot_store: AsyncMock
    ) -> None:
        """``dry_run`` drops ``content`` so a wide probe stays readable."""
        hot_store.query_traces = AsyncMock(
            return_value=[
                {
                    "system_id": "akosha",
                    "conversation_id": "conv-1",
                    "content": "the bulky blob",
                    "timestamp": "2026-09-06T04:00:00Z",
                    "metadata": {"trace_id": "abc"},
                }
            ]
        )
        result = await tool(
            task_class="code_generation", system_id="akosha", dry_run=True
        )
        assert len(result) == 1
        assert "content" not in result[0]
        assert result[0]["metadata"] == {"trace_id": "abc"}


class TestQueryLocalTracesError:
    """The tool must swallow HotStore exceptions and return an empty list."""

    @pytest.fixture
    def tool_with_failing_store(self) -> Any:
        """Register the tool with an AsyncMock whose query_traces raises."""
        app = _FakeApp()
        mock_store = AsyncMock(
            query_traces=AsyncMock(side_effect=RuntimeError("db connection lost")),
        )
        register_otel_query_tools(app=app, hot_store=mock_store)
        return app.tools["query_local_traces"]

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_runtime_error(self, tool_with_failing_store: Any) -> None:
        """The tool must not propagate RuntimeError; it returns ``[]`` instead."""
        result = await tool_with_failing_store(task_class="code_generation", system_id="akosha")
        assert result == []

    @pytest.mark.asyncio
    async def test_logs_exception_with_traceback(
        self, tool_with_failing_store: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """On error, the tool logs at exception level so the traceback is captured."""
        with caplog.at_level(logging.ERROR, logger="akosha.mcp.tools.otel_tools"):
            result = await tool_with_failing_store(task_class="code_generation", system_id="akosha")
        assert result == []
        assert any(rec.levelno == logging.ERROR for rec in caplog.records)
        assert any("Error querying traces" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_swallows_value_error(self) -> None:
        """Non-RuntimeError exceptions (e.g. ValueError) are also swallowed."""
        app = _FakeApp()
        mock_store = AsyncMock(
            query_traces=AsyncMock(side_effect=ValueError("bad input")),
        )
        register_otel_query_tools(app=app, hot_store=mock_store)
        tool = app.tools["query_local_traces"]
        result = await tool(task_class="code_generation", system_id="akosha")
        assert result == []
