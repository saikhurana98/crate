"""Listening-ledger primitives (pure): a play, the per-song merge key, and
timestamp normalisation.

A song is keyed on its **primary artist + cleaned title** rather than a Spotify
id: Spotify relinks and duplicates ids across releases and remasters, which
would split one song's plays across several keys and undercount it. Version
words (live, remix, acoustic, edit, …) are kept, so different recordings stay
different songs.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

# "(feat. X)", "[with Y]", "(ft. Z)" — featured credits, same recording.
_FEAT = re.compile(r"\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s[^\)\]]*[\)\]]", re.IGNORECASE)
# "- 2011 Remaster", "(Remastered 2009)", "[2015 Remaster]", "- Digitally Remastered"
_REMASTER = re.compile(
    r"\s*(?:[-\u2013\u2014]\s*|[\(\[]\s*)"
    r"(?:\d{4}\s+)?(?:digital(?:ly)?\s+)?remaster(?:ed)?(?:\s+\d{4})?(?:\s+version)?"
    r"\s*[\)\]]?\s*$",
    re.IGNORECASE,
)
_FILLER = frozenset({"the", "a", "an", "feat", "ft", "featuring"})
_LATIN_ACCENTS = ("\u0300", "\u036f")  # combining diacritics: café == cafe


def _tokens(text: str) -> list[str]:
    """Script-aware tokens: letters, marks and digits of any script form words
    (so Devanagari/CJK titles keep distinct keys), Latin accents are dropped, the
    rest separates. Casefolded; filler words removed."""
    folded = unicodedata.normalize("NFKD", text).casefold()
    words: list[str] = []
    cur: list[str] = []
    for ch in folded:
        if _LATIN_ACCENTS[0] <= ch <= _LATIN_ACCENTS[1]:
            continue
        if unicodedata.category(ch)[0] in "LMN":
            cur.append(ch)
        elif cur:
            words.append("".join(cur))
            cur = []
    if cur:
        words.append("".join(cur))
    return [w for w in words if w not in _FILLER]


def primary_artist(artist: str | None) -> str:
    """The first credited artist (sources join several with ", ")."""
    return (artist or "").split(",")[0].strip()


def clean_title(title: str | None) -> str:
    """The title without featured-artist credits or remaster decoration."""
    return _REMASTER.sub("", _FEAT.sub("", title or "")).strip()


def merge_key(artist: str | None, title: str | None) -> str:
    """Stable per-song key: ``tok:`` + primary-artist tokens + clean-title tokens."""
    return "tok:" + " ".join(_tokens(primary_artist(artist)) + _tokens(clean_title(title)))


def norm_played_at(raw: str) -> str:
    """A play timestamp as UTC whole seconds, ``YYYY-MM-DDTHH:MM:SSZ``.

    Accepts the API form (milliseconds + ``Z``), the export form (``Z``), an
    explicit offset, or a naive time (read as UTC). Raises ValueError otherwise."""
    dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class Play:
    """One ledger row (the owning user is passed alongside)."""

    played_at: str
    merge_key: str
    track_uri: str | None
    artist: str | None
    title: str | None
    album: str | None = None
    ms_played: int | None = None
