"""Tests for akosha/mcp/tools/pycharm_tools.py — CircuitBreakerState, SearchResult, PyCharmMCPAdapter."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from akosha.mcp.tools.pycharm_tools import (
    CircuitBreakerState,
    PyCharmMCPAdapter,
    SearchResult,
    get_pycharm_adapter,
    register_pycharm_tools,
)

# ---------------------------------------------------------------------------
# CircuitBreakerState
# ---------------------------------------------------------------------------


class TestCircuitBreakerState:
    def test_initial_state(self):
        cb = CircuitBreakerState()
        assert cb.failure_count == 0
        assert cb.is_open is False
        assert cb.failure_threshold == 3
        assert cb.recovery_timeout == 60.0

    def test_record_failure_increments(self):
        cb = CircuitBreakerState()
        cb.record_failure()
        assert cb.failure_count == 1

    def test_record_failure_updates_time(self):
        cb = CircuitBreakerState()
        before = time.time()
        cb.record_failure()
        assert cb.last_failure_time >= before

    def test_record_failure_opens_at_threshold(self):
        cb = CircuitBreakerState(failure_threshold=3)
        cb.record_failure()
        assert not cb.is_open
        cb.record_failure()
        assert not cb.is_open
        cb.record_failure()
        assert cb.is_open is True

    def test_record_success_resets(self):
        cb = CircuitBreakerState()
        cb.record_failure()
        cb.record_failure()
        assert cb.failure_count == 2
        cb.record_success()
        assert cb.failure_count == 0
        assert cb.is_open is False

    def test_record_success_closes_circuit(self):
        cb = CircuitBreakerState(failure_threshold=2)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open
        cb.record_success()
        assert not cb.is_open

    def test_can_execute_when_closed(self):
        cb = CircuitBreakerState()
        assert cb.can_execute() is True

    def test_can_execute_when_open_and_not_recovered(self):
        cb = CircuitBreakerState(failure_threshold=1)
        cb.record_failure()
        assert cb.is_open
        assert cb.can_execute() is False

    def test_can_execute_half_open_after_timeout(self):
        cb = CircuitBreakerState(failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()
        assert cb.is_open
        time.sleep(0.02)
        assert cb.can_execute() is True


# ---------------------------------------------------------------------------
# SearchResult
# ---------------------------------------------------------------------------


class TestSearchResult:
    def test_defaults(self):
        sr = SearchResult(file_path="test.py", line_number=10, column=5, match_text="def foo")
        assert sr.file_path == "test.py"
        assert sr.line_number == 10
        assert sr.column == 5
        assert sr.match_text == "def foo"
        assert sr.repo_path is None
        assert sr.context_before is None
        assert sr.context_after is None

    def test_with_optional_fields(self):
        sr = SearchResult(
            file_path="test.py",
            line_number=10,
            column=5,
            match_text="def foo",
            repo_path="/repo",
            context_before="prev",
            context_after="next",
        )
        assert sr.repo_path == "/repo"
        assert sr.context_before == "prev"
        assert sr.context_after == "next"


# ---------------------------------------------------------------------------
# PyCharmMCPAdapter init
# ---------------------------------------------------------------------------


class TestPyCharmMCPAdapterInit:
    def test_init_with_mcp(self):
        mcp = AsyncMock()
        adapter = PyCharmMCPAdapter(mcp_client=mcp)
        assert adapter._available is True
        assert adapter._mcp is mcp

    def test_init_without_mcp(self):
        adapter = PyCharmMCPAdapter(mcp_client=None)
        assert adapter._available is False

    def test_init_custom_timeout(self):
        adapter = PyCharmMCPAdapter(timeout=60.0)
        assert adapter._timeout == 60.0

    def test_init_custom_max_results(self):
        adapter = PyCharmMCPAdapter(max_results=50)
        assert adapter._max_results == 50

    def test_init_empty_cache(self):
        adapter = PyCharmMCPAdapter()
        assert adapter._cache == {}
        assert adapter._cache_ttl == {}


# ---------------------------------------------------------------------------
# _sanitize_regex
# ---------------------------------------------------------------------------


class TestSanitizeRegex:
    def test_valid_pattern(self):
        adapter = PyCharmMCPAdapter()
        assert adapter._sanitize_regex(r"def\s+\w+") == r"def\s+\w+"

    def test_rejects_dangerous_nested_star(self):
        adapter = PyCharmMCPAdapter()
        result = adapter._sanitize_regex("(.*))+")
        assert result == ""

    def test_rejects_dangerous_nested_plus(self):
        adapter = PyCharmMCPAdapter()
        result = adapter._sanitize_regex("(.+)+")
        assert result == ""

    def test_rejects_dangerous_star_star(self):
        adapter = PyCharmMCPAdapter()
        result = adapter._sanitize_regex("(.*)*")
        assert result == ""

    def test_rejects_dangerous_plus_star(self):
        adapter = PyCharmMCPAdapter()
        result = adapter._sanitize_regex("(.+)*")
        assert result == ""

    def test_rejects_dangerous_star_brace(self):
        adapter = PyCharmMCPAdapter()
        result = adapter._sanitize_regex("(.*){100}")
        assert result == ""

    def test_rejects_dangerous_plus_brace(self):
        adapter = PyCharmMCPAdapter()
        result = adapter._sanitize_regex("(.+){100}")
        assert result == ""

    def test_rejects_very_long_pattern(self):
        adapter = PyCharmMCPAdapter()
        result = adapter._sanitize_regex("a" * 501)
        assert result == ""

    def test_rejects_invalid_regex(self):
        adapter = PyCharmMCPAdapter()
        result = adapter._sanitize_regex("[invalid")
        assert result == ""

    def test_simple_pattern_passes(self):
        adapter = PyCharmMCPAdapter()
        # (a+)* is NOT in the dangerous patterns list (those use .* and .+)
        # So this should pass
        result = adapter._sanitize_regex("(a+)*")
        assert result == "(a+)*"

    def test_backreference_passes(self):
        adapter = PyCharmMCPAdapter()
        # Backreferences are not in the dangerous patterns list
        result = adapter._sanitize_regex(r"(a)\1")
        assert result == r"(a)\1"


# ---------------------------------------------------------------------------
# _is_safe_path
# ---------------------------------------------------------------------------


class TestIsSafePath:
    def test_safe_path(self):
        adapter = PyCharmMCPAdapter()
        assert adapter._is_safe_path("/home/user/file.py") is True

    def test_empty_path(self):
        adapter = PyCharmMCPAdapter()
        assert adapter._is_safe_path("") is False

    def test_path_traversal(self):
        adapter = PyCharmMCPAdapter()
        assert adapter._is_safe_path("../../etc/passwd") is False

    def test_null_byte(self):
        adapter = PyCharmMCPAdapter()
        assert adapter._is_safe_path("/safe/path\x00danger") is False

    def test_relative_path_allowed(self):
        adapter = PyCharmMCPAdapter()
        assert adapter._is_safe_path("relative/path.py") is True

    def test_dotdot_midpath(self):
        adapter = PyCharmMCPAdapter()
        assert adapter._is_safe_path("/safe/../etc/passwd") is False


# ---------------------------------------------------------------------------
# _get_cached / _set_cached
# ---------------------------------------------------------------------------


class TestCaching:
    @pytest.mark.asyncio
    async def test_cache_miss(self):
        adapter = PyCharmMCPAdapter()
        assert adapter._get_cached("missing") is None

    @pytest.mark.asyncio
    async def test_cache_hit(self):
        adapter = PyCharmMCPAdapter()
        adapter._set_cached("key", [1, 2, 3], ttl=60.0)
        assert adapter._get_cached("key") == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_cache_expired(self):
        adapter = PyCharmMCPAdapter()
        adapter._set_cached("key", "value", ttl=0.01)
        time.sleep(0.02)
        assert adapter._get_cached("key") is None

    @pytest.mark.asyncio
    async def test_cache_clear(self):
        adapter = PyCharmMCPAdapter()
        adapter._set_cached("key", "value", ttl=60.0)
        adapter.clear_cache()
        assert adapter._get_cached("key") is None


# ---------------------------------------------------------------------------
# _execute_with_circuit_breaker
# ---------------------------------------------------------------------------


class TestExecuteWithCircuitBreaker:
    @pytest.mark.asyncio
    async def test_success(self):
        adapter = PyCharmMCPAdapter()

        async def impl():
            return "ok"

        result = await adapter._execute_with_circuit_breaker(impl)
        assert result == "ok"
        assert adapter._circuit_breaker.failure_count == 0

    @pytest.mark.asyncio
    async def test_failure_raises(self):
        adapter = PyCharmMCPAdapter()

        async def impl():
            raise RuntimeError("fail")

        with pytest.raises(RuntimeError):
            await adapter._execute_with_circuit_breaker(impl)
        assert adapter._circuit_breaker.failure_count == 1

    @pytest.mark.asyncio
    async def test_open_circuit_returns_empty_list(self):
        adapter = PyCharmMCPAdapter()
        adapter._circuit_breaker.is_open = True
        adapter._circuit_breaker.last_failure_time = time.time()

        async def impl():
            return "should not run"

        result = await adapter._execute_with_circuit_breaker(impl)
        assert result == []


# ---------------------------------------------------------------------------
# search_regex
# ---------------------------------------------------------------------------


class TestSearchRegex:
    @pytest.mark.asyncio
    async def test_invalid_pattern(self):
        adapter = PyCharmMCPAdapter()
        result = await adapter.search_regex("(invalid", scope="all")
        assert result == []

    @pytest.mark.asyncio
    async def test_no_mcp(self):
        adapter = PyCharmMCPAdapter(mcp_client=None)
        result = await adapter.search_regex("test", scope="all")
        assert result == []

    @pytest.mark.asyncio
    async def test_with_mcp(self):
        mcp = AsyncMock()
        mcp.search_regex = AsyncMock(
            return_value=[
                {
                    "file_path": "test.py",
                    "line": 10,
                    "column": 5,
                    "match": "def foo()",
                    "context_before": "prev",
                    "context_after": "next",
                }
            ]
        )
        adapter = PyCharmMCPAdapter(mcp_client=mcp)
        results = await adapter.search_regex("def foo", scope="all")
        assert len(results) == 1
        assert isinstance(results[0], SearchResult)
        assert results[0].file_path == "test.py"
        assert results[0].match_text == "def foo()"

    @pytest.mark.asyncio
    async def test_max_results(self):
        mcp = AsyncMock()
        mcp.search_regex = AsyncMock(
            return_value=[{"file_path": f"f{i}.py", "line": i, "match": "x"} for i in range(200)]
        )
        adapter = PyCharmMCPAdapter(mcp_client=mcp, max_results=5)
        results = await adapter.search_regex("test", scope="all")
        assert len(results) == 5

    @pytest.mark.asyncio
    async def test_mcp_error(self):
        mcp = AsyncMock()
        mcp.search_regex = AsyncMock(side_effect=TimeoutError("slow"))
        adapter = PyCharmMCPAdapter(mcp_client=mcp)
        results = await adapter.search_regex("test", scope="all")
        assert results == []

    @pytest.mark.asyncio
    async def test_mcp_generic_exception(self):
        mcp = AsyncMock()
        mcp.search_regex = AsyncMock(side_effect=ValueError("bad"))
        adapter = PyCharmMCPAdapter(mcp_client=mcp)
        results = await adapter.search_regex("test", scope="all")
        assert results == []

    @pytest.mark.asyncio
    async def test_caching(self):
        mcp = AsyncMock()
        mcp.search_regex = AsyncMock(return_value=[{"file_path": "f.py", "line": 1, "match": "x"}])
        adapter = PyCharmMCPAdapter(mcp_client=mcp)
        r1 = await adapter.search_regex("test")
        r2 = await adapter.search_regex("test")
        assert len(r1) == 1
        assert mcp.search_regex.call_count == 1  # second call hits cache


# ---------------------------------------------------------------------------
# get_file_problems
# ---------------------------------------------------------------------------


class TestGetFileProblems:
    @pytest.mark.asyncio
    async def test_unsafe_path(self):
        adapter = PyCharmMCPAdapter()
        result = await adapter.get_file_problems("../../etc/passwd")
        assert result == []

    @pytest.mark.asyncio
    async def test_empty_path(self):
        adapter = PyCharmMCPAdapter()
        result = await adapter.get_file_problems("")
        assert result == []

    @pytest.mark.asyncio
    async def test_no_mcp(self):
        adapter = PyCharmMCPAdapter(mcp_client=None)
        result = await adapter.get_file_problems("test.py")
        assert result == []

    @pytest.mark.asyncio
    async def test_with_mcp(self):
        mcp = AsyncMock()
        mcp.get_file_problems = AsyncMock(
            return_value=[
                {"file": "test.py", "line": 5, "severity": "WARNING", "message": "unused"}
            ]
        )
        adapter = PyCharmMCPAdapter(mcp_client=mcp)
        problems = await adapter.get_file_problems("test.py")
        assert len(problems) == 1
        assert problems[0]["severity"] == "WARNING"

    @pytest.mark.asyncio
    async def test_errors_only_flag(self):
        mcp = AsyncMock()
        mcp.get_file_problems = AsyncMock(
            return_value=[
                {"file": "test.py", "line": 5, "severity": "WARNING", "message": "unused"}
            ]
        )
        adapter = PyCharmMCPAdapter(mcp_client=mcp)
        problems = await adapter.get_file_problems("test.py", errors_only=True)
        assert len(problems) == 1

    @pytest.mark.asyncio
    async def test_mcp_error(self):
        mcp = AsyncMock()
        mcp.get_file_problems = AsyncMock(side_effect=RuntimeError("err"))
        adapter = PyCharmMCPAdapter(mcp_client=mcp)
        problems = await adapter.get_file_problems("test.py")
        assert problems == []

    @pytest.mark.asyncio
    async def test_none_return(self):
        mcp = AsyncMock()
        mcp.get_file_problems = AsyncMock(return_value=None)
        adapter = PyCharmMCPAdapter(mcp_client=mcp)
        problems = await adapter.get_file_problems("test.py")
        assert problems == []


# ---------------------------------------------------------------------------
# find_usages
# ---------------------------------------------------------------------------


class TestFindUsages:
    @pytest.mark.asyncio
    async def test_no_mcp(self):
        adapter = PyCharmMCPAdapter(mcp_client=None)
        result = await adapter.find_usages("my_func")
        assert result == []

    @pytest.mark.asyncio
    async def test_with_mcp(self):
        mcp = AsyncMock()
        mcp.find_usages = AsyncMock(
            return_value=[{"file": "a.py", "line": 3, "context": "my_func()"}]
        )
        adapter = PyCharmMCPAdapter(mcp_client=mcp)
        usages = await adapter.find_usages("my_func")
        assert len(usages) == 1

    @pytest.mark.asyncio
    async def test_file_path_filter(self):
        mcp = AsyncMock()
        mcp.find_usages = AsyncMock(
            return_value=[{"file": "a.py", "line": 3, "context": "my_func()"}]
        )
        adapter = PyCharmMCPAdapter(mcp_client=mcp)
        usages = await adapter.find_usages("my_func", file_path="a.py")
        assert len(usages) == 1

    @pytest.mark.asyncio
    async def test_mcp_error(self):
        mcp = AsyncMock()
        mcp.find_usages = AsyncMock(side_effect=ConnectionError("err"))
        adapter = PyCharmMCPAdapter(mcp_client=mcp)
        usages = await adapter.find_usages("my_func")
        assert usages == []

    @pytest.mark.asyncio
    async def test_none_return(self):
        mcp = AsyncMock()
        mcp.find_usages = AsyncMock(return_value=None)
        adapter = PyCharmMCPAdapter(mcp_client=mcp)
        usages = await adapter.find_usages("my_func")
        assert usages == []

    @pytest.mark.asyncio
    async def test_caching(self):
        mcp = AsyncMock()
        mcp.find_usages = AsyncMock(return_value=[{"file": "a.py", "line": 1}])
        adapter = PyCharmMCPAdapter(mcp_client=mcp)
        r1 = await adapter.find_usages("func")
        r2 = await adapter.find_usages("func")
        assert mcp.find_usages.call_count == 1


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_no_mcp(self):
        adapter = PyCharmMCPAdapter(mcp_client=None)
        result = await adapter.health_check()
        assert result["mcp_available"] is False
        assert result["circuit_breaker_open"] is False
        assert result["failure_count"] == 0
        assert result["cache_size"] == 0

    @pytest.mark.asyncio
    async def test_with_mcp(self):
        mcp = AsyncMock()
        adapter = PyCharmMCPAdapter(mcp_client=mcp)
        result = await adapter.health_check()
        assert result["mcp_available"] is True
        assert result["cache_size"] == 0

    @pytest.mark.asyncio
    async def test_open_circuit(self):
        adapter = PyCharmMCPAdapter(mcp_client=AsyncMock())
        adapter._circuit_breaker.is_open = True
        adapter._circuit_breaker.last_failure_time = time.time()
        result = await adapter.health_check()
        assert result["mcp_available"] is True
        assert result["circuit_breaker_open"] is True

    @pytest.mark.asyncio
    async def test_with_cache_entries(self):
        adapter = PyCharmMCPAdapter(mcp_client=AsyncMock())
        adapter._set_cached("test", "val", ttl=60.0)
        result = await adapter.health_check()
        assert result["cache_size"] == 1


# ---------------------------------------------------------------------------
# get_pycharm_adapter (module-level singleton)
# ---------------------------------------------------------------------------


class TestGetPyCharmAdapter:
    def test_returns_adapter(self):
        import akosha.mcp.tools.pycharm_tools as mod

        old = mod._pycharm_adapter
        mod._pycharm_adapter = None
        adapter = get_pycharm_adapter()
        assert isinstance(adapter, PyCharmMCPAdapter)
        mod._pycharm_adapter = old

    def test_singleton(self):
        import akosha.mcp.tools.pycharm_tools as mod

        old = mod._pycharm_adapter
        mod._pycharm_adapter = None
        a1 = get_pycharm_adapter()
        a2 = get_pycharm_adapter()
        assert a1 is a2
        mod._pycharm_adapter = old


# ---------------------------------------------------------------------------
# register_pycharm_tools
# ---------------------------------------------------------------------------


class TestRegisterPyCharmTools:
    def test_invalid_registry(self):
        import akosha.mcp.tools.pycharm_tools as mod

        old = mod._pycharm_adapter
        mod._pycharm_adapter = None
        register_pycharm_tools(MagicMock(), MagicMock())
        mod._pycharm_adapter = old

    def test_registers_5_tools(self):
        import akosha.mcp.tools.pycharm_tools as mod
        from akosha.mcp.tools.tool_registry import FastMCPToolRegistry

        old = mod._pycharm_adapter
        mod._pycharm_adapter = None
        app = MagicMock()

        def tool_decorator(*args, **kwargs):
            def deco(func):
                return func

            return deco

        app.tool = tool_decorator
        registry = FastMCPToolRegistry(app)
        registry.app = registry._app

        hot_store = AsyncMock()
        register_pycharm_tools(registry, hot_store)

        tools = registry.tools
        assert "search_code_patterns" in tools
        assert "get_code_problems" in tools
        assert "find_function_usage" in tools
        assert "analyze_imports" in tools
        assert "pycharm_health" in tools
        mod._pycharm_adapter = old


def _make_tool_registry():
    """Create registry and hot_store for tool function testing."""
    import akosha.mcp.tools.pycharm_tools as mod
    from akosha.mcp.tools.tool_registry import FastMCPToolRegistry

    old = mod._pycharm_adapter
    mod._pycharm_adapter = None

    app = MagicMock()
    captured = []

    def tool_decorator(*args, **kwargs):
        def deco(func):
            captured.append(func)
            return func

        return deco

    app.tool = tool_decorator
    registry = FastMCPToolRegistry(app)
    registry.app = registry._app

    hot_store = AsyncMock()
    hot_store.list_code_graphs = AsyncMock(return_value=[])

    register_pycharm_tools(registry, hot_store)

    mod._pycharm_adapter = old
    return registry, captured, hot_store


class TestSearchCodePatternsTool:
    @pytest.mark.asyncio
    async def test_no_results(self):
        registry, captured, hot_store = _make_tool_registry()
        tool = captured[0]
        result = await tool(pattern="nonexistent", limit=10)
        assert result["status"] == "success"
        assert result["count"] == 0
        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_with_code_graph_results(self):
        registry, captured, hot_store = _make_tool_registry()
        hot_store.list_code_graphs = AsyncMock(
            return_value=[
                {"repo_path": "/repo1", "commit_hash": "abc", "nodes_count": 1},
            ]
        )
        hot_store.get_code_graph = AsyncMock(
            return_value={
                "repo_path": "/repo1",
                "graph_data": {
                    "nodes": {
                        "n1": {
                            "type": "function",
                            "source": "def my_func(): pass",
                            "file_path": "a.py",
                            "start_line": 10,
                        },
                        "n2": {"type": "class", "source": "class Foo: pass"},
                    }
                },
            }
        )
        tool = captured[0]
        result = await tool(pattern="my_func")
        assert result["status"] == "success"
        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_graph_missing_data_is_ignored(self):
        registry, captured, hot_store = _make_tool_registry()
        hot_store.list_code_graphs = AsyncMock(
            return_value=[
                {"repo_path": "/repo1", "commit_hash": "abc", "nodes_count": 1},
                {"repo_path": "/repo2", "commit_hash": "def", "nodes_count": 1},
            ]
        )
        hot_store.get_code_graph = AsyncMock(
            side_effect=[
                None,
                {"repo_path": "/repo2"},
            ]
        )
        tool = captured[0]
        result = await tool(pattern="does_not_match")
        assert result["status"] == "success"
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_code_graph_no_nodes(self):
        registry, captured, hot_store = _make_tool_registry()
        hot_store.list_code_graphs = AsyncMock(
            return_value=[
                {"repo_path": "/repo1", "commit_hash": "abc"},
            ]
        )
        hot_store.get_code_graph = AsyncMock(
            return_value={
                "graph_data": {"nodes": {}},
            }
        )
        tool = captured[0]
        result = await tool(pattern="test")
        assert result["status"] == "success"
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_exception_returns_error(self):
        registry, captured, hot_store = _make_tool_registry()
        hot_store.list_code_graphs = AsyncMock(side_effect=RuntimeError("fail"))
        tool = captured[0]
        result = await tool(pattern="test")
        assert result["status"] == "error"
        assert "fail" in result["message"]


class TestGetCodeProblemsTool:
    @pytest.mark.asyncio
    async def test_no_results(self):
        registry, captured, hot_store = _make_tool_registry()
        tool = captured[1]
        result = await tool(severity="ERROR")
        assert result["status"] == "success"
        assert result["count"] == 0
        assert result["problems"] == []

    @pytest.mark.asyncio
    async def test_with_problems(self):
        registry, captured, hot_store = _make_tool_registry()
        hot_store.list_code_graphs = AsyncMock(
            return_value=[
                {"repo_path": "/repo1", "commit_hash": "abc"},
            ]
        )
        hot_store.get_code_graph = AsyncMock(
            return_value={
                "graph_data": {
                    "nodes": {
                        "n1": {
                            "type": "function",
                            "file_path": "a.py",
                            "start_line": 5,
                            "problems": [
                                {"severity": "ERROR", "message": "type error", "category": "TYPE"},
                                {"severity": "WARNING", "message": "unused", "category": "STYLE"},
                            ],
                        },
                    }
                },
            }
        )
        tool = captured[1]
        result = await tool(severity="ERROR")
        assert result["status"] == "success"
        assert result["count"] == 1
        assert result["problems"][0]["severity"] == "ERROR"

    @pytest.mark.asyncio
    async def test_warning_severity_includes_warnings(self):
        registry, captured, hot_store = _make_tool_registry()
        hot_store.list_code_graphs = AsyncMock(
            return_value=[
                {"repo_path": "/repo1", "commit_hash": "abc"},
            ]
        )
        hot_store.get_code_graph = AsyncMock(
            return_value={
                "graph_data": {
                    "nodes": {
                        "n1": {
                            "type": "function",
                            "file_path": "a.py",
                            "start_line": 5,
                            "problems": [
                                {"severity": "WARNING", "message": "unused", "category": "STYLE"},
                            ],
                        },
                    }
                },
            }
        )
        tool = captured[1]
        result = await tool(severity="WARNING")
        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_info_severity_and_non_dict_nodes(self):
        registry, captured, hot_store = _make_tool_registry()
        hot_store.list_code_graphs = AsyncMock(
            return_value=[
                {"repo_path": "/repo1", "commit_hash": "abc"},
            ]
        )
        hot_store.get_code_graph = AsyncMock(
            return_value={
                "graph_data": {
                    "nodes": {
                        "n1": {
                            "type": "function",
                            "file_path": "a.py",
                            "start_line": 5,
                            "problems": [
                                {"severity": "INFO", "message": "note", "category": "STYLE"},
                            ],
                        },
                        "n2": "not-a-dict",
                    }
                },
            }
        )
        tool = captured[1]
        result = await tool(severity="INFO")
        assert result["status"] == "success"
        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_graph_not_found(self):
        registry, captured, hot_store = _make_tool_registry()
        hot_store.list_code_graphs = AsyncMock(
            return_value=[
                {"repo_path": "/repo1", "commit_hash": "abc"},
            ]
        )
        hot_store.get_code_graph = AsyncMock(return_value=None)
        tool = captured[1]
        result = await tool()
        assert result["status"] == "success"
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_repo_filter(self):
        registry, captured, hot_store = _make_tool_registry()
        hot_store.list_code_graphs = AsyncMock(
            return_value=[
                {"repo_path": "/repo1", "commit_hash": "abc"},
            ]
        )
        tool = captured[1]
        result = await tool(repo_path="/repo1")
        hot_store.list_code_graphs.assert_called_once_with(repo_path="/repo1", limit=100)

    @pytest.mark.asyncio
    async def test_exception_returns_error(self):
        registry, captured, hot_store = _make_tool_registry()
        hot_store.list_code_graphs = AsyncMock(side_effect=RuntimeError("fail"))
        tool = captured[1]
        result = await tool()
        assert result["status"] == "error"


class TestFindFunctionUsageTool:
    @pytest.mark.asyncio
    async def test_no_results(self):
        registry, captured, hot_store = _make_tool_registry()
        tool = captured[2]
        result = await tool(function_name="nonexistent")
        assert result["status"] == "success"
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_with_graph_results(self):
        registry, captured, hot_store = _make_tool_registry()
        hot_store.list_code_graphs = AsyncMock(
            return_value=[
                {"repo_path": "/repo1", "commit_hash": "abc"},
            ]
        )
        hot_store.get_code_graph = AsyncMock(
            return_value={
                "graph_data": {
                    "nodes": {
                        "n1": {
                            "type": "function",
                            "name": "my_func",
                            "file_path": "a.py",
                            "start_line": 10,
                        },
                    }
                },
            }
        )
        tool = captured[2]
        result = await tool(function_name="my_func")
        assert result["status"] == "success"
        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_exception_and_missing_match_paths(self):
        registry, captured, hot_store = _make_tool_registry()
        hot_store.list_code_graphs = AsyncMock(
            return_value=[
                {"repo_path": "/repo1", "commit_hash": "abc"},
                {"repo_path": "/repo2", "commit_hash": "def"},
            ]
        )

        async def get_code_graph(repo_path: str, commit_hash: str):
            if repo_path == "/repo1":
                raise RuntimeError("boom")
            return {
                "graph_data": {
                    "nodes": {
                        "n1": {
                            "type": "function",
                            "name": "different",
                            "file_path": "b.py",
                            "start_line": 1,
                        }
                    }
                }
            }

        hot_store.get_code_graph = AsyncMock(side_effect=get_code_graph)
        tool = captured[2]
        result = await tool(function_name="my_func")
        assert result["status"] == "success"
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_exception_returns_error(self):
        registry, captured, hot_store = _make_tool_registry()
        hot_store.list_code_graphs = AsyncMock(side_effect=RuntimeError("fail"))
        tool = captured[2]
        result = await tool(function_name="test")
        assert result["status"] == "error"


class TestAnalyzeImportsTool:
    @pytest.mark.asyncio
    async def test_no_results(self):
        registry, captured, hot_store = _make_tool_registry()
        tool = captured[3]
        result = await tool(analysis_type="unused")
        assert result["status"] == "success"
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_unused_imports(self):
        registry, captured, hot_store = _make_tool_registry()
        hot_store.list_code_graphs = AsyncMock(
            return_value=[
                {"repo_path": "/repo1", "commit_hash": "abc"},
            ]
        )
        hot_store.get_code_graph = AsyncMock(
            return_value={
                "graph_data": {
                    "nodes": {
                        "n1": {"type": "import", "name": "os"},
                        "n2": {"type": "call", "name": "os.path"},
                        "n3": {"type": "function", "name": "main"},
                    }
                },
            }
        )
        tool = captured[3]
        result = await tool(analysis_type="unused")
        assert result["status"] == "success"
        assert result["count"] >= 1
        assert any(r["type"] == "unused_import" for r in result["imports"])

    @pytest.mark.asyncio
    async def test_circular_imports(self):
        registry, captured, hot_store = _make_tool_registry()
        hot_store.list_code_graphs = AsyncMock(
            return_value=[
                {"repo_path": "/repo1", "commit_hash": "abc"},
            ]
        )
        hot_store.get_code_graph = AsyncMock(
            return_value={
                "graph_data": {
                    "nodes": {"n1": {}, "n2": {}},
                    "edges": [
                        {"type": "imports", "source": "a", "target": "b"},
                        {"type": "imports", "source": "b", "target": "a"},
                    ],
                },
            }
        )
        tool = captured[3]
        result = await tool(analysis_type="circular")
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_import_patterns(self):
        registry, captured, hot_store = _make_tool_registry()
        hot_store.list_code_graphs = AsyncMock(
            return_value=[
                {"repo_path": "/repo1", "commit_hash": "abc"},
            ]
        )
        hot_store.get_code_graph = AsyncMock(
            return_value={
                "graph_data": {
                    "nodes": {
                        "n1": {"type": "import", "name": "os"},
                        "n2": {"type": "import", "name": "os"},
                        "n3": {"type": "import", "name": "sys"},
                    }
                },
            }
        )
        tool = captured[3]
        result = await tool(analysis_type="patterns")
        assert result["status"] == "success"
        assert result["count"] >= 1

    @pytest.mark.asyncio
    async def test_unknown_analysis_type_returns_empty(self):
        registry, captured, hot_store = _make_tool_registry()
        hot_store.list_code_graphs = AsyncMock(
            return_value=[
                {"repo_path": "/repo1", "commit_hash": "abc"},
            ]
        )
        hot_store.get_code_graph = AsyncMock(
            return_value={
                "graph_data": {
                    "nodes": {
                        "n1": {"type": "function", "name": "foo"},
                    }
                }
            }
        )
        tool = captured[3]
        result = await tool(analysis_type="custom")
        assert result["status"] == "success"
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_repo_filter(self):
        registry, captured, hot_store = _make_tool_registry()
        hot_store.list_code_graphs = AsyncMock(return_value=[])
        tool = captured[3]
        result = await tool(repo_path="/repo1")
        hot_store.list_code_graphs.assert_called_once_with(repo_path="/repo1", limit=100)

    @pytest.mark.asyncio
    async def test_exception_returns_error(self):
        registry, captured, hot_store = _make_tool_registry()
        hot_store.list_code_graphs = AsyncMock(side_effect=RuntimeError("fail"))
        tool = captured[3]
        result = await tool()
        assert result["status"] == "error"


class TestPyCharmHealthTool:
    @pytest.mark.asyncio
    async def test_success(self):
        registry, captured, hot_store = _make_tool_registry()
        tool = captured[4]
        result = await tool()
        assert result["status"] == "success"
        assert "healthy" in result

    @pytest.mark.asyncio
    async def test_exception(self):
        import akosha.mcp.tools.pycharm_tools as mod
        from akosha.mcp.tools.tool_registry import FastMCPToolRegistry

        old = mod._pycharm_adapter
        mod._pycharm_adapter = None

        app = MagicMock()
        captured = []
        app.tool = lambda *a, **kw: lambda f: (captured.append(f), f)[1]
        registry = FastMCPToolRegistry(app)
        registry.app = registry._app
        hot_store = AsyncMock()

        # Provide a broken adapter so health_check raises
        broken = AsyncMock()
        broken.health_check = AsyncMock(side_effect=RuntimeError("fail"))
        with patch("akosha.mcp.tools.pycharm_tools.get_pycharm_adapter", return_value=broken):
            register_pycharm_tools(registry, hot_store)

        mod._pycharm_adapter = old
        tool = captured[4]
        result = await tool()
        assert result["status"] == "error"


class TestPyCharmToolRuntimeBranches:
    @pytest.mark.asyncio
    async def test_register_and_execute_branches(self):
        import akosha.mcp.tools.pycharm_tools as mod
        from akosha.mcp.tools.tool_registry import FastMCPToolRegistry

        class FakeAdapter:
            _available = True

            async def search_regex(self, pattern: str, file_pattern: str | None = None, scope: str = "all"):
                return [
                    SearchResult(
                        file_path="a.py",
                        line_number=10,
                        column=1,
                        match_text="def foo()",
                        context_before="before",
                        context_after="after",
                    )
                ]

            async def get_file_problems(self, file_path: str, errors_only: bool = False):
                return [{"file": file_path, "line": 1, "severity": "ERROR", "message": "err"}]

            async def find_usages(self, symbol_name: str, file_path: str | None = None):
                return [{"file_path": "b.py", "line": 3, "type": "call"}]

            async def health_check(self):
                return {
                    "mcp_available": True,
                    "circuit_breaker_open": False,
                    "failure_count": 0,
                    "cache_size": 0,
                }

        class FakeApp:
            def __init__(self) -> None:
                self.tools: dict[str, object] = {}

            def tool(self, *args, **kwargs):
                def decorator(func):
                    self.tools[func.__name__] = func
                    return func

                return decorator

        fake_adapter = FakeAdapter()
        hot_store = AsyncMock()
        hot_store.list_code_graphs = AsyncMock(
            return_value=[
                {"repo_path": "/repo1", "commit_hash": "abc"},
                {"repo_path": "/repo2", "commit_hash": "def"},
            ]
        )
        async def get_code_graph(repo_path: str, commit_hash: str):
            if repo_path == "/repo1" and commit_hash == "abc":
                return {
                    "graph_data": {
                        "nodes": {
                            "n1": {
                                "type": "function",
                                "source": "def my_func(): pass",
                                "name": "my_func",
                                "file_path": "graph.py",
                                "start_line": 10,
                                "problems": [
                                    {"severity": "ERROR", "message": "broken", "category": "TYPE"},
                                    {"severity": "WARNING", "message": "warn", "category": "STYLE"},
                                ],
                            },
                            "n2": {
                                "type": "import",
                                "name": "package.foo",
                                "file_path": "import.py",
                                "start_line": 2,
                            },
                            "n3": {
                                "type": "call",
                                "name": "my_func",
                                "file_path": "call.py",
                                "start_line": 5,
                            },
                        },
                        "edges": [
                            {"type": "imports", "source": "a", "target": "b"},
                            {"type": "imports", "source": "b", "target": "a"},
                        ],
                    }
                }
            if repo_path == "/repo2" and commit_hash == "def":
                return None
            return None

        hot_store.get_code_graph = AsyncMock(side_effect=get_code_graph)

        old_adapter = mod._pycharm_adapter
        mod._pycharm_adapter = fake_adapter  # type: ignore[assignment]
        try:
            app = FakeApp()
            registry = FastMCPToolRegistry(app)
            registry.app = registry._app

            register_pycharm_tools(registry, hot_store)

            search = await app.tools["search_code_patterns"](pattern="foo", limit=5)
            problems = await app.tools["get_code_problems"](severity="WARNING", limit=10)
            usages = await app.tools["find_function_usage"](function_name="my_func", limit=10)
            imports_unused = await app.tools["analyze_imports"](analysis_type="unused", limit=10)
            imports_circular = await app.tools["analyze_imports"](analysis_type="circular", limit=10)
            imports_patterns = await app.tools["analyze_imports"](analysis_type="patterns", limit=10)
            health = await app.tools["pycharm_health"]()

            assert search["status"] == "success"
            assert search["count"] == 1
            assert search["results"][0]["source"] == "pycharm_index"

            assert problems["status"] == "success"
            assert problems["count"] == 2

            assert usages["status"] == "success"
            assert usages["count"] == 2

            assert imports_unused["status"] == "success"
            assert imports_unused["count"] >= 1
            assert imports_circular["status"] == "success"
            assert imports_circular["count"] >= 1
            assert imports_patterns["status"] == "success"
            assert imports_patterns["count"] >= 1

            assert health["status"] == "success"
            assert health["healthy"] is True
        finally:
            mod._pycharm_adapter = old_adapter
