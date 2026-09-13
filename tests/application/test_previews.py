"""resolve_preview: Spotify id -> ISRC -> Deezer id -> fresh CDN preview URL."""

from __future__ import annotations

from typing import Any

import pytest

from crate.application.previews import resolve_preview, safe_preview

SID = "4uLU6hMCjMI75M1A2tKUQC"
CDN = "https://cdnt-preview.dzcdn.net/api/1/1/a/b/c/0/abc.mp3?hdnea=exp=1"


class FakeCache:
    def __init__(self, rows: dict[str, dict[str, Any]] | None = None) -> None:
        self.rows = rows or {}

    def get(self, spotify_id: str) -> dict[str, Any] | None:
        return dict(self.rows[spotify_id]) if spotify_id in self.rows else None

    def put(self, spotify_id: str, meta: dict[str, Any]) -> None:
        self.rows[spotify_id] = {**self.rows.get(spotify_id, {}), **meta}

    def set_deezer_id(self, spotify_id: str, deezer_id: str | None) -> None:
        self.rows.setdefault(spotify_id, {})["deezer_id"] = deezer_id


class FakeDeezer:
    def __init__(
        self, by_isrc: dict[str, dict[str, Any]], by_id: dict[str, dict[str, Any]]
    ) -> None:
        self.by_isrc, self.by_id = by_isrc, by_id
        self.calls: list[str] = []

    def track_by_isrc(self, isrc: str) -> dict[str, Any] | None:
        self.calls.append(f"isrc:{isrc}")
        return self.by_isrc.get(isrc)

    def track(self, deezer_id: str) -> dict[str, Any] | None:
        self.calls.append(f"id:{deezer_id}")
        return self.by_id.get(deezer_id)


def _spotify(isrc: str | None) -> Any:
    calls: list[list[str]] = []

    def tracks(ids: list[str]) -> list[dict[str, Any]]:
        calls.append(ids)
        return [{"id": SID, "isrc": isrc, "title": "T", "artist": "A"}]

    tracks.calls = calls  # type: ignore[attr-defined]
    return tracks


def test_resolves_via_isrc_and_caches_meta_and_deezer_id() -> None:
    cache, spotify = FakeCache(), _spotify("USUM71703861")
    deezer = FakeDeezer({"USUM71703861": {"id": 42, "preview": CDN}}, {})
    assert resolve_preview(SID, cache=cache, spotify_tracks=spotify, deezer=deezer) == CDN
    assert cache.rows[SID]["isrc"] == "USUM71703861" and cache.rows[SID]["deezer_id"] == "42"


def test_a_cached_deezer_id_skips_spotify_and_isrc_lookups() -> None:
    cache = FakeCache({SID: {"isrc": "X", "deezer_id": "42"}})
    spotify = _spotify("X")
    deezer = FakeDeezer({}, {"42": {"id": 42, "preview": CDN}})
    assert resolve_preview(SID, cache=cache, spotify_tracks=spotify, deezer=deezer) == CDN
    assert deezer.calls == ["id:42"] and spotify.calls == []


def test_a_stale_deezer_id_falls_back_to_the_isrc() -> None:
    cache = FakeCache({SID: {"isrc": "GBAYE0601498", "deezer_id": "7"}})
    deezer = FakeDeezer({"GBAYE0601498": {"id": 8, "preview": CDN}}, {})
    assert resolve_preview(SID, cache=cache, spotify_tracks=_spotify(None), deezer=deezer) == CDN
    assert deezer.calls == ["id:7", "isrc:GBAYE0601498"] and cache.rows[SID]["deezer_id"] == "8"


def test_no_preview_when_the_track_or_isrc_is_unknown() -> None:
    deezer = FakeDeezer({}, {})
    assert (
        resolve_preview(SID, cache=FakeCache(), spotify_tracks=lambda ids: [], deezer=deezer)
        is None
    )
    assert (
        resolve_preview(SID, cache=FakeCache(), spotify_tracks=_spotify(None), deezer=deezer)
        is None
    )
    cache = FakeCache()
    assert (
        resolve_preview(SID, cache=cache, spotify_tracks=_spotify("NOPE00000001"), deezer=deezer)
        is None
    )
    assert cache.rows[SID]["deezer_id"] is None  # the miss is remembered as "no id"


def test_only_deezer_cdn_https_urls_come_back() -> None:
    for bad in ("http://cdnt-preview.dzcdn.net/x.mp3", "https://evil.example/x.mp3", None, 5):
        deezer = FakeDeezer({"USUM71703861": {"id": 1, "preview": bad}}, {})
        got = resolve_preview(
            SID, cache=FakeCache(), spotify_tracks=_spotify("USUM71703861"), deezer=deezer
        )
        assert got is None
    assert safe_preview(CDN) == CDN
    assert safe_preview("https://dzcdn.net.evil.example/x") is None


@pytest.mark.parametrize("bad", ["", "short", "4uLU6hMCjMI75M1A2tKUQ/", "../../etc/passwd000000"])
def test_rejects_anything_but_a_spotify_id(bad: str) -> None:
    with pytest.raises(ValueError):
        resolve_preview(
            bad, cache=FakeCache(), spotify_tracks=_spotify(None), deezer=FakeDeezer({}, {})
        )
