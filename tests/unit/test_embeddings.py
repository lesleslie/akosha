"""Tests for embedding service."""

from __future__ import annotations

import numpy as np
import pytest

import akosha.processing.embeddings as embeddings_module
from akosha.processing.embeddings import EmbeddingService, get_embedding_service


class TestEmbeddingService:
    """Test suite for EmbeddingService (mock-only)."""

    @pytest.fixture
    def service(self) -> EmbeddingService:
        """Create fresh embedding service for each test."""
        return EmbeddingService()

    @pytest.mark.asyncio
    async def test_initialization(self, service: EmbeddingService) -> None:
        """Test service initialization.

        After the oneiric delegation migration, ``is_available()`` is
        finally honest: it returns True when a real backend (Ollama,
        llama.cpp, MiniMax, model2vec) probed successfully. The old
        "always False because mock-only" assertion is gone.
        """
        assert not service._initialized
        # Before initialize(), the service has no opinion on availability.
        assert service.backend_name() == "uninitialized"

        await service.initialize()

        assert service._initialized
        # ``is_available()`` now reflects whether a real backend was
        # reached; the old test's "always False" assertion is removed.
        assert service.backend_name() in {
            "llama_cpp",
            "ollama",
            "minimax",
            "model2vec",
            "mock",
        }

    @pytest.mark.asyncio
    async def test_singleton(self) -> None:
        """Test singleton pattern."""
        svc1 = get_embedding_service()
        svc2 = get_embedding_service()

        assert svc1 is svc2

    @pytest.mark.asyncio
    async def test_generate_embedding_returns_mock_vector(
        self, service: EmbeddingService
    ) -> None:
        """Test mock embedding generation."""
        service._initialized = True

        text = "test conversation about Python development"
        embedding = await service.generate_embedding(text)

        assert isinstance(embedding, np.ndarray)
        assert embedding.shape == (384,)
        assert embedding.dtype == np.float32

        # Check normalized (L2 norm should be ~1.0)
        norm = np.linalg.norm(embedding)
        assert abs(norm - 1.0) < 0.01

    @pytest.mark.asyncio
    async def test_fallback_deterministic(self, service: EmbeddingService) -> None:
        """Test that mock embeddings are deterministic."""
        service._initialized = True

        text = "deterministic test"
        emb1 = await service.generate_embedding(text)
        emb2 = await service.generate_embedding(text)

        # Same text should produce same embedding
        np.testing.assert_array_almost_equal(emb1, emb2)

    @pytest.mark.asyncio
    async def test_fallback_different_texts(self, service: EmbeddingService) -> None:
        """Test that different texts produce different mock embeddings."""
        service._initialized = True

        emb1 = await service.generate_embedding("text one")
        emb2 = await service.generate_embedding("text two")

        # Different texts should produce different embeddings
        assert not np.allclose(emb1, emb2)

    @pytest.mark.asyncio
    async def test_batch_embeddings(self, service: EmbeddingService) -> None:
        """Test batch embedding generation."""
        service._initialized = True

        texts = [
            "first conversation",
            "second conversation",
            "third conversation",
        ]

        embeddings = await service.generate_batch_embeddings(texts)

        assert len(embeddings) == 3
        for emb in embeddings:
            assert isinstance(emb, np.ndarray)
            assert emb.shape == (384,)
            assert emb.dtype == np.float32

    @pytest.mark.asyncio
    async def test_batch_embeddings_empty(self, service: EmbeddingService) -> None:
        """Test batch embedding with empty list."""
        service._initialized = True

        embeddings = await service.generate_batch_embeddings([])

        assert embeddings == []

    @pytest.mark.asyncio
    async def test_initialize_returns_early_when_already_initialized(self) -> None:
        """Repeated initialize calls should be cheap no-ops."""
        service = EmbeddingService()
        await service.initialize()
        first_backend = service.backend_name()
        first_dim = service.dimension()

        await service.initialize()

        # Backend + dimension must be stable across repeated init.
        assert service.backend_name() == first_backend
        assert service.dimension() == first_dim

    @pytest.mark.asyncio
    async def test_generate_methods_auto_initialize_when_needed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The public methods should lazily initialize an unprepared service.

        After the migration to oneiric, the lazy-init path is exercised
        by ``generate_embedding`` itself — if the chain hasn't been
        awaited, the first encode triggers ``initialize``.
        """
        service = EmbeddingService()
        assert not service._initialized

        # Patch encode to return a known vector (skip the chain).
        async def fake_encode(text: str) -> np.ndarray:
            return np.ones(384, dtype=np.float32)

        monkeypatch.setattr(service, "encode", fake_encode)

        embedding = await service.generate_embedding("lazy-init")

        # Auto-initialization should have happened on first encode.
        assert service._initialized
        assert embedding.shape == (384,)

    @pytest.mark.asyncio
    async def test_fallback_embedding_zero_norm_stays_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The mock fallback's zero-vector path stays zero (no NaN/Inf)."""
        service = EmbeddingService()
        service._initialized = True

        # The new fallback uses ``np.random.default_rng`` instead of
        # ``random.Random.gauss``. Patch the seed-derived RNG to emit
        # all zeros so the test exercises the L2-normalize "skip if
        # norm == 0" branch.
        from oneiric.adapters.observability import embeddings as _oneiric_emb

        monkeypatch.setattr(
            _oneiric_emb.np.random,
            "default_rng",
            lambda _seed: type("R", (), {"standard_normal": lambda self, n: np.zeros(n)})(),
        )

        embedding = service._generate_fallback_embedding("zero-vector")

        assert isinstance(embedding, np.ndarray)
        assert embedding.shape == (384,)
        assert np.count_nonzero(embedding) == 0
        assert np.all(np.isfinite(embedding))

    @pytest.mark.asyncio
    async def test_compute_similarity(self, service: EmbeddingService) -> None:
        """Test similarity computation."""
        emb1 = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        emb2 = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        emb3 = np.array([0.0, 1.0, 0.0], dtype=np.float32)

        # Identical vectors
        sim_12 = await service.compute_similarity(emb1, emb2)
        assert abs(sim_12 - 1.0) < 0.01

        # Orthogonal vectors
        sim_13 = await service.compute_similarity(emb1, emb3)
        assert abs(sim_13 - 0.0) < 0.01

    @pytest.mark.asyncio
    async def test_rank_by_similarity(self, service: EmbeddingService) -> None:
        """Test ranking candidates by similarity."""
        query = np.array([1.0, 0.0], dtype=np.float32)

        candidates = [
            np.array([0.9, 0.1], dtype=np.float32),  # Most similar
            np.array([0.1, 0.9], dtype=np.float32),  # Least similar
            np.array([0.7, 0.3], dtype=np.float32),  # Medium similarity
        ]

        results = await service.rank_by_similarity(query, candidates, limit=2)

        assert len(results) == 2
        # Most similar should be first
        assert results[0][0] == 0
        assert results[0][1] > results[1][1]

    @pytest.mark.asyncio
    async def test_rank_by_similarity_empty(self, service: EmbeddingService) -> None:
        """Test ranking with empty candidates."""
        query = np.array([1.0, 0.0], dtype=np.float32)

        results = await service.rank_by_similarity(query, [])

        assert results == []
