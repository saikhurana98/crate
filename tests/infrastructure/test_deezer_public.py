"""DeezerPublic: keyless track lookups over an injected get_json."""

from __future__ import annotations

from typing import Any

from crate.infrastructure.deezer.public import DeezerPublic


def _client(reply: Any) -> tuple[DeezerPublic, list[str]]:
    seen: list[str] = []

    def get(url: str) -> Any:
        seen.append(url)
        return reply

    return DeezerPublic(get), seen


def test_track_by_isrc_normalises_and_hits_the_isrc_endpoint() -> None:
    client, seen = _client({"id": 3135556, "preview": "https://cdnt-preview.dzcdn.net/x.mp3"})
    assert client.track_by_isrc(" gbaye0601498 ")["id"] == 3135556  # type: ignore[index]
    assert seen == ["https://api.deezer.com/track/isrc:GBAYE0601498"]


def test_track_by_id() -> None:
    client, seen = _client({"id": 42})
    assert client.track("42") == {"id": 42}
    assert seen == ["https://api.deezer.com/track/42"]


def test_deezer_error_payloads_and_junk_are_none() -> None:
    client, _ = _client({"error": {"type": "DataException", "message": "no data", "code": 800}})
    assert client.track_by_isrc("GBAYE0601498") is None
    assert _client(None)[0].track("1") is None
    assert _client({"title": "no id"})[0].track("1") is None


def test_malformed_values_never_reach_the_network() -> None:
    client, seen = _client({"id": 1})
    assert client.track_by_isrc("../../x") is None
    assert client.track("12/../34") is None
    assert client.track_by_isrc("") is None
    assert seen == []
