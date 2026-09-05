"""Tests for DeduplicationService — MinHash primary, SHA-256 fallback.

Audit H2: the prior compute_fingerprint returned a SHA-256 digest
regardless of caller intent, and find_similar always returned []. Both
made dedup "wired up but functionally empty" — a perfect example of
the wire-up-drift pattern this repo's discipline was written to catch.
"""

from __future__ import annotations

import pytest

from akosha.processing.deduplication import DeduplicationService


# ---------------------------------------------------------------------------
# MinHash (production default)
# ---------------------------------------------------------------------------


def test_minhash_fingerprint_is_deterministic() -> None:
    service = DeduplicationService()
    fp1 = service.compute_fingerprint("hello world")
    fp2 = service.compute_fingerprint("hello world")
    assert fp1 == fp2
    assert len(fp1) > 0


def test_minhash_fingerprint_differs_for_different_input() -> None:
    service = DeduplicationService()
    fp1 = service.compute_fingerprint("the quick brown fox")
    fp2 = service.compute_fingerprint("completely unrelated content about stars")
    assert fp1 != fp2


def test_minhash_similar_input_yields_near_identical_fingerprints() -> None:
    """One-word edit to a 45-word passage should leave the MinHash Jaccard
    comfortably above the unrelated-content baseline.

    MinHash's standard error shrinks with num_perm but never reaches
    zero — at 128 perms a 1-of-46 token edit can drop Jaccard by ~5%.
    We pin the floor at 0.7 so the test is stable across runs while
    still being clearly above the 0.5 default threshold.
    """
    service = DeduplicationService()
    fp1 = service.compute_fingerprint(
        "the quick brown fox jumps over the lazy dog " * 5
    )
    fp2 = service.compute_fingerprint(
        "the quick brown fox jumps over the lazy dog " * 5 + "extra"
    )
    sim = service.find_similar(fp1, [fp2])[0][1]
    assert sim > 0.7, f"near-identical inputs scored {sim} (expected > 0.7)"


def test_minhash_dissimilar_input_scores_below_threshold() -> None:
    service = DeduplicationService(threshold=0.9)
    fp1 = service.compute_fingerprint(
        "the quick brown fox jumps over the lazy dog"
    )
    fp2 = service.compute_fingerprint(
        "completely different text about nothing related at all really"
    )
    matches = service.find_similar(fp1, [fp2])
    assert matches == [], f"unrelated inputs unexpectedly matched: {matches}"


def test_minhash_returns_indexed_matches_sorted_by_similarity() -> None:
    """``find_similar`` returns ``(candidate_index, similarity)`` tuples."""
    service = DeduplicationService(threshold=0.5)
    base = "the quick brown fox jumps over the lazy dog " * 3
    near = base + "more"
    unrelated = "completely different content"
    query = service.compute_fingerprint(base)
    cands = [
        service.compute_fingerprint(unrelated),  # index 0: irrelevant
        service.compute_fingerprint(near),  # index 1: should match
    ]
    matches = service.find_similar(query, cands)
    assert matches, "near-duplicate did not match"
    # Only the near-match should appear.
    assert [idx for idx, _ in matches] == [1]
    # The single match must be above threshold.
    assert matches[0][1] >= 0.5


def test_minhash_empty_candidates_returns_empty_list() -> None:
    service = DeduplicationService()
    fp = service.compute_fingerprint("anything")
    assert service.find_similar(fp, []) == []


def test_minhash_threshold_filters() -> None:
    """Custom threshold on a single call overrides the service default."""
    service = DeduplicationService(threshold=0.5)
    fp1 = service.compute_fingerprint("the quick brown fox " * 10)
    fp2 = service.compute_fingerprint("the quick brown fox " * 10 + "z")
    # Below the higher threshold → no match.
    high = service.find_similar(fp1, [fp2], threshold=0.999)
    assert high == []
    # Above the lower threshold → match.
    low = service.find_similar(fp1, [fp2], threshold=0.5)
    assert len(low) == 1


# ---------------------------------------------------------------------------
# SHA-256 fallback (HasherFallback path)
# ---------------------------------------------------------------------------


def test_sha256_backend_produces_byte_exact_fingerprint() -> None:
    """SHA-256 mode: fingerprint is the raw 32-byte digest."""
    service = DeduplicationService(backend="sha256")
    fp = service.compute_fingerprint("hello")
    assert len(fp) == 32
    assert fp == service.compute_fingerprint("hello")


def test_sha256_find_similar_only_matches_byte_identical() -> None:
    service = DeduplicationService(backend="sha256")
    fp = service.compute_fingerprint("hello")
    same = service.compute_fingerprint("hello")
    other = service.compute_fingerprint("world")
    matches = service.find_similar(fp, [same, other])
    # Only the byte-identical candidate matches.
    assert [idx for idx, _ in matches] == [0]
    assert matches[0][1] == 1.0


def test_sha256_find_similar_no_byte_match() -> None:
    service = DeduplicationService(backend="sha256")
    fp = service.compute_fingerprint("hello")
    other = service.compute_fingerprint("hellp")  # one char off → different digest
    assert service.find_similar(fp, [other]) == []


# ---------------------------------------------------------------------------
# is_duplicate (exact-match membership check; backend-independent)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_duplicate_returns_true_for_known_hash() -> None:
    service = DeduplicationService()
    content = "the quick brown fox"
    digest = service._compute_hash(content)
    assert await service.is_duplicate(content, {digest}) is True


@pytest.mark.asyncio
async def test_is_duplicate_returns_false_for_unknown_hash() -> None:
    service = DeduplicationService()
    assert await service.is_duplicate("anything", {"deadbeef" * 8}) is False
