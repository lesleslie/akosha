"""Tests for akosha/processing/embedding_dim.py — the embedding-dim resolver.

Plan: docs/plans/2026-08-29-embedding-dim-fix.md (Phase 1). Pins the
single source of truth for "what dim does this backend produce" so the
HotStore schema and the embedding service agree at startup.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from akosha.processing.embedding_dim import (
    BACKEND_DEFAULT_DIMS,
    DEFAULT_DIMENSION,
    resolve_embedding_dim,
)


class TestResolveEmbeddingDimNoService:
    """No service passed → use the hard-coded 384 fallback."""

    def test_no_service_returns_default(self) -> None:
        """``resolve_embedding_dim()`` with no service falls back to 384."""
        assert resolve_embedding_dim() == DEFAULT_DIMENSION
        assert DEFAULT_DIMENSION == 384

    def test_no_service_explicit_none(self) -> None:
        """``resolve_embedding_dim(None)`` also falls back to 384."""
        assert resolve_embedding_dim(None) == 384


class TestResolveEmbeddingDimInitialized:
    """Service with a populated ``dimension()`` returns its reported value."""

    def test_initialized_backend_with_dim_768(self) -> None:
        """An initialized service with dim 768 wins over the default table."""
        service = MagicMock()
        service.dimension = MagicMock(return_value=768)
        service.backend_name = MagicMock(return_value="llama_cpp")
        # Underlying attribute also populated (post-initialize state).
        service._backend_dim = 768

        assert resolve_embedding_dim(service) == 768

    def test_initialized_backend_with_dim_1024_minimax(self) -> None:
        """MiniMax defaults to 1024 — verifier for the table lookup fallback."""
        service = MagicMock()
        service.dimension = MagicMock(return_value=1024)
        service.backend_name = MagicMock(return_value="minimax")
        service._backend_dim = 1024

        assert resolve_embedding_dim(service) == 1024


class TestResolveEmbeddingDimUninitialized:
    """Service exists but ``dimension()`` returns ``None`` → fall back to the defaults table."""

    def test_uninitialized_service_with_backend_name_ollama(self) -> None:
        """``_backend_dim=None`` and ``backend_name()='ollama'`` returns 768."""
        service = SimpleNamespace(
            _backend_dim=None,
            dimension=lambda: None,
            backend_name=lambda: "ollama",
        )

        assert resolve_embedding_dim(service) == 768
        assert BACKEND_DEFAULT_DIMS["ollama"] == 768

    def test_uninitialized_service_with_unknown_backend_falls_back(self) -> None:
        """An unknown backend name still falls back to 384 (no table entry)."""
        service = SimpleNamespace(
            _backend_dim=None,
            dimension=lambda: None,
            backend_name=lambda: "mystery-backend",
        )

        assert resolve_embedding_dim(service) == DEFAULT_DIMENSION

    def test_uninitialized_mock_backend(self) -> None:
        """The mock backend's default dim is 384 (matches existing test fixtures)."""
        service = SimpleNamespace(
            _backend_dim=None,
            dimension=lambda: None,
            backend_name=lambda: "mock",
        )

        assert resolve_embedding_dim(service) == 384