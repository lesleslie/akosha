"""Deduplication service for conversations."""

from __future__ import annotations

import hashlib
import logging
from typing import Literal

logger = logging.getLogger(__name__)


def _minhash_to_bytes(minhash: object) -> bytes:
    """Serialize a datasketch ``MinHash`` to bytes via ``hashvalues.tobytes()``.

    datasketch >=2.0 dropped the legacy ``.hashbytes`` property; the
    current API is to access the underlying ``hashvalues`` numpy array
    directly and call ``tobytes()``.
    """
    return bytes(minhash.hashvalues.tobytes())  # type: ignore[attr-defined]


def _bytes_to_minhash(raw: bytes, num_perm: int) -> object:
    """Inverse of :func:`_minhash_to_bytes` — rebuild a ``MinHash``.

    Uses ``np.frombuffer`` to read the raw bytes back into a uint32
    array of the same dtype/length, then assigns to ``hashvalues`` on
    a freshly constructed ``MinHash``.
    """
    import numpy as np
    from datasketch import MinHash  # type: ignore[import-not-found]

    mh = MinHash(num_perm=num_perm)
    mh.hashvalues = np.frombuffer(raw, dtype=mh.hashvalues.dtype).copy()  # type: ignore[attr-defined]
    return mh


class DeduplicationService:
    """Conversation deduplication service.

    Provides exact and fuzzy deduplication:
    - ``backend="sha256"`` (HasherFallback): exact-match via SHA-256 digests.
    - ``backend="minhash"`` (default): fuzzy similarity via datasketch MinHash.

    The MinHash path is the production default; SHA-256 exists as a
    deterministic fallback when ``datasketch`` cannot be imported or
    when the deployment wants exact-match only.

    Two interfaces:
    - ``is_duplicate(content, existing_hashes)``: exact-match membership check.
    - ``compute_fingerprint`` + ``find_similar``: fuzzy similarity search.

    Both fingerprint interfaces are synchronous because datasketch's
    MinHash is CPU-bound and the async call site would just wrap a sync
    computation. Async wrappers (``await service.compute_fingerprint(...)``)
    work via the implicit event-loop return.
    """

    def __init__(
        self,
        *,
        backend: Literal["minhash", "sha256"] = "minhash",
        num_perm: int = 128,
        threshold: float = 0.5,
    ) -> None:
        self._backend = backend
        self._num_perm = num_perm
        self._threshold = threshold

    async def is_duplicate(
        self,
        content: str,
        existing_hashes: set[str],
    ) -> bool:
        """Check if content is a duplicate (exact match).

        Args:
            content: Conversation content
            existing_hashes: Set of existing SHA-256 hex digests

        Returns:
            True if duplicate, False otherwise
        """
        content_hash = self._compute_hash(content)
        return content_hash in existing_hashes

    @staticmethod
    def _compute_hash(content: str) -> str:
        """Compute SHA-256 hex digest of content (used by ``is_duplicate``)."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def compute_fingerprint(self, content: str) -> bytes:
        """Compute a similarity fingerprint for ``content``.

        Returns:
            ``backend="minhash"`` → ``hashvalues.tobytes()`` payload
            (datasketch >=2.0 native serialization, Jaccard-approximating).
            ``backend="sha256"`` → raw 32-byte SHA-256 digest
            (exact-match fingerprint; Jaccard similarity is 1.0 iff
            byte-identical content).
        """
        if self._backend == "minhash":
            try:
                from datasketch import MinHash  # type: ignore[import-not-found]
            except ImportError:
                logger.warning(
                    "datasketch not installed; falling back to SHA-256 fingerprint"
                )
                return hashlib.sha256(content.encode("utf-8")).digest()
            m = MinHash(num_perm=self._num_perm)
            for word in content.split():
                m.update(word.encode("utf-8"))
            return _minhash_to_bytes(m)
        return hashlib.sha256(content.encode("utf-8")).digest()

    def find_similar(
        self,
        fingerprint: bytes,
        candidates: list[bytes],
        threshold: float | None = None,
    ) -> list[tuple[int, float]]:
        """Find candidate fingerprints similar to ``fingerprint``.

        Returns a list of ``(candidate_index, similarity)`` pairs
        sorted by similarity descending, filtered by ``threshold``
        (defaults to the service-level ``threshold``).
        """
        if threshold is None:
            threshold = self._threshold
        if not candidates:
            return []

        if self._backend == "minhash":
            try:
                from datasketch import MinHash  # noqa: F401  # type: ignore[import-not-found]
            except ImportError:
                # SHA-256 fallback: only byte-identical matches count.
                return [
                    (i, 1.0)
                    for i, cand in enumerate(candidates)
                    if cand == fingerprint
                ]
            return _minhash_jaccard(fingerprint, candidates, threshold, self._num_perm)
        # SHA-256 backend: exact byte equality is the only similarity.
        return [
            (i, 1.0)
            for i, cand in enumerate(candidates)
            if cand == fingerprint
        ]


def _minhash_jaccard(
    query_bytes: bytes,
    candidate_bytes_list: list[bytes],
    threshold: float,
    num_perm: int,
) -> list[tuple[int, float]]:
    """Compute Jaccard similarities between ``query_bytes`` (a serialized
    MinHash) and each candidate using datasketch's native Jaccard estimator.

    Sorting + threshold filtering happen here so the public method
    stays a thin shim.
    """
    query = _bytes_to_minhash(query_bytes, num_perm)
    scored: list[tuple[int, float]] = []
    for i, cand_bytes in enumerate(candidate_bytes_list):
        cand = _bytes_to_minhash(cand_bytes, num_perm)
        sim = query.jaccard(cand)
        if sim >= threshold:
            scored.append((i, float(sim)))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored
