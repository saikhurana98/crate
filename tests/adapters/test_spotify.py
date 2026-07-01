"""Spotify metadata resolver (anonymous TOTP token -> track ISRC via spclient).

Mirrors SpotiFLAC: a client-side TOTP mints an anonymous token, the base62 id is
converted to a GID, and spclient's metadata endpoint returns the ISRC. Pure
helpers are tested directly; resolve() uses an injected HTTP fake (no network).
"""

import base64

from lbmd.adapters.metadata.spotify import (
    base62_to_gid,
    extract_track_id,
    generate_totp,
    parse_track,
    resolve,
    totp,
)


def test_totp_matches_rfc6238_vector() -> None:
    # RFC 6238 SHA1 test vector: secret "12345678901234567890", T=59 -> 287082 (6-digit).
    secret_b32 = base64.b32encode(b"12345678901234567890").decode()
    assert totp(secret_b32, 59) == "287082"


def test_generate_totp_is_six_digits() -> None:
    code = generate_totp(1_700_000_000)
    assert code.isdigit() and len(code) == 6


def test_base62_to_gid_known_vector() -> None:
    # verified live: Never Gonna Give You Up
    assert base62_to_gid("4uLU6hMCjMI75M1A2tKUQC") == "93bc414a606747b2b612491ef83d5a3e"


def test_extract_track_id_from_url_uri_and_free_text() -> None:
    tid = "4iV5W9uYEdYUVa79Axb7Rh"
    assert extract_track_id(f"https://open.spotify.com/track/{tid}") == tid
    assert extract_track_id(f"spotify:track:{tid}") == tid
    assert extract_track_id(f"https://open.spotify.com/track/{tid}?si=xyz") == tid
    assert extract_track_id("Dua Lipa Levitating") is None


def test_parse_track_pulls_isrc_and_metadata() -> None:
    track = {
        "name": "Never Gonna Give You Up",
        "artist": [{"name": "Rick Astley"}],
        "album": {"name": "Whenever You Need Somebody"},
        "duration": 213573,
        "external_id": [{"type": "isrc", "id": "GBARL9300135"}],
    }
    meta = parse_track(track)
    assert meta.isrc == "GBARL9300135"
    assert meta.title == "Never Gonna Give You Up"
    assert meta.artist == "Rick Astley"
    assert meta.album == "Whenever You Need Somebody"
    assert meta.duration_s == 214


def test_resolve_returns_none_for_free_text() -> None:
    assert resolve("Dua Lipa Levitating", get_json=lambda *a, **k: {}, clock=lambda: 0.0) is None


def test_resolve_by_url_mints_token_then_fetches_spclient_metadata() -> None:
    calls: list[str] = []

    def get_json(url: str, params: dict, headers: dict | None = None) -> object:  # type: ignore[type-arg]
        calls.append(url)
        if "api/token" in url:
            assert params["totp"] and params["totpVer"] == "61"
            return {"accessToken": "tok-abc"}
        assert headers is not None and headers["Authorization"] == "Bearer tok-abc"
        assert params["market"] == "from_token"
        return {
            "name": "Dealer",
            "artist": [{"name": "Diljit Dosanjh"}],
            "album": {"name": "Dealer"},
            "duration": 110000,
            "external_id": [{"type": "isrc", "id": "QT65Z2673203"}],
        }

    meta = resolve(
        "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
        get_json=get_json,
        clock=lambda: 1_700_000_000.0,
    )
    assert meta is not None
    assert meta.isrc == "QT65Z2673203"
    assert meta.title == "Dealer"
    assert any("api/token" in c for c in calls)
    assert any("/metadata/4/track/93bc414a606747b2b612491ef83d5a3e" in c for c in calls)


def test_resolve_none_when_token_mint_fails() -> None:
    meta = resolve("spotify:track:abc", get_json=lambda *a, **k: {}, clock=lambda: 0.0)
    assert meta is None
