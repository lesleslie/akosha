"""Embedding service for semantic search using ONNX models.

This module provides local embedding generation using sentence-transformers
with ONNX runtime for privacy-preserving semantic search.

 graceful Degradation:
    - If sentence-transformers unavailable: Returns mock embeddings
    - If model loading fails: Falls back to random embeddings
    - Always functional, never blocks operations
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import numpy as np
import numpy.typing as npt

logger = logging.getLogger(__name__)


class EmbeddingService:
    """Local embedding generation service.

    Uses all-MiniLM-L6-v2 model (384-dimensional embeddings) for semantic
    similarity search. Model runs locally via ONNX for privacy.

    Attributes:
        _model: sentence-transformers model (lazy loaded)
        _initialized: Whether model has been loaded
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        """Initialize embedding service.

        Args:
            model_name: HuggingFace model name (default: all-MiniLM-L6-v2)
        """
        self.model_name = model_name
        self._model: Any = None
        self._initialized = False
        self._available = False
        self._embedding_dim = 384  # all-MiniLM-L6-v2 dimension

        logger.info(f"Embedding service created with model: {model_name}")

    async def initialize(self) -> None:
        """Initialize embedding model (lazy loading).

        Attempts to load sentence-transformers model. If unavailable,
        marks service as unavailable and continues with fallback mode.
        """
        if self._initialized:
            return

        logger.info(f"Initializing embedding model: {self.model_name}")

        try:
            # Try importing sentence-transformers
            from sentence_transformers import SentenceTransformer

            logger.debug("sentence-transformers available, loading model...")

            # Load model in executor thread to avoid blocking
            loop = asyncio.get_event_loop()
            self._model = await loop.run_in_executor(
                None,
                self._load_model_sync,
                SentenceTransformer,
            )

            self._available = True
            self._initialized = True
            logger.info(
                f"✅ Embedding model loaded: {self.model_name} "
                f"(dim={self._embedding_dim})"
            )

        except ImportError as e:
            logger.warning(
                f"⚠️ sentence-transformers not available: {e}. "
                "Using fallback embeddings (mock)."
            )
            self._available = False
            self._initialized = True

        except Exception as e:
            logger.error(f"❌ Failed to load embedding model: {e}. Using fallback.")
            self._available = False
            self._initialized = True

    @staticmethod
    def _load_model_sync(model_class: type[Any]) -> Any:
        """Load model synchronously (runs in executor thread).

        Args:
            model_class: SentenceTransformer class

        Returns:
            Loaded model instance
        """
        return model_class("all-MiniLM-L6-v2")

    def is_available(self) -> bool:
        """Check if embedding service is available.

        Returns:
            True if real embeddings available, False if using fallback
        """
        return self._available

    async def generate_embedding(
        self,
        text: str,
    ) -> npt.NDArray[np.float32]:
        """Generate embedding for single text.

        Args:
            text: Input text to embed

        Returns:
            Embedding vector (384-dimensional float32 array)
        """
        if not self._initialized:
            await self.initialize()

        if self._available and self._model:
            # Real embedding generation
            loop = asyncio.get_event_loop()
            embedding = await loop.run_in_executor(
                None,
                self._model.encode,
                text,
            )
            return np.array(embedding, dtype=np.float32)
        else:
            # Fallback: Mock embedding based on text hash
            logger.debug("Using fallback embedding generation")
            return self._generate_fallback_embedding(text)

    async def generate_batch_embeddings(
        self,
        texts: list[str],
        batch_size: int = 32,
    ) -> list[npt.NDArray[np.float32]]:
        """Generate embeddings for multiple texts (batch processing).

        Args:
            texts: List of input texts
            batch_size: Batch size for processing

        Returns:
            List of embedding vectors
        """
        if not self._initialized:
            await self.initialize()

        if not texts:
            return []

        if self._available and self._model:
            # Batch embedding generation
            loop = asyncio.get_event_loop()
            embeddings = await loop.run_in_executor(
                None,
                self._model.encode,
                texts,
                {"batch_size": batch_size},
            )
            return [np.array(emb, dtype=np.float32) for emb in embeddings]
        else:
            # Fallback: Generate mock embeddings individually
            logger.debug(f"Using fallback embeddings for {len(texts)} texts")
            return [self._generate_fallback_embedding(text) for text in texts]

    def _generate_fallback_embedding(self, text: str) -> npt.NDArray[np.float32]:
        """Generate fallback embedding when model unavailable.

        Creates deterministic but non-semantic embedding based on text hash.
        This allows system to function without real embeddings for development.

        Args:
            text: Input text

        Returns:
            Mock embedding vector (384-dimensional)
        """
        # Generate deterministic random embedding based on text hash
        text_hash = hash(text)

        # Create local random state to avoid global seed issues
        local_random = random.Random(text_hash)

        # Generate embedding using local random state
        embedding_list = [local_random.gauss(0, 1) for _ in range(self._embedding_dim)]
        embedding = np.array(embedding_list, dtype=np.float32)

        # L2 normalize
        norm = np.linalg.norm(embedding)
        if norm > 0:
            embedding = (embedding / norm).astype(np.float32)

        return embedding

    async def compute_similarity(
        self,
        embedding1: npt.NDArray[np.float32],
        embedding2: npt.NDArray[np.float32],
    ) -> float:
        """Compute cosine similarity between two embeddings.

        Args:
            embedding1: First embedding vector
            embedding2: Second embedding vector

        Returns:
            Cosine similarity score (-1 to 1, typically 0 to 1 for normalized)
        """
        # Ensure numpy arrays
        emb1 = np.array(embedding1, dtype=np.float32)
        emb2 = np.array(embedding2, dtype=np.float32)

        # Cosine similarity: dot product of normalized vectors
        similarity = float(np.dot(emb1, emb2))

        return similarity

    async def rank_by_similarity(
        self,
        query_embedding: npt.NDArray[np.float32],
        candidate_embeddings: list[npt.NDArray[np.float32]],
        limit: int = 10,
    ) -> list[tuple[int, float]]:
        """Rank candidates by similarity to query.

        Args:
            query_embedding: Query embedding vector
            candidate_embeddings: List of candidate embeddings
            limit: Maximum results to return

        Returns:
            List of (index, similarity_score) tuples, sorted by similarity
        """
        if not candidate_embeddings:
            return []

        # Compute similarities
        similarities = []
        for idx, candidate_emb in enumerate(candidate_embeddings):
            similarity = await self.compute_similarity(query_embedding, candidate_emb)
            similarities.append((idx, similarity))

        # Sort by similarity (descending)
        similarities.sort(key=lambda x: x[1], reverse=True)

        return similarities[:limit]


# Singleton instance
_embedding_service: EmbeddingService | None = None


def get_embedding_service() -> EmbeddingService:
    """Get singleton embedding service instance.

    Returns:
        EmbeddingService instance
    """
    global _embedding_service

    if _embedding_service is None:
        _embedding_service = EmbeddingService()

    return _embedding_service
