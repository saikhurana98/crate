"""Offset-tolerant fingerprint similarity (pure)."""

from crate.domain.fingerprint import THRESHOLD, is_match, similarity


def _seq(n: int, seed: int = 1) -> list[int]:
    """Deterministic, well-distributed 32-bit sequence (splitmix64 → low 32 bits).

    A plain LCG has badly non-random low bits, which biases XOR bit-agreement; the
    mixer gives ~uniform bits so unrelated sequences agree ~50% as real audio does.
    """
    out = []
    for i in range(n):
        z = (seed + (i + 1) * 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
        z = z ^ (z >> 31)
        out.append(z & 0xFFFFFFFF)
    return out


def test_identical_fingerprints_score_one() -> None:
    fp = _seq(200)
    assert similarity(fp, fp) == 1.0
    assert is_match(fp, fp)


def test_empty_scores_zero() -> None:
    assert similarity([], _seq(10)) == 0.0
    assert similarity(_seq(10), []) == 0.0
    assert not is_match([], [])


def test_different_fingerprints_fall_below_threshold() -> None:
    a, b = _seq(200, seed=1), _seq(200, seed=999)
    score = similarity(a, b)
    assert score < 0.6  # ~half the bits agree by chance
    assert not is_match(a, b)


def test_single_bit_flips_stay_above_threshold() -> None:
    fp = _seq(200)
    noisy = list(fp)
    for i in range(0, 200, 20):  # flip one bit in ~10 of 200 words
        noisy[i] ^= 1
    assert similarity(fp, noisy) >= THRESHOLD
    assert is_match(fp, noisy)


def test_shifted_copy_recovered_by_offset_search() -> None:
    base = _seq(200)
    shifted = [0, 0, 0, 0, 0, *base]  # 5 frames of leading padding
    assert similarity(base, shifted) >= THRESHOLD


def test_tiny_overlap_does_not_fake_a_match() -> None:
    # Two disjoint sequences that share only a couple of aligned words at the
    # extreme offset must not score as a match — min_overlap guards this.
    a, b = _seq(100, seed=2), _seq(100, seed=3)
    assert similarity(a, b, max_offset=98) < THRESHOLD
