"""Shared title-match guard."""

from lbmd.domain.matching import norm_tokens, title_matches


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
