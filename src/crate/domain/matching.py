"""Title-match guard shared by source adapters.

Fuzzy search / bot replies routinely surface a different song with a related
name. This rejects a candidate whose title's significant tokens are not mostly
present in the query, so a wrong-song result is skipped instead of accepted.
Pure, dependency-free.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TypeVar

# Drop a candidate whose runtime is off the wanted duration by more than this
# (shared tolerance for the source-side prefilter and the post-download check).
DURATION_TOLERANCE_S = 7

_T = TypeVar("_T")


def duration_prefilter(
    candidates: list[_T],
    want_duration_s: int | None,
    of: Callable[[_T], int | None],
    tolerance_s: int = DURATION_TOLERANCE_S,
) -> list[_T]:
    """Keep only candidates within ``tolerance_s`` of ``want_duration_s``.

    ``of`` extracts a candidate's duration (``None`` when unknown → the candidate
    is never dropped, since we can't compare). If the wanted duration is unknown,
    or the filter would empty the pool, the original list is returned unchanged —
    a wrong-duration candidate beats no candidate (same rule Phase 0 gave yt-dlp).
    """
    if want_duration_s is None:
        return candidates
    near = [
        c
        for c in candidates
        if of(c) is None or abs(int(of(c)) - want_duration_s) <= tolerance_s  # type: ignore[arg-type]
    ]
    return near or candidates


# tokens that carry no identity — ignored when comparing a title to the query
_FILLER = frozenset(
    {
        "from",
        "feat",
        "featuring",
        "ft",
        "official",
        "video",
        "audio",
        "song",
        "full",
        "lyrics",
        "title",
        "track",
        "the",
        "a",
        "an",
    }
)


def norm_tokens(text: str) -> list[str]:
    cleaned = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    return [w for w in cleaned.split() if len(w) > 1 and w not in _FILLER]


def _overlap(text: str, query_tokens: set[str]) -> float:
    """Fraction of ``text``'s significant tokens present in the query (0..1)."""
    tokens = norm_tokens(text)
    if not tokens:
        return 0.0
    return sum(1 for w in tokens if w in query_tokens) / len(tokens)


def title_matches(title: str, query: str, threshold: float = 0.6) -> bool:
    """True if most of the candidate *title*'s significant tokens appear in the query."""
    if not norm_tokens(title):
        return False
    return _overlap(title, set(norm_tokens(query))) >= threshold


def artist_matches(candidate_artist: str, want_artist: str, threshold: float = 0.5) -> bool:
    """True if the candidate's artist plausibly matches the requested artist.

    Guards against a same-title track by a *different* artist slipping through on
    sources that can't match by ISRC (monochrome/soulseek/telegram/ytdlp). The real
    incident: a request for Alfaaz — "Hai Mera Dil" landed Alka Yagnik — "Hai Mera
    Dil Tu", whose title matched and whose duration coincidentally lined up.

    Both names are tokenized via :func:`norm_tokens`; the check is the fraction of
    the **smaller** token set that appears in the other. Using the smaller set makes
    it symmetric and lenient about extra collaborators: a single requested artist
    fully inside a multi-artist candidate passes ("Alfaaz" inside "Alfaaz, Yo Yo
    Honey Singh"), and a collaborator of a multi-artist request passes too. It's
    deliberately lenient (default 0.5) so alias/label-credit differences aren't
    falsely rejected — its job is only to reject a *completely* unrelated artist
    ("Alfaaz" vs "Alka Yagnik" → 0 overlap → False).

    An empty ``want_artist`` OR empty candidate tokens returns True — never gate on
    missing data.
    """
    want_tokens = set(norm_tokens(want_artist))
    cand_tokens = set(norm_tokens(candidate_artist))
    if not want_tokens or not cand_tokens:
        return True
    smaller, larger = (
        (want_tokens, cand_tokens)
        if len(want_tokens) <= len(cand_tokens)
        else (cand_tokens, want_tokens)
    )
    overlap = sum(1 for t in smaller if t in larger) / len(smaller)
    return overlap >= threshold


def match_score(title: str, artist: str, query: str) -> float:
    """Relevance of a candidate to the query: title overlap + artist overlap.

    The title carries the song identity; the **artist** term is what separates
    two different songs that share a title (e.g. a requested "Mammoth" by one
    artist vs an unrelated "Mammoth" by another). Higher is better; adapters pick
    the highest-scoring title-matching candidate instead of the first one.
    """
    q = set(norm_tokens(query))
    return _overlap(title, q) + _overlap(artist, q)
