from pathlib import Path
from typing import Any

from crate.adapters.sources.saavn import download
from crate.domain.identity import TrackIdentity


def test_saavn_matches_artist_and_preserves_album_metadata(tmp_path: Path) -> None:
    row: dict[str, Any] = {
        "id": "VQp1eXug",
        "name": "Zara Sa",
        "artists": {"primary": [{"name": "Pritam"}, {"name": "KK"}]},
        "album": {"name": "Jannat"},
        "image": [{"url": "https://c.saavncdn.com/cover.jpg"}],
        "duration": 302,
    }
    wrong = {**row, "id": "wrong", "artists": {"primary": [{"name": "Other Artist"}]}}

    def fetch(ref: str, dest: Path) -> None:
        assert ref == row["id"]
        dest.write_bytes(b"audio")

    result = download(
        "Pritam Zara Sa",
        tmp_path,
        TrackIdentity("Pritam", "Zara Sa"),
        search=lambda _: [wrong, row],
        fetch=fetch,
    )
    assert result.path.read_bytes() == b"audio"
    assert list(tmp_path.iterdir()) == [result.path]
    assert result.artist == "Pritam, KK"
    assert result.album == "Jannat"
    assert result.art_url == "https://c.saavncdn.com/cover.jpg"
