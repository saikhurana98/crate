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


def title_matches(title: str, query: str, threshold: float = 0.6) -> bool:
    """True if most of the candidate *title*'s significant tokens appear in the query."""
    title_tokens = norm_tokens(title)
    if not title_tokens:
        return False
    query_tokens = set(norm_tokens(query))
    hits = sum(1 for w in title_tokens if w in query_tokens)
    return hits / len(title_tokens) >= threshold
