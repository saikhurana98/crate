"""Title-match guard shared by source adapters.

Fuzzy search / bot replies routinely surface a different song with a related
name. This rejects a candidate whose title's significant tokens are not mostly
present in the query, so a wrong-song result is skipped instead of accepted.
Pure, dependency-free.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from crate.domain.identity import TrackIdentity

# Drop a candidate whose runtime is off the wanted duration by more than this
# (shared tolerance for the source-side prefilter and the post-download check).
DURATION_TOLERANCE_S = 7

# How far (seconds) a candidate's runtime may drift from the wanted one before the
# duration term of candidate_match() reaches zero. Beyond ~half a minute the two
# are almost certainly different recordings (edit/extended/live), so the term
# stops distinguishing them.
_MATCH_DURATION_SPAN_S = 30.0

# candidate_match() term weights (must sum to 1.0). Title carries the song
# identity, the artist separates same-title songs, duration is the tiebreak.
_W_TITLE = 0.55
_W_ARTIST = 0.30
_W_DURATION = 0.15

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


# Words marking an *alternate recording* rather than the requested one. Unlike
# _FILLER these are meaningful — they say "this is a different version" — so they
# are not stripped, only counted when the query didn't ask for them.
_VERSION_MARKERS = frozenset(
    {
        "edit",
        "edits",
        "remix",
        "remixes",
        "rmx",
        "bootleg",
        "mashup",
        "rework",
        "refix",
        "flip",
        "vip",
        "live",
        "cover",
        "instrumental",
        "acapella",
        "karaoke",
        "nightcore",
        "slowed",
        "sped",
        "reverb",
        "demo",
        "snippet",
    }
)


def norm_tokens(text: str) -> list[str]:
    cleaned = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    return [w for w in cleaned.split() if len(w) > 1 and w not in _FILLER]


def unwanted_version_markers(title: str, query: str) -> int:
    """How many "different version" words the title carries that the query didn't ask for.

    A title can contain every token of the query and still be the wrong
    recording: a DJ bootleg named ``"SHALT x Sean Paul - Temperature -'Acheron'
    ULTIMATE DESTRUCTION Edit-"`` covers the query completely. Token *coverage*
    can't see that — it's deliberately blind to extra words so release decoration
    doesn't penalise a good file — so this counts the markers instead.

    Zero when the query asks for that version (searching "temperature remix"
    should return remixes), so it only ever demotes an unrequested alternate.
    Ranking signal, not a gate: if a bootleg is all the network has, it still
    beats nothing. The duration prefilter is the hard guard when a runtime is
    known; this covers identity-less free-text requests, which have none."""
    wanted = set(norm_tokens(query))
    return sum(1 for t in set(norm_tokens(title)) if t in _VERSION_MARKERS and t not in wanted)


def _overlap(text: str, query_tokens: set[str]) -> float:
    """Fraction of ``text``'s significant tokens present in the query (0..1)."""
    tokens = norm_tokens(text)
    if not tokens:
        return 0.0
    return sum(1 for w in tokens if w in query_tokens) / len(tokens)


def title_matches(title: str, query: str, threshold: float = 0.6) -> bool:
    """True if the candidate *title* and the query mostly share significant tokens.

    Bidirectional, but the two directions are not symmetric:

    * **Title ⊆ query** (fractional, ``threshold``): most of the title's tokens
      appear in the query — a clean ``"Get Lucky"`` under a longer query.
    * **Query ⊆ title** (*complete*, not fractional): every query token appears
      in the title. This is what lets a verbose source title of the right song
      through — ``"Daft Punk - Get Lucky (Official Audio) ft. Pharrell Williams,
      Nile Rodgers"`` — where featured artists inflate the title.

    The second direction demands *full* coverage because a query usually carries
    the artist as well as the title, and an artist name alone can supply most of
    the query's tokens. Accepting a fraction let a different song by the right
    artist through: ``"Sean Paul - Get Busy"`` scored 2/3 against the query
    ``"Sean Paul Temperature"`` and passed. Requiring every token means the
    actual title words must be present, which is the whole point of this gate."""
    title_tokens = norm_tokens(title)
    query_tokens = norm_tokens(query)
    if not title_tokens or not query_tokens:
        return False
    query_set, title_set = set(query_tokens), set(title_tokens)
    title_in_query = sum(1 for w in title_tokens if w in query_set) / len(title_tokens)
    return title_in_query >= threshold or query_set <= title_set


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


def _jaccard(a: str, b: str) -> float:
    """Token-set similarity of two strings (0..1), filler-stripped and symmetric.

    Jaccard index (intersection size over union size) over :func:`norm_tokens`.
    Empty on either side is 0."""
    ta, tb = set(norm_tokens(a)), set(norm_tokens(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def candidate_match(
    cand_title: str,
    cand_artist: str,
    cand_duration_s: int | None,
    want: TrackIdentity,
) -> int:
    """Score a review candidate against the wanted track: 0..100 (higher = closer).

    A weighted blend of three symmetric similarities — title-token overlap,
    artist overlap, and duration proximity — exactly the design's "match % =
    title · artist · duration similarity". Terms whose inputs are unknown (no
    wanted artist, no duration on either side) are dropped and the remaining
    weights renormalised, so a candidate is never penalised for data the request
    simply doesn't carry. Pure and dependency-free.
    """
    want_title = want.title or want.display()
    terms: list[tuple[float, float]] = [(_W_TITLE, _jaccard(cand_title, want_title))]
    if want.artist:
        terms.append((_W_ARTIST, _jaccard(cand_artist, want.artist)))
    if want.duration_s is not None and cand_duration_s is not None:
        delta = abs(cand_duration_s - want.duration_s)
        terms.append((_W_DURATION, max(0.0, 1.0 - delta / _MATCH_DURATION_SPAN_S)))
    total_weight = sum(w for w, _ in terms)
    if total_weight == 0:
        return 0
    score = sum(w * s for w, s in terms) / total_weight
    return max(0, min(100, round(score * 100)))
