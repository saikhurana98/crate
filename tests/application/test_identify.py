"""Advisory AcoustID hint: pure parse + injected-IO wiring."""

from pathlib import Path

from crate.application.identify import identify, parse_acoustid
from crate.domain.identity import IdentityHint

FULL = {
    "results": [
        {
            "id": "acoustid-1",
            "score": 0.98,
            "recordings": [
                {
                    "id": "mbid-123",
                    "title": "Lonely Together (acoustic)",
                    "artists": [{"name": "Avicii"}],
                }
            ],
        }
    ]
}


def test_parse_picks_top_recording() -> None:
    hint = parse_acoustid(FULL)
    assert hint == IdentityHint(
        canonical_artist="Avicii", canonical_title="Lonely Together (acoustic)", mbid="mbid-123"
    )


def test_parse_no_results_is_none() -> None:
    assert parse_acoustid({"results": []}) is None
    assert parse_acoustid({}) is None


def test_parse_result_without_recordings_is_none() -> None:
    # A fingerprint known to AcoustID but not linked to a MusicBrainz recording.
    assert parse_acoustid({"results": [{"id": "a", "score": 0.9}]}) is None


def test_parse_title_only_recording() -> None:
    data = {"results": [{"recordings": [{"id": "m", "title": "Untitled"}]}]}
    assert parse_acoustid(data) == IdentityHint(
        canonical_artist=None, canonical_title="Untitled", mbid="m"
    )


def test_parse_unnameable_recording_is_none() -> None:
    assert parse_acoustid({"results": [{"recordings": [{"id": "m"}]}]}) is None


def test_identify_wires_fingerprint_and_lookup() -> None:
    seen: dict[str, object] = {}

    def fp(path: Path) -> tuple[int, str]:
        seen["path"] = path
        return 201, "AQAAB64"

    def lookup(duration: int, fingerprint: str) -> dict[str, object]:
        seen["lookup"] = (duration, fingerprint)
        return FULL

    hint = identify(Path("/x/song.m4a"), fingerprint_compressed=fp, lookup=lookup)
    assert hint is not None and hint.canonical_artist == "Avicii"
    assert seen["path"] == Path("/x/song.m4a")
    assert seen["lookup"] == (201, "AQAAB64")


def test_identify_returns_none_on_miss() -> None:
    hint = identify(
        Path("/x/song.m4a"),
        fingerprint_compressed=lambda p: (100, "fp"),
        lookup=lambda d, f: {"results": []},
    )
    assert hint is None
