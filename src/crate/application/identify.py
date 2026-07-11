"""Advisory identity hint from an acoustic fingerprint (ADR-0019).

Given a file, fingerprint it and ask AcoustID what recording it is, returning an
:class:`IdentityHint` (canonical artist/title/MBID) — the review UI shows this to
flag wrong-version grabs (``… (acoustic)``/``(instrumental)``/different song). It
is *advisory*: AcoustID covers only part of a regional library and is sometimes
wrong, so nothing downstream may depend on it, and it deliberately carries **no
ISRC** (the spike measured 0% ISRC resolution — see ADR-0019).

``parse_acoustid`` is pure and tested here; the fingerprinter and the lookup call
are injected, so ``identify`` runs without a subprocess or the network.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from crate.domain.identity import IdentityHint

# path -> (duration_s, fingerprint_b64); the compressed form the lookup wants.
CompressedFingerprinter = Callable[[Path], tuple[int, str]]
# (duration_s, fingerprint_b64) -> AcoustID lookup JSON.
AcoustidLookup = Callable[[int, str], dict[str, Any]]


def parse_acoustid(data: dict[str, Any]) -> IdentityHint | None:
    """Top recording of an AcoustID lookup → :class:`IdentityHint`, or ``None``.

    Reads the first result's first recording for artist/title/MBID. Returns
    ``None`` when there is no result, no linked recording, or nothing nameable —
    all normal outcomes for this library (72% known, 55% MBID). ISRCs are ignored
    by design."""
    results = data.get("results") or []
    if not results:
        return None
    recordings = results[0].get("recordings") or []
    if not recordings:
        return None
    rec = recordings[0]
    artists = rec.get("artists") or []
    artist = artists[0].get("name") if artists else None
    title = rec.get("title")
    if not artist and not title:
        return None
    return IdentityHint(canonical_artist=artist, canonical_title=title, mbid=rec.get("id"))


def identify(
    path: Path,
    *,
    fingerprint_compressed: CompressedFingerprinter,
    lookup: AcoustidLookup,
) -> IdentityHint | None:
    """Fingerprint ``path`` and resolve its advisory :class:`IdentityHint`.

    Returns ``None`` when AcoustID has no usable answer. Raises only if the
    injected fingerprinter or lookup fails (the API endpoint turns that into an
    empty hint), so a fingerprint/network hiccup never wedges the caller."""
    duration, fp = fingerprint_compressed(path)
    return parse_acoustid(lookup(duration, fp))
