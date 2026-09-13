"""Listening primitives: the per-song merge key + timestamp normalisation."""

from __future__ import annotations

import pytest

from crate.domain.listening import clean_title, merge_key, norm_played_at, primary_artist


def test_merge_key_ignores_case_punctuation_and_articles() -> None:
    assert merge_key("The Weeknd", "Blinding Lights!") == merge_key("the weeknd", "blinding lights")


@pytest.mark.parametrize(
    "title",
    [
        "Bohemian Rhapsody - 2011 Remaster",
        "Bohemian Rhapsody (Remastered 2011)",
        "Bohemian Rhapsody [2011 Remaster]",
        "Bohemian Rhapsody - Remastered",
        "Bohemian Rhapsody \u2013 Digitally Remastered 2011",
        "Bohemian Rhapsody - 2011 Remastered Version",
    ],
)
def test_merge_key_folds_remasters_into_the_original(title: str) -> None:
    assert merge_key("Queen", title) == merge_key("Queen", "Bohemian Rhapsody")


def test_merge_key_drops_featured_credits() -> None:
    assert merge_key("Drake", "Nice For What (feat. Big Freedia)") == merge_key(
        "Drake", "Nice For What"
    )
    assert merge_key("X", "Song [with Y]") == merge_key("X", "Song")


def test_merge_key_uses_the_primary_artist_only() -> None:
    assert merge_key("Daft Punk, Pharrell Williams", "Get Lucky") == merge_key(
        "Daft Punk", "Get Lucky"
    )


@pytest.mark.parametrize("variant", ["Song - Live", "Song (Acoustic)", "Song - Radio Edit"])
def test_merge_key_keeps_different_recordings_apart(variant: str) -> None:
    assert merge_key("A", variant) != merge_key("A", "Song")


def test_merge_key_keeps_non_latin_titles_distinct() -> None:
    a = merge_key("Arijit Singh", "\u0924\u0941\u092e \u0939\u0940 \u0939\u094b")
    b = merge_key(
        "Arijit Singh", "\u091a\u0928\u094d\u0928\u093e \u092e\u0947\u0930\u0947\u092f\u093e"
    )
    assert a != b and a != merge_key("Arijit Singh", "")  # never collapses to artist-only


def test_merge_key_treats_accents_as_plain_letters() -> None:
    assert merge_key("Beyonc\u00e9", "Caf\u00e9") == merge_key("Beyonce", "Cafe")


def test_helpers_tolerate_missing_values() -> None:
    assert primary_artist(None) == "" and clean_title(None) == ""
    assert merge_key(None, None) == "tok:"


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("2026-09-01T10:05:00.123Z", "2026-09-01T10:05:00Z"),  # API form
        ("2026-09-01T10:05:00Z", "2026-09-01T10:05:00Z"),  # export form
        ("2026-09-01T15:35:00+05:30", "2026-09-01T10:05:00Z"),  # offset -> UTC
        ("2026-09-01T10:05:00", "2026-09-01T10:05:00Z"),  # naive read as UTC
    ],
)
def test_norm_played_at_forms(raw: str, want: str) -> None:
    assert norm_played_at(raw) == want


def test_norm_played_at_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        norm_played_at("yesterday")
