"""Tier-2 arcod adapter — Qobuz proxy, async job, no auth.

The HTTP flow (search / create job / poll / fetch) is injected; the pure pieces —
search parsing, the POST payload, track selection, and the poll-loop orchestration
— are tested directly with fakes (no network). The search fixture is a real TRACK
object captured from ``music-sources/arcod/arcod.xyz.har``.
"""

from pathlib import Path
from typing import Any

import pytest

from crate.adapters.sources.arcod import (
    ArcodTrack,
    arcod_download,
    build_download_payload,
    parse_search,
    select_track,
)
from crate.domain.identity import TrackIdentity

# A real TRACK from the HAR (query "diljit dosanjh panga"), trimmed to the fields
# the adapter reads. arcod delivered this one as .mp3 despite the FLAC request.
_TRACK = {
    "id": 302821383,
    "title": 'Panga (From "The Next Level")',
    "version": None,
    "isrc": "INS180902477",
    "duration": 282,
    "maximum_bit_depth": 16,
    "hires": False,
    "track_number": 4,
    "media_number": 1,
    "performer": {"name": "Diljit Dosanjh", "id": 1318447},
    "album": {
        "id": "x5zb76hnr897a",
        "title": "Dil Se Diljit",
        "release_date_original": "2024-11-08",
        "image": {"large": "https://static.qobuz.com/images/covers/7a/89/x5zb76hnr897a_600.jpg"},
    },
}
SEARCH_RESPONSE = {"success": True, "data": {"tracks": {"items": [_TRACK]}}}


def test_parse_search_maps_track_fields() -> None:
    tracks = parse_search(SEARCH_RESPONSE)
    assert len(tracks) == 1
    t = tracks[0]
    assert t.id == 302821383
    assert t.title == 'Panga (From "The Next Level")'
    assert t.artist == "Diljit Dosanjh"
    assert t.artist_id == "1318447"
    assert t.album_id == "x5zb76hnr897a"
    assert t.album_title == "Dil Se Diljit"
    assert t.cover_url.endswith("x5zb76hnr897a_600.jpg")
    assert t.release_date == "2024-11-08"
    assert t.isrc == "INS180902477"
    assert t.duration_s == 282
    assert t.track_no == 4
    assert t.hires is False


def test_parse_search_raises_on_failure() -> None:
    with pytest.raises(LookupError, match="arcod search failed"):
        parse_search({"success": False, "error": "rate limited"})


def test_build_download_payload_field_mapping() -> None:
    track = parse_search(SEARCH_RESPONSE)[0]
    payload = build_download_payload(track)
    assert payload["albumId"] == "x5zb76hnr897a"
    assert payload["trackId"] == "302821383"  # stringified track id
    assert payload["albumTitle"] == "Dil Se Diljit"
    assert payload["artistName"] == "Diljit Dosanjh"
    assert payload["artistId"] == "1318447"  # stringified performer id
    assert payload["coverUrl"].endswith("x5zb76hnr897a_600.jpg")
    assert payload["releaseDate"] == "2024-11-08"
    assert payload["tracksCount"] == 1
    assert payload["quality"] == 27
    assert payload["format"] == "FLAC"
    assert payload["bitrate"] == 320
    assert payload["embedLyrics"] is True
    assert payload["lyricsMode"] == "embed"
    assert payload["downloadBooklet"] is False
    assert payload["attachCover"] is False
    assert payload["zipName"] == "{artists} - {name}"
    assert payload["trackName"] == "{track} - {name}"


def _at(
    id_: int,
    title: str,
    artist: str,
    duration_s: int | None = None,
    isrc: str | None = None,
) -> ArcodTrack:
    return ArcodTrack(
        id=id_,
        title=title,
        artist=artist,
        artist_id="0",
        album_id="al",
        album_title="Album",
        cover_url="",
        release_date="",
        isrc=isrc,
        duration_s=duration_s,
        track_no=None,
        hires=False,
    )


def test_select_track_title_guard() -> None:
    tracks = parse_search(SEARCH_RESPONSE)
    assert select_track(tracks, "Diljit Dosanjh Panga The Next Level").id == 302821383
    with pytest.raises(LookupError, match="no title-matching"):
        select_track(tracks, "Completely Different Song")


def test_select_track_isrc_short_circuits() -> None:
    tracks = [
        _at(1, "Levitating", "Cover Artist", 200, "GBWRONG00001"),  # wrong, first
        _at(2, "Levitating", "Dua Lipa", 203, "GBAHT2000123"),  # wanted
    ]
    want = TrackIdentity(title="anything", isrc="GBAHT2000123")
    assert select_track(tracks, "anything", want).id == 2


def test_select_track_prefilters_by_duration() -> None:
    tracks = [
        _at(1, "Levitating", "Dua Lipa", 480),  # extended, off
        _at(2, "Levitating", "Dua Lipa", 203),  # wanted
    ]
    want = TrackIdentity(artist="Dua Lipa", title="Levitating", duration_s=203)
    assert select_track(tracks, "Dua Lipa Levitating", want).id == 2


def test_select_track_artist_gate_drops_wrong_artist_same_title() -> None:
    # A same-title track by a different artist with a coincidentally-matching
    # duration is gated out -> LookupError so the cascade advances (Alfaaz/Alka).
    tracks = [_at(1, "Hai Mera Dil Tu", "Alka Yagnik", 209)]
    want = TrackIdentity(artist="Alfaaz", title="Hai Mera Dil", duration_s=210)
    with pytest.raises(LookupError, match="no title-matching"):
        select_track(tracks, "Alfaaz Hai Mera Dil", want)


def test_select_track_artist_gate_keeps_correct_artist() -> None:
    tracks = [
        _at(1, "Hai Mera Dil Tu", "Alka Yagnik", 209),
        _at(2, "Hai Mera Dil", "Alfaaz", 210),
    ]
    want = TrackIdentity(artist="Alfaaz", title="Hai Mera Dil", duration_s=210)
    assert select_track(tracks, "Alfaaz Hai Mera Dil", want).id == 2


class _FakePoll:
    """Returns ``pending`` first, then the given completed/failed status."""

    def __init__(self, *statuses: dict[str, Any]) -> None:
        self._statuses = list(statuses)
        self.seen: list[str] = []
        self.tokens: list[str] = []

    def __call__(self, job_id: str, token: str) -> dict[str, Any]:
        self.seen.append(job_id)
        self.tokens.append(token)
        return self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]


def test_arcod_download_happy_path(tmp_path: Path) -> None:
    payloads: dict[str, Any] = {}
    served = b"ID3fake-mp3-bytes"

    def search(q: str) -> Any:
        return SEARCH_RESPONSE

    def create_download(payload: dict[str, Any]) -> dict[str, Any]:
        payloads["sent"] = payload
        return {"id": "job-uuid-1", "accessToken": "tok-1", "status": "pending"}

    poll = _FakePoll(
        {"status": "pending"},
        {
            "status": "completed",
            "downloadUrl": "https://dl.arcod.xyz/downloads/job-uuid-1/track.mp3",
            "fileName": "04 - Panga.mp3",
            "fileSize": 12217334,
        },
    )

    def fetch(url: str, dest: Path) -> None:
        assert url.endswith("track.mp3")
        assert dest.name.endswith(".part")  # streams to a sidecar
        dest.write_bytes(served)

    outcome = arcod_download(
        "Diljit Dosanjh Panga",
        tmp_path,
        want=TrackIdentity(artist="Diljit Dosanjh", title="Panga", isrc="INS180902477"),
        search=search,
        create_download=create_download,
        poll=poll,
        fetch=fetch,
        clock=iter([1.0, 1.0, 3.0, 6.0]).__next__,  # start, deadline-base, in-loop, elapsed
        sleep=lambda _s: None,
    )
    assert outcome.tier == 2
    assert outcome.source == "arcod"
    assert outcome.elapsed_s == pytest.approx(5.0)
    assert outcome.path.suffix == ".mp3"  # extension from server fileName (downgraded)
    assert outcome.path.read_bytes() == served
    assert outcome.title == 'Panga (From "The Next Level")'
    assert outcome.artist == "Diljit Dosanjh"
    assert outcome.album == "Dil Se Diljit"
    assert outcome.track_no == 4
    assert payloads["sent"]["trackId"] == "302821383"
    assert poll.seen == ["job-uuid-1", "job-uuid-1"]  # polled twice (pending -> completed)
    assert poll.tokens == ["tok-1", "tok-1"]  # accessToken threaded into every poll
    assert not list(tmp_path.glob("*.part"))  # sidecar cleaned up


def test_arcod_download_raises_on_failed_status(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="failed: source unavailable"):
        arcod_download(
            "Diljit Dosanjh Panga Next Level",
            tmp_path,
            search=lambda q: SEARCH_RESPONSE,
            create_download=lambda p: {"id": "job-x", "accessToken": "t", "status": "pending"},
            poll=lambda j, t: {"status": "failed", "error": "source unavailable"},
            fetch=lambda u, d: None,
            clock=lambda: 0.0,
            sleep=lambda _s: None,
        )


def test_arcod_download_raises_on_timeout(tmp_path: Path) -> None:
    # The fake clock jumps past the deadline while the job stays pending -> timeout.
    # start=0.0, deadline=0.0+POLL_TIMEOUT_S, then the in-loop clock reads far ahead.
    clock = iter([0.0, 0.0, 1e9]).__next__  # start, deadline base, in-loop (past deadline)
    with pytest.raises(RuntimeError, match="timed out"):
        arcod_download(
            "Diljit Dosanjh Panga Next Level",
            tmp_path,
            search=lambda q: SEARCH_RESPONSE,
            create_download=lambda p: {"id": "job-slow", "accessToken": "t", "status": "pending"},
            poll=lambda j, t: {"status": "pending"},
            fetch=lambda u, d: None,
            clock=clock,
            sleep=lambda _s: None,
        )


def test_arcod_download_raises_when_no_match(tmp_path: Path) -> None:
    with pytest.raises(LookupError):
        arcod_download(
            "No Such Song Here",
            tmp_path,
            search=lambda q: SEARCH_RESPONSE,
            create_download=lambda p: {"id": "x", "accessToken": "t"},
            poll=lambda j, t: {"status": "completed"},
            fetch=lambda u, d: None,
            clock=lambda: 0.0,
            sleep=lambda _s: None,
        )


def test_arcod_track_dataclass_immutable() -> None:
    t = _at(1, "T", "A")
    with pytest.raises(AttributeError):
        t.title = "x"  # type: ignore[misc]
