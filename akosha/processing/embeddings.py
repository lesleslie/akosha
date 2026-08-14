"""Embedding service for semantic search.

Provides embedding generation for Akosha's semantic search and similarity
pipelines. As of 2026-08 the runtime path is **deterministic mock
embeddings** generated from ``numpy.random`` — there is no native ONNX, no
sentence-transformers, and no fastembed import in this process.

Why mock?

- The previous ONNX / sentence-transformers runtime was removed when the
  ``embeddings`` PEP 735 dependency group was emptied (see ``pyproject.toml``
  lines 173-176). Embedding generation is now delegated to MCP-side
  providers (Ollama, OpenAI) running in the configured Bodai ecosystem.
- ``akosha/processing/embeddings.py`` exposes a stable interface
  (``EmbeddingService``, ``get_embedding_service``,
  ``MockEmbeddingService``) so callers and tests do not need to change.

Graceful degradation:

    - If a real embedding backend is unavailable: returns deterministic
      mock vectors seeded from the input text hash, so semantic-search
      scores are stable across runs for the same corpus.
    - ``is_available()`` returns False to signal to callers that they
      are operating against synthetic vectors — operations continue but
      relevance ranking is not meaningful.
    - Always functional; never blocks operations.
"""

from __future__ import annotations

import logging
import random

import numpy as np
import numpy.typing as npt

from akosha.observability import record_counter, record_histogram, traced

logger = logging.getLogger(__name__)


class EmbeddingService:
    """Deterministic mock embedding service.

    Returns 384-dimensional float32 vectors derived from the input text
    hash. There is no real ONNX / sentence-transformers / fastembed runtime
    in this process — see the module docstring for why this is mock-only.

    ``is_available()`` always returns False; callers that need real
    embeddings should route through the configured MCP-side provider
    (Ollama, OpenAI).

    Attributes:
        model_name: Retained for API compatibility; informational only.
        _initialized: Whether the singleton has been lazily prepared.
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        """Initialize embedding service.

        Args:
            model_name: Retained for API compatibility; the runtime is
                mock-only regardless of value.
        """
        self.model_name = model_name
        self._initialized = False
        self._embedding_dim = 384  # all-MiniLM-L6-v2 dimension

        logger.info(
            f"Embedding service created (mock-only, model_name={model_name} retained for API compat)"
        )

    async def initialize(self) -> None:
        """Mark the service as initialized (mock-only; no model to load).

        Kept as an async method so callers and tests can rely on the
        original lazy-initialization contract.
        """
        if self._initialized:
            return

        logger.info(
            f"Embedding service initialized (mock-only, dim={self._embedding_dim})"
        )
        self._initialized = True

    def is_available(self) -> bool:
        """Check if real embeddings are available.

        Returns:
            Always False: this service only produces deterministic mock
            embeddings. Callers needing real embeddings must route to an
            MCP-side provider.
        """
        return False

    @traced("generate_embedding")
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

        from akosha.observability import add_span_attributes

        add_span_attributes(
            {
                "embedding.text_length": len(text),
                "embedding.mode": "mock",
            }
        )

        result = self._generate_fallback_embedding(text)

        # Record metrics
        record_counter("embedding.generated", 1, {"mode": "mock"})
        record_histogram("embedding.text_length", len(text), {"mode": "mock"})

        return result

    @traced("generate_batch_embeddings")
    async def generate_batch_embeddings(
        self,
        texts: list[str],
        batch_size: int = 32,
    ) -> list[npt.NDArray[np.float32]]:
        """Generate embeddings for multiple texts (batch processing).

        Args:
            texts: List of input texts
            batch_size: Retained for API compatibility; mock generation
                does not benefit from a batch dimension.
        Returns:
            List of embedding vectors
        """
        if not self._initialized:
            await self.initialize()

        from akosha.observability import add_span_attributes

        if not texts:
            return []

        add_span_attributes(
            {
                "batch.size": batch_size,
                "batch.count": len(texts),
            }
        )

        logger.debug(f"Generating mock embeddings for {len(texts)} texts")
        result = [self._generate_fallback_embedding(text) for text in texts]

        # Record metrics
        record_histogram("embedding.batch_size", len(texts), {"mode": "mock"})
        record_counter("embedding.batch.generated", 1, {"mode": "mock"})

        return result

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
        """Rank candidates by similarity to query using vectorized operations.

        Vectorized implementation: O(n) instead of O(n²)
        - Batch similarity computation with np.dot()
        - Top-k selection with np.argpartition()

        Args:
            query_embedding: Query embedding vector
            candidate_embeddings: List of candidate embeddings
            limit: Maximum results to return

        Returns:
            List of (index, similarity_score) tuples, sorted by similarity
        """
        if not candidate_embeddings:
            return []

        # Vectorized similarity computation (O(n) vs O(n²))
        # Stack all candidate embeddings into a matrix
        candidate_matrix = np.array(candidate_embeddings, dtype=np.float32)

        # Compute all similarities at once using dot product
        # query_embedding shape: (384,), candidate_matrix shape: (n, 384)
        # Result shape: (n,)
        similarities = np.dot(candidate_matrix, query_embedding)

        # Find top-k indices using argpartition (O(n) vs O(n log n) for full sort)
        k = min(limit, len(similarities))
        top_k_indices = np.argpartition(-similarities, k - 1)[:k]

        # Sort the top-k results (only k elements, not all n)
        top_k_indices = top_k_indices[np.argsort(-similarities[top_k_indices])]

        # Return as list of (index, similarity) tuples
        results = [(int(idx), float(similarities[idx])) for idx in top_k_indices]

        return results


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
