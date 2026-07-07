"""TrackIdentity: display() rendering + best-effort from_query() parsing."""

import pytest

from crate.domain.identity import TrackIdentity, from_query


def test_display_renders_artist_then_title() -> None:
    assert TrackIdentity(artist="Daft Punk", title="Get Lucky").display() == "Daft Punk Get Lucky"


def test_display_uses_whichever_parts_exist() -> None:
    assert TrackIdentity(title="Rhubarb").display() == "Rhubarb"
    assert TrackIdentity(artist="Aphex Twin").display() == "Aphex Twin"
    assert TrackIdentity().display() == ""
    # isrc/duration never appear in the free-text query
    assert TrackIdentity(title="X", isrc="US123", duration_s=200).display() == "X"


def test_from_query_splits_hyphen_shape() -> None:
    got = from_query("Daft Punk - Get Lucky")
    assert got == TrackIdentity(artist="Daft Punk", title="Get Lucky")


@pytest.mark.parametrize("dash", ["-", "\u2014", "\u2013"])
def test_from_query_splits_on_each_dash_variant(dash: str) -> None:
    got = from_query(f"Burial {dash} Archangel")
    assert got == TrackIdentity(artist="Burial", title="Archangel")


def test_from_query_title_only_when_no_separator() -> None:
    assert from_query("Aphex Twin Rhubarb") == TrackIdentity(title="Aphex Twin Rhubarb")


def test_from_query_ignores_hyphen_without_surrounding_spaces() -> None:
    # a hyphenated word is not an artist/title separator
    assert from_query("Jay-Z 99 Problems") == TrackIdentity(title="Jay-Z 99 Problems")


def test_from_query_empty_string_is_all_none() -> None:
    assert from_query("   ") == TrackIdentity()


def test_from_query_dangling_separator_falls_back_to_title() -> None:
    # a separator with an empty side is not a valid split -> title-only
    assert from_query("Song - ") == TrackIdentity(title="Song -")


def test_identity_is_frozen() -> None:
    t = TrackIdentity(title="X")
    with pytest.raises(AttributeError):
        t.title = "y"  # type: ignore[misc]
