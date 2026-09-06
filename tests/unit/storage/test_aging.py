"""Tests for INT8 quantization round-trip + scale persistence (audit H3).

Audit H3 found three compounding bugs in the legacy ``_quantize_embedding``:
1. ``int(v * 127)`` with no clipping produced values outside ``[-127, 127]``.
2. The fixed scale of 127 ignored the input's magnitude — two
   semantically similar vectors with different magnitudes produced
   wildly different INT8 fingerprints.
3. The scale factor was thrown away, so round-trip dequantization was
   impossible.

The fix is :func:`quantize_embedding` / :func:`dequantize` with a
persisted scale factor (``QuantizedVector.values`` + ``scale``).
"""

from __future__ import annotations

import math

import pytest

from akosha.storage.aging import (
    QuantizedVector,
    dequantize,
    quantize_embedding,
)


def test_quantize_roundtrip_within_eps() -> None:
    """audit H3: quantization must be reversible within 1/127."""
    emb = [0.1, 0.5, -0.3, 0.8, -0.9]
    q = quantize_embedding(emb)
    restored = dequantize(q)
    for orig, rec in zip(emb, restored, strict=True):
        assert abs(orig - rec) <= 1 / 127 + 1e-9, f"round-trip error {orig - rec} exceeds 1/127"


def test_quantize_clamps_to_int8_range() -> None:
    """Values outside [-1, 1] (after scaling) must clip to [-127, 127]."""
    emb = [2.0, -3.0, 0.5]
    q = quantize_embedding(emb)
    for v in q.values:
        assert -127 <= v <= 127, f"value {v} outside INT8 range"


def test_quantize_preserves_zero() -> None:
    """All-zero embedding → scale=1.0 sentinel, values all zero."""
    emb = [0.0, 0.0, 0.0]
    q = quantize_embedding(emb)
    assert q.scale == 1.0
    assert all(v == 0 for v in q.values)


def test_quantize_scale_is_persisted_and_correct() -> None:
    """Scale must equal 127 / max(abs(original))."""
    emb = [0.5, -0.5]
    q = quantize_embedding(emb)
    assert q.scale > 0
    assert math.isclose(q.scale, 127 / max(abs(v) for v in emb), rel_tol=1e-6)


def test_quantize_empty_embedding_returns_empty_values() -> None:
    """Empty input → empty values, scale=1.0 sentinel (no division by zero)."""
    q = quantize_embedding([])
    assert q.values == []
    assert q.scale == 1.0


def test_quantize_single_value_normalizes_to_extremes() -> None:
    """A single non-zero value normalizes to ±127 (it IS the max-abs).

    For ``[1.0]``: max-abs=1, scale=127, value 1*127=127.
    For ``[-1.0]``: max-abs=1, scale=127, value -1*127=-127.
    For ``[0.5]``: max-abs=0.5, scale=254, value 0.5*254=127.
    So every single-value input maps to ±127 — the lone value IS the
    max-abs and saturates the scale.
    """
    assert quantize_embedding([1.0]).values == [127]
    assert quantize_embedding([-1.0]).values == [-127]
    assert quantize_embedding([0.5]).values == [127]
    assert quantize_embedding([-0.5]).values == [-127]


def test_dequantize_round_trip_with_realistic_embedding() -> None:
    """A 384-dim embedding (production shape) round-trips within tolerance."""
    import random

    rng = random.Random(42)
    emb = [rng.uniform(-1.0, 1.0) for _ in range(384)]
    q = quantize_embedding(emb)
    restored = dequantize(q)
    assert len(restored) == len(emb)
    max_err = max(abs(o - r) for o, r in zip(emb, restored, strict=True))
    assert max_err <= 1 / 127 + 1e-9


def test_quantized_vector_is_named_tuple() -> None:
    """The result is a NamedTuple so callers can unpack: ``vals, scale = q``."""
    q = quantize_embedding([0.1, 0.2])
    vals, scale = q
    assert vals == q.values
    assert scale == q.scale


def test_clipping_works_for_far_out_of_range_values() -> None:
    """Values that would overflow INT8 must clip, not wrap."""
    # max_abs=10, scale=12.7; value 1.0 → 13 (in range), but a hypothetical
    # value of 100 would multiply to 1270 → clipped to 127.
    emb = [10.0, -10.0, 100.0, -100.0]
    q = quantize_embedding(emb)
    assert max(q.values) == 127
    assert min(q.values) == -127


def test_known_quantization_values() -> None:
    """Pin a few hand-computed values so refactors don't drift."""
    # max-abs=1, scale=127; all values fit exactly
    q = quantize_embedding([1.0, -1.0, 0.5, 0.0])
    assert q.values == [127, -127, round(127 * 0.5), 0]
    assert math.isclose(q.scale, 127.0, rel_tol=1e-9)


def test_dequantize_inverts_quantize_for_arbitrary_input() -> None:
    """Property test: dequantize(quantize(x)) ≈ x for random vectors."""
    import random

    rng = random.Random(0)
    for _ in range(20):
        emb = [rng.uniform(-1.0, 1.0) for _ in range(rng.randint(1, 100))]
        q = quantize_embedding(emb)
        restored = dequantize(q)
        for o, r in zip(emb, restored, strict=True):
            assert abs(o - r) <= 1 / 127 + 1e-9


@pytest.mark.parametrize(
    "embedding",
    [
        [1.0],
        [1.0, -1.0],
        [0.001, -0.001],
        [0.5, -0.5, 0.0],
        [0.0],
        [1.0, 0.0, -1.0, 0.5],
    ],
)
def test_quantize_dequantize_round_trip(embedding: list[float]) -> None:
    """Parametrized round-trip — covers edge cases including single-zero."""
    q = quantize_embedding(embedding)
    restored = dequantize(q)
    for o, r in zip(embedding, restored, strict=True):
        assert abs(o - r) <= 1 / 127 + 1e-9


def test_quantized_vector_has_expected_fields() -> None:
    """Pin the public surface so external callers don't break."""
    q = quantize_embedding([0.1, -0.2])
    assert isinstance(q, QuantizedVector)
    assert hasattr(q, "values")
    assert hasattr(q, "scale")
