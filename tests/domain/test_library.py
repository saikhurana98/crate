"""Library placement rules: sanitising + the <Artist>/<Album>/<NN Title> layout."""

from pathlib import Path

from crate.domain.library import TrackTags, library_relpath, sanitize


def test_sanitize_replaces_illegal_chars_and_trims() -> None:
    assert sanitize("AC/DC: Live?") == "AC_DC_ Live_"
    assert sanitize("  spaced   out  ") == "spaced out"
    assert sanitize("trailing dots...") == "trailing dots"  # Windows/SMB hostile
    assert sanitize("") == "_"  # never empty


def test_sanitize_caps_length() -> None:
    assert len(sanitize("x" * 500)) == 180


def test_relpath_full_tags_with_track_number() -> None:
    tags = TrackTags(artist="Daft Punk", album="Discovery", title="Aerodynamic", track_no=3)
    assert library_relpath(tags, ".flac") == Path("Daft Punk/Discovery/03 Aerodynamic.flac")


def test_relpath_prefers_album_artist_and_handles_multidisc() -> None:
    tags = TrackTags(
        artist="Various", album_artist="VA", album="Best Of", title="Song", track_no=4, disc_no=2
    )
    # album_artist wins the top folder; disc>1 prefixes the track number
    assert library_relpath(tags, ".flac") == Path("VA/Best Of/2-04 Song.flac")


def test_relpath_falls_back_when_tags_missing() -> None:
    assert library_relpath(TrackTags(), ".mp3") == Path("Unknown Artist/Unknown Album/Untitled.mp3")


def test_relpath_disc_one_is_not_prefixed_and_ext_normalised() -> None:
    tags = TrackTags(artist="A", album="B", title="C", track_no=1, disc_no=1)
    # ext given without a dot still yields a single dotted suffix
    assert library_relpath(tags, "flac") == Path("A/B/01 C.flac")
