"""A single searchable download candidate (Review-tab feature).

The cascade normally does search -> select -> download in one call and keeps only
the winner. The review flow needs the *intermediate* step exposed: every
title-matching result a source found, so the user can compare them side by side
(source / tier / quality / duration / match %) and re-download the one they pick.

A :class:`Candidate` is that exposed intermediate — pure metadata plus ``ref``,
the source-specific opaque handle needed to fetch *exactly* this result later
(Qobuz track id, Deezer id, arcod download payload, yt-dlp video id, …) without
re-running the search. Pure, zero-IO domain type.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Candidate:
    """One result a source returned for a review query, fetchable via ``ref``."""

    source: str
    tier: int
    ref: str  # opaque, source-specific handle to fetch exactly this result
    title: str
    artist: str
    album: str | None
    quality_label: str
    lossless: bool
    duration_s: int | None
    size_bytes: int | None
