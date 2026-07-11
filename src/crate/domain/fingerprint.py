"""Acoustic-fingerprint similarity (pure, zero-IO).

Two Chromaprint *raw* fingerprints — each a sequence of 32-bit sub-fingerprints,
one roughly every ~0.12 s — are compared by an offset-tolerant bit-error-rate:
overlay them, count differing bits, slide one against the other to absorb the
encoder padding that shifts otherwise-identical audio by a frame or two, and keep
the best alignment. Same recording ⇒ ~1.0; a different recording ⇒ ~0.5 (half the
bits agree by chance), so the 0.95 gate separates them cleanly.

This is the accuracy core of the review fingerprint guard (ADR-0019). It takes
raw integer sequences (from ``fpcalc -raw``); decoding the file is the injected
infrastructure's job.
"""

from __future__ import annotations

from collections.abc import Sequence

THRESHOLD = 0.95  # a replace below this must not happen silently (ADR-0019)

_MASK32 = 0xFFFFFFFF
_BITS = 32


def similarity(
    a: Sequence[int],
    b: Sequence[int],
    *,
    max_offset: int = 40,
    min_overlap: int | None = None,
) -> float:
    """Best offset-tolerant agreement between two raw fingerprints, in ``0.0..1.0``.

    Slides ``b`` against ``a`` over ``±max_offset`` frames; at each alignment the
    score is ``1 - (differing bits / compared bits)`` over the overlap. Alignments
    whose overlap is shorter than ``min_overlap`` are skipped so a tiny coincidental
    overlap can't fake a high score; when unset it defaults to half the shorter
    fingerprint. Empty input ⇒ ``0.0``.
    """
    if not a or not b:
        return 0.0
    if min_overlap is None:
        min_overlap = max(1, min(len(a), len(b)) // 2)

    best = 0.0
    for offset in range(-max_offset, max_offset + 1):
        # offset > 0 shifts b later (drops b's head); offset < 0 drops a's head.
        head_a = -offset if offset < 0 else 0
        head_b = offset if offset > 0 else 0
        overlap = min(len(a) - head_a, len(b) - head_b)
        if overlap < min_overlap:
            continue
        diff_bits = 0
        for i in range(overlap):
            diff_bits += ((a[head_a + i] ^ b[head_b + i]) & _MASK32).bit_count()
        score = 1.0 - diff_bits / (overlap * _BITS)
        best = max(best, score)
    return best


def is_match(a: Sequence[int], b: Sequence[int], *, threshold: float = THRESHOLD) -> bool:
    """True when two raw fingerprints agree at or above ``threshold``."""
    return similarity(a, b) >= threshold
