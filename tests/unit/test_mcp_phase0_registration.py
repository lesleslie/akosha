"""Unit tests for Akosha Phase 0 self-registration to Dhara.

Uses the same patching strategy as test_mcp_server_lifespan.py: all heavy
dependencies are patched before importing from akosha.mcp.server.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def patched_server_module(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch all dependencies before importing akosha.mcp.server functions."""
    # Patch FastMCP so module-level import doesn't trigger real server init
    mock_mcp_server = MagicMock()
    mock_mcp_server.lifespan = None
    mock_fsmcp_instance = MagicMock()
    mock_fsmcp_instance._mcp_server = mock_mcp_server

    def mock_fastmcp(name: str, version: str | None = None, lifespan: Any = None) -> MagicMock:
        return mock_fsmcp_instance

    monkeypatch.setattr("akosha.mcp.server.FastMCP", mock_fastmcp)

    # Patch optional dependency checks
    monkeypatch.setattr("akosha.mcp.server.MCP_COMMON_AVAILABLE", False)
    monkeypatch.setattr("akosha.mcp.server.RATE_LIMITING_AVAILABLE", False)
    monkeypatch.setattr("akosha.mcp.server.SERVERPANELS_AVAILABLE", False)

    # Patch telemetry so lifespan doesn't try to init OTel
    mock_tracer = MagicMock()
    mock_meter = MagicMock()
    monkeypatch.setattr(
        "akosha.observability.setup_telemetry",
        lambda **kwargs: (mock_tracer, mock_meter),
    )
    monkeypatch.setattr("akosha.observability.shutdown_telemetry", MagicMock())

    # Patch hot_store so lifespan doesn't init DuckDB
    mock_hot_store = MagicMock()
    mock_hot_store.initialize = AsyncMock()
    monkeypatch.setattr("akosha.storage.hot_store.HotStore", lambda database_path: mock_hot_store)

    # Patch analytics/graph/embeddings
    mock_analytics = MagicMock()
    mock_graph = MagicMock()
    mock_embedding = MagicMock()
    mock_embedding.initialize = AsyncMock()
    mock_embedding.is_available = MagicMock(return_value=True)
    monkeypatch.setattr(
        "akosha.processing.embeddings.get_embedding_service", lambda: mock_embedding
    )
    monkeypatch.setattr("akosha.processing.analytics.TimeSeriesAnalytics", lambda: mock_analytics)
    monkeypatch.setattr(
        "akosha.processing.knowledge_graph.KnowledgeGraphBuilder", lambda: mock_graph
    )
    monkeypatch.setattr("akosha.mcp.tools.register_all_tools", MagicMock())
    monkeypatch.setattr("akosha.mcp.auth.validate_auth_config", lambda: True)

    # Now safe to import
    from akosha.mcp.server import (
        DHARA_DEFAULT_URL,
        _heartbeat_task,
        _register_component_to_dhara,
        _register_to_dhara_once,
    )

    return {
        "_register_to_dhara_once": _register_to_dhara_once,
        "_register_component_to_dhara": _register_component_to_dhara,
        "_heartbeat_task": _heartbeat_task,
        "DHARA_DEFAULT_URL": DHARA_DEFAULT_URL,
    }


class TestRegisterToDharaOnce:
    """Tests for _register_to_dhara_once function."""

    @pytest.mark.asyncio
    async def test_returns_true_on_success(self, patched_server_module: dict[str, Any]) -> None:
        """Should return True when Dhara responds successfully."""
        _register_to_dhara_once = patched_server_module["_register_to_dhara_once"]

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_instance = MagicMock()
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_instance.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_instance

            result = await _register_to_dhara_once(
                "http://localhost:8683",
                "component_endpoint/akosha",
                "http://localhost:8682/mcp",
            )

            assert result is True
            mock_instance.post.assert_called_once()
            call_args = mock_instance.post.call_args
            assert call_args[0][0] == "http://localhost:8683/tools/call"
            assert call_args[1]["json"] == {
                "name": "put",
                "arguments": {
                    "key": "component_endpoint/akosha",
                    "value": "http://localhost:8682/mcp",
                },
            }

    @pytest.mark.asyncio
    async def test_returns_false_on_http_error(self, patched_server_module: dict[str, Any]) -> None:
        """Should return False when Dhara returns an HTTP error."""
        import httpx

        _register_to_dhara_once = patched_server_module["_register_to_dhara_once"]

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_instance = MagicMock()
            mock_instance.__aenter__ = AsyncMock(side_effect=httpx.HTTPError("connection refused"))
            mock_client_cls.return_value = mock_instance

            result = await _register_to_dhara_once(
                "http://localhost:8683",
                "component_endpoint/akosha",
                "http://localhost:8682/mcp",
            )

            assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self, patched_server_module: dict[str, Any]) -> None:
        """Should return False on any other exception."""
        _register_to_dhara_once = patched_server_module["_register_to_dhara_once"]

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_instance = MagicMock()
            mock_instance.__aenter__ = AsyncMock(side_effect=OSError("unexpected"))
            mock_client_cls.return_value = mock_instance

            result = await _register_to_dhara_once(
                "http://localhost:8683",
                "component_endpoint/akosha",
                "http://localhost:8682/mcp",
            )

            assert result is False


class TestRegisterComponentToDhara:
    """Tests for _register_component_to_dhara function."""

    @pytest.mark.asyncio
    async def test_registers_on_first_attempt(
        self,
        patched_server_module: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Should register on first attempt and start heartbeat."""

        _register_component_to_dhara = patched_server_module["_register_component_to_dhara"]

        monkeypatch.setenv("DHARA_MCP_URL", "http://localhost:8683")

        attempt_count = 0

        async def mock_register_once(*args: object, **kwargs: object) -> bool:
            nonlocal attempt_count
            attempt_count += 1
            return True

        with patch(
            "akosha.mcp.server._register_to_dhara_once",
            new_callable=AsyncMock,
            side_effect=mock_register_once,
        ):
            import akosha.mcp.server as server_module

            server_module._heartbeat_task = None

            await _register_component_to_dhara("http://localhost:8682/mcp")

            assert attempt_count == 1

    @pytest.mark.asyncio
    async def test_retries_with_exponential_backoff(
        self,
        patched_server_module: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Should retry with exponential backoff on failure."""
        import asyncio

        _register_component_to_dhara = patched_server_module["_register_component_to_dhara"]

        monkeypatch.setenv("DHARA_MCP_URL", "http://localhost:8683")
        monkeypatch.setattr(asyncio, "create_task", AsyncMock())

        attempt_count = 0
        sleep_intervals: list[float] = []

        async def mock_register_once(*args: object, **kwargs: object) -> bool:
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 3:
                return False
            return True

        async def mock_sleep(delay: float) -> None:
            sleep_intervals.append(delay)

        with (
            patch(
                "akosha.mcp.server._register_to_dhara_once",
                new_callable=AsyncMock,
                side_effect=mock_register_once,
            ),
            patch("asyncio.sleep", new_callable=AsyncMock, side_effect=mock_sleep),
        ):
            import akosha.mcp.server as server_module

            server_module._heartbeat_task = None

            await _register_component_to_dhara("http://localhost:8682/mcp")

            assert attempt_count == 3
            assert sleep_intervals[0] == 1.0
            assert sleep_intervals[1] == 2.0

    @pytest.mark.asyncio
    async def test_uses_env_var_for_dhara_url(
        self,
        patched_server_module: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Should use DHARA_MCP_URL env var when set."""
        import asyncio

        _register_component_to_dhara = patched_server_module["_register_component_to_dhara"]

        monkeypatch.setenv("DHARA_MCP_URL", "http://custom-dhara:9999")
        monkeypatch.setattr(asyncio, "create_task", AsyncMock())

        captured_urls: list[str] = []

        async def mock_register_once(dhara_url: str, key: str, mcp_url: str) -> bool:
            captured_urls.append(dhara_url)
            return True

        with patch(
            "akosha.mcp.server._register_to_dhara_once",
            new_callable=AsyncMock,
            side_effect=mock_register_once,
        ):
            import akosha.mcp.server as server_module

            server_module._heartbeat_task = None

            await _register_component_to_dhara("http://localhost:8682/mcp")

            assert captured_urls[0] == "http://custom-dhara:9999"

    @pytest.mark.asyncio
    async def test_uses_default_url_when_env_not_set(
        self,
        patched_server_module: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Should use DHARA_DEFAULT_URL when env var is not set."""
        import asyncio

        DHARA_DEFAULT_URL = patched_server_module["DHARA_DEFAULT_URL"]
        _register_component_to_dhara = patched_server_module["_register_component_to_dhara"]

        monkeypatch.delenv("DHARA_MCP_URL", raising=False)
        monkeypatch.setattr(asyncio, "create_task", AsyncMock())

        captured_urls: list[str] = []

        async def mock_register_once(dhara_url: str, key: str, mcp_url: str) -> bool:
            captured_urls.append(dhara_url)
            return True

        with patch(
            "akosha.mcp.server._register_to_dhara_once",
            new_callable=AsyncMock,
            side_effect=mock_register_once,
        ):
            import akosha.mcp.server as server_module

            server_module._heartbeat_task = None

            await _register_component_to_dhara("http://localhost:8682/mcp")

            assert captured_urls[0] == DHARA_DEFAULT_URL

    @pytest.mark.asyncio
    async def test_heartbeat_task_is_created(
        self,
        patched_server_module: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Should create a heartbeat task after successful registration."""
        import asyncio

        _register_component_to_dhara = patched_server_module["_register_component_to_dhara"]

        monkeypatch.setenv("DHARA_MCP_URL", "http://localhost:8683")
        monkeypatch.setattr(asyncio, "create_task", MagicMock())

        with patch(
            "akosha.mcp.server._register_to_dhara_once", new_callable=AsyncMock
        ) as mock_register:
            mock_register.return_value = True

            import akosha.mcp.server as server_module

            server_module._heartbeat_task = None

            await _register_component_to_dhara("http://localhost:8682/mcp")

            # asyncio.create_task was called once (heartbeat started)
            assert asyncio.create_task.called is True

    @pytest.mark.asyncio
    async def test_retries_until_success(
        self,
        patched_server_module: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Should retry until registration succeeds and then start heartbeat."""
        import asyncio

        _register_component_to_dhara = patched_server_module["_register_component_to_dhara"]

        monkeypatch.setenv("DHARA_MCP_URL", "http://localhost:8683")
        monkeypatch.setattr(asyncio, "create_task", MagicMock())

        attempt_count = 0

        async def mock_register_once(*args: object, **kwargs: object) -> bool:
            nonlocal attempt_count
            attempt_count += 1
            # Succeeds on 3rd attempt
            return attempt_count >= 3

        with patch(
            "akosha.mcp.server._register_to_dhara_once",
            new_callable=AsyncMock,
            side_effect=mock_register_once,
        ):
            import akosha.mcp.server as server_module

            server_module._heartbeat_task = None

            await _register_component_to_dhara("http://localhost:8682/mcp")

            # Should have tried 3 times before succeeding
            assert attempt_count == 3
            # Heartbeat task should have been started
            assert asyncio.create_task.called is True

    @pytest.mark.asyncio
    async def test_bounded_retry_when_dhara_unreachable(
        self,
        patched_server_module: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """B1 regression: bounded retry when Dhara is unreachable.

        The previous code used `itertools.count()` for the retry loop,
        making it infinite. Combined with an unreachable Dhara, this
        blocked the lifespan startup forever. The bug was hidden because
        the lifespan itself didn't fire (private-attribute poke no-op'd
        in FastMCP 3.x); the public-API lifespan fix activates the
        lifespan and exposes the bug. This test pins the bounded behavior
        so it can't regress.
        """
        import asyncio

        _register_component_to_dhara = patched_server_module["_register_component_to_dhara"]

        monkeypatch.setenv("DHARA_MCP_URL", "http://localhost:8683")
        monkeypatch.setattr(asyncio, "create_task", MagicMock())

        # Always fail — Dhara unreachable
        attempt_count = 0

        async def always_fail(*args: object, **kwargs: object) -> bool:
            nonlocal attempt_count
            attempt_count += 1
            return False

        # Mock asyncio.sleep to keep the test fast (<1s real time)
        # — the real sleep would total 1+2+4+8+16 = 31s of real waits.
        sleep_intervals: list[float] = []

        async def fake_sleep(t: float) -> None:
            sleep_intervals.append(t)

        with patch(
            "akosha.mcp.server._register_to_dhara_once",
            new_callable=AsyncMock,
            side_effect=always_fail,
        ), patch("akosha.mcp.server.asyncio.sleep", fake_sleep):
            import akosha.mcp.server as server_module

            server_module._heartbeat_task = None

            # The bounded retry must exit in well under 1 second of
            # real time (no real sleeps because we mocked sleep).
            await asyncio.wait_for(
                _register_component_to_dhara("http://localhost:8682/mcp"),
                timeout=1.0,
            )

        # The retry is bounded by MAX_STARTUP_ATTEMPTS = 5.
        # Verify exactly 5 attempts, not infinite.
        assert attempt_count == 5, (
            f"Expected exactly 5 retry attempts (MAX_STARTUP_ATTEMPTS), got {attempt_count}"
        )
        # Sleep intervals should be exponential: 1, 2, 4, 8, 16.
        assert sleep_intervals == [1, 2, 4, 8, 16], (
            f"Expected exponential backoff [1,2,4,8,16], got {sleep_intervals}"
        )
        # Even when bounded retries exhaust, the heartbeat task must
        # still be started (Phase 2 of the registration flow).
        assert asyncio.create_task.called is True
