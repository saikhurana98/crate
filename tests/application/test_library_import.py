"""library_import.to_identity — Spotify track dict -> TrackIdentity mapping."""

from __future__ import annotations

from crate.application.library_import import to_identity
from crate.domain.identity import TrackIdentity


def test_to_identity_maps_fields() -> None:
    track = {"query": "A T", "artist": "A", "title": "T", "isrc": "US123", "duration_s": 210}
    assert to_identity(track) == TrackIdentity("A", "T", "US123", 210)


def test_to_identity_tolerates_missing_fields() -> None:
    assert to_identity({"query": "x"}) == TrackIdentity(None, None, None, None)
