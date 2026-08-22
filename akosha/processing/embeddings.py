"""Embedding service shim — delegates to ``oneiric``.

Akosha used to own its own mock-only ``EmbeddingService``. After the
hybrid-chain refactor in ``oneiric`` (see
``oneiric/docs/plans/2026-08-22-hybrid-embeddings-design.md``), Akosha
delegates here so callers can keep importing
``akosha.processing.embeddings`` while the actual implementation lives
in oneiric.

The shim exposes:

- ``EmbeddingService`` — subclass of oneiric's ``EmbeddingService`` that
  keeps the legacy akosha API surface (``generate_embedding``,
  ``generate_batch_embeddings``, ``compute_similarity``,
  ``rank_by_similarity``, ``_initialized`` flag) so downstream code
  doesn't need to change.
- ``get_embedding_service()`` — memoized factory that returns the
  singleton service for this process.

Why a shim rather than a direct migration of callers to oneiric?
The shim preserves the public import path for any external consumer
(tools, tests, third-party code that imports
``akosha.processing.embeddings``) without requiring coordinated
changes across the Bodai ecosystem.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
from oneiric.adapters.observability.embeddings import EmbeddingService as _OneiricEmbeddingService
from oneiric.adapters.observability.embedding_settings import EmbeddingSettings

__all__ = [
    "EmbeddingService",
    "EmbeddingSettings",
    "get_embedding_service",
]


# ---------------------------------------------------------------------------
# Backwards-compatibility subclass
# ---------------------------------------------------------------------------


class EmbeddingService(_OneiricEmbeddingService):
    """Akosha-flavoured EmbeddingService that delegates to oneiric.

    Adds the legacy akosha async API (``generate_embedding``,
    ``generate_batch_embeddings``) and similarity helpers
    (``compute_similarity``, ``rank_by_similarity``) on top of oneiric's
    ``encode`` / ``encode_batch`` / ``embed_trace``. The ``_initialized``
    attribute is preserved for any legacy callers / fixtures that
    inspect it.

    Once ``await initialize()`` has been awaited, ``is_available()``
    reflects whether a real backend probed successfully (it used to
    always be ``False`` because the akosha implementation was mock-only).
    """

    def __init__(
        self,
        settings: EmbeddingSettings | None = None,
        model_name: str | None = None,
    ) -> None:
        super().__init__(settings=settings, model_name=model_name)
        # Legacy attribute: callers used to check this to know whether
        # ``initialize`` had been awaited. Now it reflects whether
        # ``initialize`` has actually completed (i.e. the chain has
        # been probed).
        self._initialized: bool = False

    async def initialize(self) -> None:  # type: ignore[override]
        """Probe the backend chain and mark the service initialized."""
        await super().initialize()
        self._initialized = True

    # -- Legacy akosha API (delegating to the oneiric hybrid chain) -----

    async def generate_embedding(
        self,
        text: str,
    ) -> np.ndarray:
        """Legacy akosha alias for ``encode``.

        Returns the embedding vector for a single text. Falls back to
        a deterministic mock when the chain produced no real backend.
        """
        if not self._initialized:
            await self.initialize()
        try:
            return await self.encode(text)
        except (RuntimeError, OSError, ValueError):
            return self._generate_fallback_embedding(text)

    async def generate_batch_embeddings(
        self,
        texts: list[str],
        batch_size: int = 32,
    ) -> list[np.ndarray]:
        """Legacy akosha alias for ``encode_batch``.

        ``batch_size`` is retained for API compatibility; the hybrid
        chain doesn't exploit batching (the model2vec backend calls
        ``model.encode`` which is already batched).
        """
        if not self._initialized:
            await self.initialize()
        if not texts:
            return []
        try:
            return await self.encode_batch(texts)
        except (RuntimeError, OSError, ValueError):
            return [self._generate_fallback_embedding(t) for t in texts]

    async def compute_similarity(
        self,
        embedding1: np.ndarray,
        embedding2: np.ndarray,
    ) -> float:
        """Cosine similarity between two embeddings."""
        emb1 = np.asarray(embedding1, dtype=np.float32)
        emb2 = np.asarray(embedding2, dtype=np.float32)
        return float(np.dot(emb1, emb2))

    async def rank_by_similarity(
        self,
        query_embedding: np.ndarray,
        candidate_embeddings: list[np.ndarray],
        limit: int = 10,
    ) -> list[tuple[int, float]]:
        """Rank candidate embeddings by cosine similarity to the query."""
        if not candidate_embeddings:
            return []
        candidate_matrix = np.asarray(candidate_embeddings, dtype=np.float32)
        similarities = candidate_matrix @ np.asarray(
            query_embedding, dtype=np.float32
        )
        k = min(limit, len(similarities))
        top_k_indices = np.argpartition(-similarities, k - 1)[:k]
        top_k_indices = top_k_indices[np.argsort(-similarities[top_k_indices])]
        return [(int(idx), float(similarities[idx])) for idx in top_k_indices]


# ---------------------------------------------------------------------------
# Singleton factory — preserves the pre-refactor ``get_embedding_service``
# API so call sites don't have to change.
# ---------------------------------------------------------------------------

_embedding_service: EmbeddingService | None = None


def get_embedding_service() -> EmbeddingService:
    """Get the process-wide singleton EmbeddingService.

    The singleton is created on first call. ``initialize()`` is awaited
    by the akosha lifespan (``akosha.mcp.server``) after grabbing the
    instance via this factory.

    Returns:
        The singleton ``EmbeddingService``.
    """
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    return _embedding_service
