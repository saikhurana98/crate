"""Structured track identity (Phase 1).

A request is no longer a flat query string. When an edge knows more about a track
— artist/title split, an ISRC, a duration — it carries that here so sources can
match precisely (ISRC short-circuit, duration prefilter) and de-dup can key on
identity rather than fragile text.

Pure, zero-IO domain type. Every field is optional: the paste-list edge only
knows a free-text line, the Spotify edge knows all four.
"""

from __future__ import annotations

from dataclasses import dataclass

# "Artist - Title" separators: hyphen-minus, em dash, en dash, each surrounded by
# spaces. Built from escapes so the ambiguous dash glyphs never appear in source.
_SEPARATORS = (" - ", " \u2014 ", " \u2013 ")


@dataclass(frozen=True, slots=True)
class TrackIdentity:
    """What we know about the track a request wants. All fields optional."""

    artist: str | None = None
    title: str | None = None
    isrc: str | None = None
    duration_s: int | None = None

    def display(self) -> str:
        """The legacy free-text query: ``"Artist Title"`` (whichever parts exist).

        This is what a source's free-text search still receives, and what the
        request row's ``query`` column stores, so behaviour is unchanged when no
        structured fields are known."""
        parts = [p for p in (self.artist, self.title) if p]
        return " ".join(parts)


def from_query(query: str) -> TrackIdentity:
    """Best-effort parse of a free-text query into an identity.

    ``"Artist - Title"`` (a hyphen/em-dash/en-dash surrounded by spaces) splits
    into artist + title; anything else becomes a title-only identity. No ISRC or
    duration can be inferred from text, so those stay ``None``."""
    text = query.strip()
    for sep in _SEPARATORS:
        if sep in text:
            artist, _, title = text.partition(sep)
            artist, title = artist.strip(), title.strip()
            if artist and title:
                return TrackIdentity(artist=artist, title=title)
    return TrackIdentity(title=text or None)
