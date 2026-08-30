"""Tests pinning the embedding-dim wiring between AkoshaApplication.start()
and the HotStore schema.

Plan: docs/plans/2026-08-29-embedding-dim-fix.md (Phase 3 + Phase 4).
Verifies that ``start()`` initialises the embedding service before
constructing the hot store so the schema dim matches the active
backend.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import akosha.main as main_module
from akosha.main import AkoshaApplication


def _make_mode() -> MagicMock:
    """Return a mock mode instance compatible with ``AkoshaApplication.__init__``."""
    mode = MagicMock()
    mode.mode_config.description = "lite mode"
    mode.mode_config.redis_enabled = False
    mode.mode_config.cold_storage_enabled = False
    mode.initialize_cache = AsyncMock(return_value=None)
    mode.initialize_cold_storage = AsyncMock(return_value=None)
    return mode


def _install_signal_spy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirror the spy used by ``tests/unit/test_hot_store_wiring.py``."""
    def fake_signal(sig: int, handler: object) -> None:  # noqa: ARG001
        pass

    monkeypatch.setattr(main_module.signal, "signal", fake_signal)


def _stub_embedding_service(*, dim: int, backend_name: str = "llama_cpp") -> MagicMock:
    """Build a stub EmbeddingService-like object for monkeypatching."""
    svc = MagicMock()
    svc.initialize = AsyncMock(return_value=None)
    svc.dimension = MagicMock(return_value=dim)
    svc.backend_name = MagicMock(return_value=backend_name)
    svc._backend_dim = dim
    return svc


class TestMainStartOrdersEmbeddingInitBeforeHotStore:
    """``start()`` must initialise the embedding service before building HotStore."""

    @pytest.mark.asyncio
    async def test_main_start_orders_embedding_init_before_hot_store(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``start()`` calls ``get_embedding_service().initialize()`` before
        ``create_hot_store(embedding_dim=...)`` and threads the resolved
        dim into the HotStore's ``_embedding_dim`` attribute."""
        emb_svc = _stub_embedding_service(dim=768, backend_name="llama_cpp")

        # Patch the module-level factory so start() picks up our stub.
        # ``main.py`` imports ``get_embedding_service`` from
        # ``akosha.processing.embeddings`` inside ``start()`` so the
        # factory lives in that module, not on main_module.
        monkeypatch.setattr(
            "akosha.processing.embeddings.get_embedding_service",
            lambda: emb_svc,
        )

        # Capture the embedding_dim that start() passes to create_hot_store.
        captured: dict[str, object] = {}

        class _FakeHotStore:
            """Stand-in for HotStore — captures the dim and asserts init order."""

            _embedding_dim: int = 0
            _initialized: bool = False

            def __init__(self, **kwargs: object) -> None:
                captured["create_kwargs"] = kwargs
                # Mirror HotStore.__init__ so the dim is recorded as a real
                # attribute — this is what the production code uses to
                # bake the schema DDL.
                dim = kwargs.get("embedding_dim")
                if isinstance(dim, int):
                    self._embedding_dim = dim

            async def initialize(self) -> None:
                # The embedding service must have been initialised first.
                emb_svc.initialize.assert_awaited()
                self._initialized = True

            async def close(self) -> None:
                self._initialized = False

        def fake_create_hot_store(**kwargs: object) -> _FakeHotStore:
            captured["dim_passed"] = kwargs.get("embedding_dim")
            return _FakeHotStore(**kwargs)

        monkeypatch.setattr(main_module, "create_hot_store", fake_create_hot_store)

        mode = _make_mode()
        monkeypatch.setattr("akosha.modes.get_mode", MagicMock(return_value=mode))

        _install_signal_spy(monkeypatch)

        app = AkoshaApplication()
        app._wire_eventbridge_publisher = MagicMock()  # type: ignore[method-assign]
        app.shutdown_event.wait = AsyncMock(return_value=None)

        await app.start()

        # The embedding service MUST have been initialised exactly once.
        emb_svc.initialize.assert_awaited_once()
        # The dim resolved from the (initialised) service must reach the store.
        assert captured.get("dim_passed") == 768
        assert app.hot_store is not None
        # HotStore's _embedding_dim is the authoritative record of what
        # schema dim the DuckDB CREATE TABLE used.
        assert app.hot_store._embedding_dim == 768

    @pytest.mark.asyncio
    async def test_main_start_no_service_falls_back_to_384(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No service → ``get_embedding_service()`` still initialises (best
        effort) and the resolved dim falls back to 384."""
        # Return a real-ish EmbeddingService stub with dim=None so the
        # resolver falls through to the default table (which has no
        # entry for ``uninitialized``) and finally the 384 fallback.
        emb_svc = SimpleNamespace(
            initialize=AsyncMock(return_value=None),
            dimension=lambda: None,
            backend_name=lambda: "uninitialized",
            _backend_dim=None,
        )
        monkeypatch.setattr(
            "akosha.processing.embeddings.get_embedding_service",
            lambda: emb_svc,
        )

        captured_dim: list[int | None] = []

        class _FakeHotStore:
            _embedding_dim: int = 0

            def __init__(self, **kwargs: object) -> None:
                captured_dim.append(kwargs.get("embedding_dim"))
                dim = kwargs.get("embedding_dim")
                if isinstance(dim, int):
                    self._embedding_dim = dim

            async def initialize(self) -> None:
                pass

            async def close(self) -> None:
                pass

        def fake_create_hot_store(**kwargs: object) -> _FakeHotStore:
            return _FakeHotStore(**kwargs)

        monkeypatch.setattr(
            main_module,
            "create_hot_store",
            fake_create_hot_store,
        )

        mode = _make_mode()
        monkeypatch.setattr("akosha.modes.get_mode", MagicMock(return_value=mode))
        _install_signal_spy(monkeypatch)

        app = AkoshaApplication()
        app._wire_eventbridge_publisher = MagicMock()  # type: ignore[method-assign]
        app.shutdown_event.wait = AsyncMock(return_value=None)

        await app.start()

        # The resolver falls back to 384 when no service is initialised.
        assert captured_dim == [384]
        assert app.hot_store is not None
        assert app.hot_store._embedding_dim == 384