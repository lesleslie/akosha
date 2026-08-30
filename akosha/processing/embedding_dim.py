"""Embedding dimension contract — single source of truth for "what dim does this backend produce".

Used by :mod:`akosha.storage.hot_store` and
:mod:`akosha.storage.pgvector_hot_store` to size their ``FLOAT[N]`` /
``vector(N)`` schema at construction time, and by
:meth:`akosha.main.AkoshaApplication.start` to wire the resolved dim
through ``create_hot_store(embedding_dim=N)`` so the schema is built
against the active backend's dim rather than the historical 384 mock
default.

Resolution order:

1. ``embedding_service.dimension()`` when the service has been
   initialized (non-``None``).
2. :data:`BACKEND_DEFAULT_DIMS` lookup keyed on
   ``embedding_service.backend_name()`` when the service exists but
   hasn't probed a real backend (oneiric keeps the dim ``None`` until
   the first ``initialize()`` finishes).
3. ``384`` as the final fallback — matches the legacy mock shape and the
   pre-fix schema, so existing tests / fixtures that never initialize
   the service continue to work.

Note on naming: the plan text references ``backend_dim()`` but the
current oneiric ``EmbeddingService`` API exposes this accessor as
``dimension()`` (see
``.venv/lib/python3.14/site-packages/oneiric/adapters/observability/embeddings.py:148``).
Both names refer to the same underlying attribute
(``EmbeddingService._backend_dim: int | None``). The plan's
``backend_dim()`` will resolve once oneiric renames the accessor, but
for now :func:`resolve_embedding_dim` calls ``dimension()`` and treats
``None`` as "not initialized".
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


#: Fallback dim per backend when the service has not yet been
#: initialized (oneiric keeps ``_backend_dim = None`` until
#: ``initialize()`` runs).
#: Updated by akosha recon 2026-08-29: dim per oneiric backend table.
BACKEND_DEFAULT_DIMS: dict[str, int] = {
    "mock": 384,
    "model2vec": 256,
    "llama_cpp": 768,
    "ollama": 768,
    "minimax": 1024,
}


#: Hard-coded fallback used when no service is provided and
#: ``BACKEND_DEFAULT_DIMS`` has no match for the active backend. Chosen
#: to match the legacy ``EmbeddingService`` mock shape and the existing
#: in-test 384-dim fixtures.
DEFAULT_DIMENSION: int = 384


def resolve_embedding_dim(
    embedding_service: object | None = None,
) -> int:
    """Return the embedding vector dimension for the active backend.

    Resolution order:

    1. ``embedding_service.dimension()`` when it returns a non-``None``
       int (i.e. the service has been initialized).
    2. :data:`BACKEND_DEFAULT_DIMS` lookup keyed on
       ``embedding_service.backend_name()`` when a service exists but
       hasn't probed a real backend (or the accessor returns ``None``).
    3. :data:`DEFAULT_DIMENSION` (384) when no service is provided or
       the backend name has no entry in the defaults table.

    Args:
        embedding_service: Optional service exposing ``dimension()`` and
            ``backend_name()``. ``None`` is the common case for tests
            that never call ``get_embedding_service().initialize()``.

    Returns:
        The resolved embedding vector dimension. Always an ``int`` — the
        historical 384 fallback keeps existing test fixtures green.
    """
    if embedding_service is not None:
        dim_attr = getattr(embedding_service, "_backend_dim", None)
        dim_method = getattr(embedding_service, "dimension", None)
        backend_name = getattr(embedding_service, "backend_name", None)
        # Prefer the live ``dimension()`` method (post-initialize) but
        # accept the underlying attribute for callers that have not yet
        # awaited initialize(). Either source returning a real int wins.
        live_dim = None
        if callable(dim_method):
            try:
                live_dim = dim_method()
            except Exception as exc:
                logger.debug(
                    "akosha.embedding_dim.dimension_call_failed",
                    extra={"error": str(exc)},
                )
                live_dim = None
        resolved_dim = live_dim if isinstance(live_dim, int) else dim_attr
        if isinstance(resolved_dim, int):
            logger.info(
                "akosha.embedding_dim.resolved",
                extra={
                    "backend_name": backend_name() if callable(backend_name) else None,
                    "backend_dim": resolved_dim,
                    "source": "backend_dim",
                },
            )
            return resolved_dim
        if callable(backend_name):
            try:
                name = backend_name()
            except Exception as exc:
                logger.debug(
                    "akosha.embedding_dim.backend_name_failed",
                    extra={"error": str(exc)},
                )
                name = None
            if name and name in BACKEND_DEFAULT_DIMS:
                fallback_dim = BACKEND_DEFAULT_DIMS[name]
                logger.info(
                    "akosha.embedding_dim.resolved",
                    extra={
                        "backend_name": name,
                        "backend_dim": fallback_dim,
                        "source": "default_table",
                    },
                )
                return fallback_dim
    logger.info(
        "akosha.embedding_dim.resolved",
        extra={
            "backend_name": None,
            "backend_dim": DEFAULT_DIMENSION,
            "source": "fallback",
        },
    )
    return DEFAULT_DIMENSION


__all__ = [
    "BACKEND_DEFAULT_DIMS",
    "DEFAULT_DIMENSION",
    "resolve_embedding_dim",
]
