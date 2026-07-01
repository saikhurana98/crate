"""MusicBrainz free-text ISRC resolver (fallback when there's no Spotify link)."""

from lbmd.adapters.metadata.musicbrainz import resolve


def _rec(score: int, isrcs: list[str] | None) -> dict:  # type: ignore[type-arg]
    r: dict = {  # type: ignore[type-arg]
        "score": score,
        "title": "Levitating",
        "length": 203000,
        "artist-credit": [{"name": "Dua Lipa"}],
    }
    if isrcs is not None:
        r["isrcs"] = isrcs
    return r


def test_resolve_returns_top_recording_isrc() -> None:
    def get_json(url, params, headers=None):  # type: ignore[no-untyped-def]
        assert "recording" in url
        assert params["inc"] == "isrcs" and headers["User-Agent"]
        return {"recordings": [_rec(100, ["GBAHT2600039"])]}

    meta = resolve("Dua Lipa Levitating", get_json=get_json)
    assert meta is not None
    assert meta.isrc == "GBAHT2600039"
    assert meta.title == "Levitating"
    assert meta.artist == "Dua Lipa"
    assert meta.duration_s == 203


def test_resolve_none_when_below_score_threshold() -> None:
    meta = resolve(
        "vague query",
        get_json=lambda *a, **k: {"recordings": [_rec(40, ["X"])]},
    )
    assert meta is None


def test_resolve_none_when_no_recordings() -> None:
    assert resolve("nothing", get_json=lambda *a, **k: {"recordings": []}) is None


def test_resolve_meta_without_isrc_still_returned() -> None:
    meta = resolve("q", get_json=lambda *a, **k: {"recordings": [_rec(95, None)]})
    assert meta is not None and meta.isrc is None
