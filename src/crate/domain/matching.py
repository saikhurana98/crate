"""Title-match guard shared by source adapters.

Fuzzy search / bot replies routinely surface a different song with a related
name. This rejects a candidate whose title's significant tokens are not mostly
present in the query, so a wrong-song result is skipped instead of accepted.
Pure, dependency-free.
"""

from __future__ import annotations

import re

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


def match_score(title: str, artist: str, query: str) -> float:
    """Relevance of a candidate to the query: title overlap + artist overlap.

    The title carries the song identity; the **artist** term is what separates
    two different songs that share a title (e.g. a requested "Mammoth" by one
    artist vs an unrelated "Mammoth" by another). Higher is better; adapters pick
    the highest-scoring title-matching candidate instead of the first one.
    """
    q = set(norm_tokens(query))
    return _overlap(title, q) + _overlap(artist, q)
