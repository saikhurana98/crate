"""Shared title-match guard."""

from crate.domain.matching import (
    DURATION_TOLERANCE_S,
    artist_matches,
    duration_prefilter,
    match_score,
    norm_tokens,
    title_matches,
)


def test_duration_prefilter_drops_out_of_tolerance_candidates() -> None:
    cands = [("a", 200), ("b", 250), ("c", 203)]
    kept = duration_prefilter(cands, 202, lambda c: c[1])
    assert kept == [("a", 200), ("c", 203)]  # 250 is off by 48 -> dropped


def test_duration_prefilter_keeps_pool_when_it_would_empty() -> None:
    cands = [("a", 480), ("b", 500)]
    # nothing near 200 -> keep all (a wrong duration beats no result)
    assert duration_prefilter(cands, 200, lambda c: c[1]) == cands


def test_duration_prefilter_noop_when_want_unknown() -> None:
    cands = [("a", 480)]
    assert duration_prefilter(cands, None, lambda c: c[1]) is cands


def test_duration_prefilter_keeps_unknown_duration_candidates() -> None:
    cands = [("a", None), ("b", 480)]
    # unknown-duration candidate is never dropped; the off-duration one is
    assert duration_prefilter(cands, 200, lambda c: c[1]) == [("a", None)]


def test_duration_tolerance_default_is_seven() -> None:
    assert DURATION_TOLERANCE_S == 7


def test_norm_tokens_drops_filler_and_short_words() -> None:
    assert norm_tokens("The Official Video - Marjaani (Full Song)") == ["marjaani"]


def test_title_matches_accepts_when_title_in_query() -> None:
    assert title_matches("Marjaani", "Marjaani Pritam") is True
    assert title_matches("I Hate Luv Storys", "I Hate Luv Storys Vishal-Shekhar") is True


def test_title_matches_rejects_different_song() -> None:
    assert title_matches("Channa Mereya", "Marjaani Pritam") is False
    assert title_matches("BENNY", "Besharmi Ki Height") is False


def test_title_matches_empty_title_is_false() -> None:
    assert title_matches("", "anything") is False


def test_match_score_prefers_the_artist_that_matches_the_query() -> None:
    q = "Dimitri Vegas, MOGUAI, Like Mike Mammoth"
    right = match_score("Mammoth", "Dimitri Vegas & Like Mike, MOGUAI", q)
    wrong = match_score("Mammoth", "ElCano", q)
    assert right > wrong  # same title, but the correct artist scores higher


def test_match_score_title_still_dominates() -> None:
    q = "Pritam Marjaani"
    # right title beats a wrong title even if the wrong one shares an artist token
    assert match_score("Marjaani", "Pritam", q) > match_score("Channa Mereya", "Pritam", q)


def test_artist_matches_rejects_unrelated_artist() -> None:
    # the real incident: a request for "Alfaaz" must not accept an "Alka Yagnik" track
    assert artist_matches("Alka Yagnik", "Alfaaz") is False


def test_artist_matches_single_artist_inside_multi_artist_candidate() -> None:
    # a single requested artist fully present in a multi-artist credit passes
    assert artist_matches("Alfaaz, Yo Yo Honey Singh", "Alfaaz") is True


def test_artist_matches_collaborator_of_multi_artist_request() -> None:
    # a collaborator of a multi-artist request passes (smaller set fully inside)
    assert artist_matches("Yo Yo Honey Singh", "Alfaaz, Yo Yo Honey Singh") is True


def test_artist_matches_exact_is_true() -> None:
    assert artist_matches("Alfaaz", "Alfaaz") is True


def test_artist_matches_empty_want_is_true() -> None:
    # never gate on missing data
    assert artist_matches("Alka Yagnik", "") is True


def test_artist_matches_empty_candidate_is_true() -> None:
    assert artist_matches("", "Alfaaz") is True


def test_artist_matches_partial_overlap_at_threshold() -> None:
    # one of two requested tokens present -> 0.5 overlap -> passes default 0.5
    assert artist_matches("Arijit Sharma", "Arijit Singh") is True


def test_artist_matches_zero_overlap_lenient_threshold() -> None:
    # the ytdlp channel gate uses a tiny threshold: any shared token passes,
    # only a completely-disjoint channel is rejected
    assert artist_matches("Alfaaz Official", "Alfaaz", threshold=1e-9) is True
    assert artist_matches("SonyMusic", "Alfaaz", threshold=1e-9) is False
