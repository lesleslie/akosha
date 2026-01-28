"""Tests for embedding service."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import numpy.typing as npt
import pytest

from akasha.processing.embeddings import EmbeddingService, get_embedding_service


class TestEmbeddingService:
    """Test suite for EmbeddingService."""

    @pytest.fixture
    def service(self) -> EmbeddingService:
        """Create fresh embedding service for each test."""
        return EmbeddingService()

    @pytest.mark.asyncio
    async def test_initialization(self, service: EmbeddingService) -> None:
        """Test service initialization."""
        assert not service._initialized
        assert not service.is_available()

        await service.initialize()

        assert service._initialized
        # Service may or may not be available depending on dependencies

    @pytest.mark.asyncio
    async def test_singleton(self) -> None:
        """Test singleton pattern."""
        svc1 = get_embedding_service()
        svc2 = get_embedding_service()

        assert svc1 is svc2

    @pytest.mark.asyncio
    async def test_generate_embedding_fallback(self, service: EmbeddingService) -> None:
        """Test embedding generation with fallback mode."""
        # Force fallback mode
        service._available = False
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
        """Test that fallback embeddings are deterministic."""
        service._available = False
        service._initialized = True

        text = "deterministic test"
        emb1 = await service.generate_embedding(text)
        emb2 = await service.generate_embedding(text)

        # Same text should produce same embedding
        np.testing.assert_array_almost_equal(emb1, emb2)

    @pytest.mark.asyncio
    async def test_fallback_different_texts(self, service: EmbeddingService) -> None:
        """Test that different texts produce different fallback embeddings."""
        service._available = False
        service._initialized = True

        emb1 = await service.generate_embedding("text one")
        emb2 = await service.generate_embedding("text two")

        # Different texts should produce different embeddings
        assert not np.allclose(emb1, emb2)

    @pytest.mark.asyncio
    async def test_batch_embeddings_fallback(self, service: EmbeddingService) -> None:
        """Test batch embedding generation with fallback."""
        service._available = False
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
        service._available = False
        service._initialized = True

        embeddings = await service.generate_batch_embeddings([])

        assert embeddings == []

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

    @pytest.mark.skip(reason="sentence_transformers not installed - graceful degradation tested instead")
    @pytest.mark.asyncio
    async def test_mock_model_loading(self) -> None:
        """Test model loading with mock."""
        service = EmbeddingService()

        # Mock the import and model loading
        with patch("sentence_transformers.SentenceTransformer") as mock_st_class:
            # Mock model instance
            mock_model = MagicMock()
            mock_model.encode.return_value = np.random.randn(384).astype(np.float32)

            # Mock the SentenceTransformer class to return our mock model
            mock_st_class.return_value = mock_model

            await service.initialize()

            assert service._initialized
            assert service.is_available()

    @pytest.mark.skip(reason="sentence_transformers not installed - graceful degradation tested instead")
    @pytest.mark.asyncio
    async def test_real_embedding_generation_mock(self) -> None:
        """Test real embedding generation with mocked model."""
        service = EmbeddingService()

        # Mock the model loading and encoding
        with patch("sentence_transformers.SentenceTransformer") as mock_st_class:
            mock_model = MagicMock()
            mock_model.encode.return_value = np.random.randn(384).astype(np.float32)
            mock_st_class.return_value = mock_model

            await service.initialize()

            # Generate embedding
            text = "test conversation"
            embedding = await service.generate_embedding(text)

            assert isinstance(embedding, np.ndarray)
            assert embedding.shape == (384,)

            # Verify model.encode was called
            mock_model.encode.assert_called_once_with(text)

    @pytest.mark.skip(reason="sentence_transformers not installed - graceful degradation tested instead")
    @pytest.mark.asyncio
    async def test_batch_real_embeddings_mock(self) -> None:
        """Test batch embedding generation with mocked model."""
        service = EmbeddingService()

        with patch("sentence_transformers.SentenceTransformer") as mock_st_class:
            mock_model = MagicMock()
            mock_model.encode.return_value = np.random.randn(3, 384).astype(np.float32)
            mock_st_class.return_value = mock_model

            await service.initialize()

            texts = ["text1", "text2", "text3"]
            embeddings = await service.generate_batch_embeddings(texts)

            assert len(embeddings) == 3

            # Verify model.encode was called with batch
            mock_model.encode.assert_called_once()

    @pytest.mark.skip(reason="sentence_transformers not installed - graceful degradation tested instead")
    @pytest.mark.asyncio
    async def test_initialization_only_once(self, service: EmbeddingService) -> None:
        """Test that initialization only runs once."""
        service._available = False
        service._initialized = True

        # First call should not re-initialize
        with patch("sentence_transformers.SentenceTransformer") as mock_st_class:
            await service.initialize()

            # Should not have attempted to load since already initialized
            mock_st_class.assert_not_called()
