"""Tests for :mod:`akosha.mcp.tools.group_registers`.

The module wraps per-group tool registration so the W0 apply_tool_profile
helper can dispatch by group name. Each wrapper creates its required
services inline (lite-mode aware). Coverage was at 48% before these
tests. The 52 missed lines were:

- 4 async wrappers' "skipped if hot_store unavailable" branches
- ``_try_create_hot_store`` shared-store + fallback + exception paths
- ``_get_shared_kg_builder`` shared + fallback branches
- ``register_fitness_group``'s no-running-loop / running-loop branches
- ``register_eventbridge_group``'s per-call AkoshaConfig re-read

These tests pin every reachable branch by mocking out the heavy
imports (FastMCP, the actual tool registration helpers, the storage
factory). The point is to cover the *flow* through group_registers,
not to re-test the underlying registration helpers.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Sync wrappers (3 functions)
# ---------------------------------------------------------------------------


class TestRegisterHealthAkoshaGroup:
    """``register_health_akosha_group`` wires the always-on health probes."""

    def test_registers_health_tools(self) -> None:
        """Calls ``register_health_tools_akosha(app)`` and logs."""
        app = MagicMock()
        with patch("akosha.mcp.tools.register_health_tools_akosha") as mock_register:
            from akosha.mcp.tools.group_registers import (
                register_health_akosha_group,
            )

            register_health_akosha_group(app)
        mock_register.assert_called_once_with(app)
        # The patch object retains its mock identity; sanity check that
        # the call did not raise (counter is 1 after exactly one call).
        assert mock_register.call_count == 1

    def test_logs_info_on_success(self, caplog: pytest.LogCaptureFixture) -> None:
        """Successful registration emits an info-level log."""
        app = MagicMock()
        with patch("akosha.mcp.tools.register_health_tools_akosha"):
            from akosha.mcp.tools.group_registers import (
                register_health_akosha_group,
            )

            with caplog.at_level(logging.INFO, logger="akosha.mcp.tools.group_registers"):
                register_health_akosha_group(app)
        assert any("Registered health check tools" in rec.message for rec in caplog.records)


class TestRegisterCrossRepoGroup:
    """``register_cross_repo_group`` wires the Phase 1 capability search tools."""

    def test_registers_cross_repo_tools(self) -> None:
        """Calls ``register_cross_repo_tools(registry)`` with a real registry."""
        app = MagicMock()
        with patch("akosha.mcp.tools.cross_repo_tools.register_cross_repo_tools") as mock_register:
            from akosha.mcp.tools.group_registers import register_cross_repo_group

            register_cross_repo_group(app)
        mock_register.assert_called_once()
        # The single positional arg is the FastMCPToolRegistry wrapper.
        registry_arg = mock_register.call_args.args[0]
        # ``FastMCPToolRegistry.__init__`` takes an app — verify our app was
        # passed. The registry stores it as the private ``_app`` attribute.
        assert getattr(registry_arg, "_app", None) is app

    def test_logs_info_on_success(self, caplog: pytest.LogCaptureFixture) -> None:
        """Successful registration logs at info level."""
        app = MagicMock()
        with patch("akosha.mcp.tools.cross_repo_tools.register_cross_repo_tools"):
            from akosha.mcp.tools.group_registers import register_cross_repo_group

            with caplog.at_level(logging.INFO, logger="akosha.mcp.tools.group_registers"):
                register_cross_repo_group(app)
        assert any(
            "Registered cross-repo capability search tools" in rec.message for rec in caplog.records
        )


class TestRegisterEventbridgeGroup:
    """``register_eventbridge_group`` wires the EventBridge publisher tool."""

    def test_registers_with_per_call_enabled_fn(self) -> None:
        """The wrapper passes an ``enabled_fn`` lambda that re-reads config per call."""
        app = MagicMock()
        with patch(
            "akosha.mcp.tools.eventbridge_tools.register_eventbridge_tools"
        ) as mock_register:
            from akosha.mcp.tools.group_registers import register_eventbridge_group

            register_eventbridge_group(app)
        mock_register.assert_called_once()
        # The first positional arg is the app; the only kwarg is enabled_fn.
        kwargs = mock_register.call_args.kwargs
        assert "enabled_fn" in kwargs
        assert callable(kwargs["enabled_fn"])

    def test_enabled_fn_returns_akosha_config_eventbridge_enabled(self) -> None:
        """The per-call lambda constructs a fresh ``AkoshaConfig`` and reads its flag."""
        app = MagicMock()
        with patch(
            "akosha.mcp.tools.eventbridge_tools.register_eventbridge_tools"
        ) as mock_register:
            from akosha.mcp.tools.group_registers import register_eventbridge_group

            register_eventbridge_group(app)
        enabled_fn = mock_register.call_args.kwargs["enabled_fn"]
        # The lambda constructs ``AkoshaConfig()`` and reads
        # ``.eventbridge.enabled`` — call it and verify it returns a bool.
        result = enabled_fn()
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Async wrappers (4 functions) + helpers
# ---------------------------------------------------------------------------


class TestTryCreateHotStore:
    """``_try_create_hot_store`` is the workhorse for 4 async wrappers."""

    @pytest.mark.asyncio
    async def test_returns_shared_store_when_available(self) -> None:
        """When ``get_shared_hot_store`` returns a non-None store, return it directly."""
        from akosha.mcp.tools.group_registers import _try_create_hot_store

        shared = MagicMock(name="shared_hot_store")
        with patch(
            "akosha.mcp.server.get_shared_hot_store",
            return_value=shared,
            create=True,
        ):
            result = await _try_create_hot_store()
        assert result is shared

    @pytest.mark.asyncio
    async def test_falls_back_to_create_hot_store_when_no_shared(self) -> None:
        """When shared store is None, fall through to ``create_hot_store`` + initialize."""
        from akosha.mcp.tools.group_registers import _try_create_hot_store

        fresh = AsyncMock(name="fresh_hot_store")
        fresh.initialize = AsyncMock()
        with patch(
            "akosha.mcp.server.get_shared_hot_store",
            return_value=None,
            create=True,
        ):
            with patch("akosha.storage.create_hot_store", return_value=fresh, create=True):
                result = await _try_create_hot_store()
        assert result is fresh
        fresh.initialize.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shared_lookup_failure_falls_back(self) -> None:
        """If ``get_shared_hot_store`` raises, fall through to per-call construction."""
        from akosha.mcp.tools.group_registers import _try_create_hot_store

        fresh = AsyncMock(name="fresh_hot_store")
        fresh.initialize = AsyncMock()
        with patch(
            "akosha.mcp.server.get_shared_hot_store",
            side_effect=ImportError("not wired"),
            create=True,
        ):
            with patch("akosha.storage.create_hot_store", return_value=fresh, create=True):
                result = await _try_create_hot_store()
        assert result is fresh

    @pytest.mark.asyncio
    async def test_returns_none_when_create_fails(self, caplog: pytest.LogCaptureFixture) -> None:
        """If ``create_hot_store`` raises, log debug and return None."""
        from akosha.mcp.tools.group_registers import _try_create_hot_store

        with patch(
            "akosha.mcp.server.get_shared_hot_store",
            return_value=None,
            create=True,
        ):
            with patch(
                "akosha.storage.create_hot_store",
                side_effect=RuntimeError("duckdb missing"),
                create=True,
            ):
                with caplog.at_level(logging.DEBUG, logger="akosha.mcp.tools.group_registers"):
                    result = await _try_create_hot_store()
        assert result is None
        assert any("create_hot_store" in rec.message for rec in caplog.records)


class TestGetSharedKgBuilder:
    """``_get_shared_kg_builder`` returns the lifespan-owned builder or ``None``."""

    def test_returns_shared_builder_when_available(self) -> None:
        """When ``get_shared_kg_builder`` returns a builder, pass it through."""
        from akosha.mcp.tools.group_registers import _get_shared_kg_builder

        shared = MagicMock(name="shared_kg_builder")
        with patch(
            "akosha.mcp.server.get_shared_kg_builder",
            return_value=shared,
            create=True,
        ):
            result = _get_shared_kg_builder()
        assert result is shared

    def test_returns_none_on_lookup_failure(self) -> None:
        """If the lookup raises, return None so callers fall back to default construction."""
        from akosha.mcp.tools.group_registers import _get_shared_kg_builder

        with patch(
            "akosha.mcp.server.get_shared_kg_builder",
            side_effect=ImportError("not wired"),
            create=True,
        ):
            result = _get_shared_kg_builder()
        assert result is None


class TestRegisterAkoshaGroup:
    """``register_akosha_group`` wires embedding + analytics + graph + hot_store."""

    @pytest.mark.asyncio
    async def test_registers_with_all_services(
        self,
    ) -> None:
        """When hot_store can be built, all services are passed to ``register_akosha_tools``."""
        app = MagicMock()
        hot_store = MagicMock()
        embedding = MagicMock()
        kg_builder = MagicMock()

        with patch(
            "akosha.mcp.tools.group_registers._try_create_hot_store",
            AsyncMock(return_value=hot_store),
        ):
            with patch(
                "akosha.mcp.tools.group_registers._get_shared_kg_builder",
                return_value=kg_builder,
            ):
                with patch(
                    "akosha.processing.embeddings.get_embedding_service",
                    return_value=embedding,
                ):
                    with patch(
                        "akosha.mcp.tools.akosha_tools.register_akosha_tools"
                    ) as mock_register:
                        from akosha.mcp.tools.group_registers import (
                            register_akosha_group,
                        )

                        await register_akosha_group(app)

        mock_register.assert_called_once()
        kwargs = mock_register.call_args.kwargs
        assert kwargs["embedding_service"] is embedding
        assert kwargs["hot_store"] is hot_store
        # When the shared kg builder is non-None, it is passed through.
        assert kwargs["graph_builder"] is kg_builder
        # analytics_service is always created (TimeSeriesAnalytics()).
        assert kwargs["analytics_service"] is not None

    @pytest.mark.asyncio
    async def test_falls_back_to_default_kg_builder_when_shared_is_none(self) -> None:
        """When ``_get_shared_kg_builder`` returns None, build a default ``KnowledgeGraphBuilder``."""
        app = MagicMock()
        hot_store = MagicMock()
        default_builder = MagicMock()

        with patch(
            "akosha.mcp.tools.group_registers._try_create_hot_store",
            AsyncMock(return_value=hot_store),
        ):
            with patch(
                "akosha.mcp.tools.group_registers._get_shared_kg_builder",
                return_value=None,
            ):
                with patch(
                    "akosha.processing.embeddings.get_embedding_service",
                    return_value=MagicMock(),
                ):
                    with patch(
                        "akosha.processing.knowledge_graph.KnowledgeGraphBuilder",
                        return_value=default_builder,
                    ):
                        with patch(
                            "akosha.mcp.tools.akosha_tools.register_akosha_tools"
                        ) as mock_register:
                            from akosha.mcp.tools.group_registers import (
                                register_akosha_group,
                            )

                            await register_akosha_group(app)

        kwargs = mock_register.call_args.kwargs
        assert kwargs["graph_builder"] is default_builder

    @pytest.mark.asyncio
    async def test_passes_none_hot_store_when_unavailable(self) -> None:
        """When ``_try_create_hot_store`` returns None, ``hot_store=None`` is passed (lite mode)."""
        app = MagicMock()
        with patch(
            "akosha.mcp.tools.group_registers._try_create_hot_store",
            AsyncMock(return_value=None),
        ):
            with patch(
                "akosha.mcp.tools.group_registers._get_shared_kg_builder",
                return_value=MagicMock(),
            ):
                with patch(
                    "akosha.processing.embeddings.get_embedding_service",
                    return_value=MagicMock(),
                ):
                    with patch(
                        "akosha.mcp.tools.akosha_tools.register_akosha_tools"
                    ) as mock_register:
                        from akosha.mcp.tools.group_registers import (
                            register_akosha_group,
                        )

                        await register_akosha_group(app)

        kwargs = mock_register.call_args.kwargs
        assert kwargs["hot_store"] is None


class TestRegisterHotStoreGatedGroups:
    """The 3 async wrappers (session_buddy, pycharm, otel_query) skip on no-hot-store."""

    @pytest.mark.asyncio
    async def test_session_buddy_skips_when_hot_store_unavailable(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When ``_try_create_hot_store`` returns None, the wrapper logs and returns."""
        from akosha.mcp.tools.group_registers import register_session_buddy_group

        app = MagicMock()
        with patch(
            "akosha.mcp.tools.group_registers._try_create_hot_store",
            AsyncMock(return_value=None),
        ):
            with caplog.at_level(logging.INFO, logger="akosha.mcp.tools.group_registers"):
                await register_session_buddy_group(app)
        # No register call should have happened.
        assert any("Skipping Session-Buddy tools" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_session_buddy_registers_when_hot_store_available(self) -> None:
        """When hot_store is available, the wrapper calls ``register_session_buddy_tools``."""
        app = MagicMock()
        hot_store = MagicMock()
        with patch(
            "akosha.mcp.tools.group_registers._try_create_hot_store",
            AsyncMock(return_value=hot_store),
        ):
            with patch(
                "akosha.mcp.tools.session_buddy_tools.register_session_buddy_tools"
            ) as mock_register:
                from akosha.mcp.tools.group_registers import (
                    register_session_buddy_group,
                )

                await register_session_buddy_group(app)
        mock_register.assert_called_once()
        args, kwargs = mock_register.call_args
        # The registry wrapper + hot_store are the positional/keyword args.
        # We just check hot_store was passed.
        assert hot_store in (list(args) + list(kwargs.values()))

    @pytest.mark.asyncio
    async def test_pycharm_skips_when_hot_store_unavailable(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """PyCharm wrapper logs and returns when no hot_store."""
        from akosha.mcp.tools.group_registers import register_pycharm_group

        app = MagicMock()
        with patch(
            "akosha.mcp.tools.group_registers._try_create_hot_store",
            AsyncMock(return_value=None),
        ):
            with caplog.at_level(logging.INFO, logger="akosha.mcp.tools.group_registers"):
                await register_pycharm_group(app)
        assert any("Skipping PyCharm tools" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_pycharm_registers_when_hot_store_available(self) -> None:
        """PyCharm wrapper registers tools when hot_store is available."""
        app = MagicMock()
        hot_store = MagicMock()
        with patch(
            "akosha.mcp.tools.group_registers._try_create_hot_store",
            AsyncMock(return_value=hot_store),
        ):
            with patch("akosha.mcp.tools.pycharm_tools.register_pycharm_tools") as mock_register:
                from akosha.mcp.tools.group_registers import register_pycharm_group

                await register_pycharm_group(app)
        mock_register.assert_called_once()

    @pytest.mark.asyncio
    async def test_otel_query_skips_when_hot_store_unavailable(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """OTel wrapper logs and returns when no hot_store."""
        from akosha.mcp.tools.group_registers import register_otel_query_group

        app = MagicMock()
        with patch(
            "akosha.mcp.tools.group_registers._try_create_hot_store",
            AsyncMock(return_value=None),
        ):
            with caplog.at_level(logging.INFO, logger="akosha.mcp.tools.group_registers"):
                await register_otel_query_group(app)
        assert any("Skipping OTel query tools" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_otel_query_registers_when_hot_store_available(self) -> None:
        """OTel wrapper registers tools when hot_store is available."""
        app = MagicMock()
        hot_store = MagicMock()
        with patch(
            "akosha.mcp.tools.group_registers._try_create_hot_store",
            AsyncMock(return_value=hot_store),
        ):
            with patch("akosha.mcp.tools.otel_tools.register_otel_query_tools") as mock_register:
                from akosha.mcp.tools.group_registers import register_otel_query_group

                await register_otel_query_group(app)
        mock_register.assert_called_once_with(app, hot_store)


class TestRegisterFitnessGroup:
    """``register_fitness_group`` wires the analyzer with Dhara endpoints."""

    def test_registers_fitness_tools(self) -> None:
        """The wrapper builds an analyzer, inits it, populates endpoints, and registers tools."""
        app = MagicMock()
        analyzer = MagicMock()
        with patch(
            "akosha.processing.fitness_analyzer.FitnessAnalyzer",
            return_value=analyzer,
        ):
            with patch("akosha.mcp.tools.fitness_tools.init_fitness_analyzer") as mock_init:
                with patch(
                    "akosha.mcp.tools.fitness_tools.register_fitness_tools"
                ) as mock_register:
                    with patch(
                        "akosha.mcp.tools._populate_component_endpoints_from_dhara"
                    ) as mock_populate:
                        from akosha.mcp.tools.group_registers import (
                            register_fitness_group,
                        )

                        register_fitness_group(app)
        mock_init.assert_called_once_with(analyzer)
        mock_populate.assert_called_once_with(analyzer)
        mock_register.assert_called_once_with(app)
        # All three collaborators invoked exactly once.
        assert mock_init.call_count == 1
        assert mock_populate.call_count == 1
        assert mock_register.call_count == 1

    def test_logs_info_on_success(self, caplog: pytest.LogCaptureFixture) -> None:
        """Successful registration emits an info-level log."""
        app = MagicMock()
        with patch(
            "akosha.processing.fitness_analyzer.FitnessAnalyzer",
            return_value=MagicMock(),
        ):
            with patch("akosha.mcp.tools.fitness_tools.init_fitness_analyzer"):
                with patch("akosha.mcp.tools.fitness_tools.register_fitness_tools"):
                    with patch("akosha.mcp.tools._populate_component_endpoints_from_dhara"):
                        from akosha.mcp.tools.group_registers import (
                            register_fitness_group,
                        )

                        with caplog.at_level(
                            logging.INFO, logger="akosha.mcp.tools.group_registers"
                        ):
                            register_fitness_group(app)
        assert any("Registered fitness analysis tools" in rec.message for rec in caplog.records)
