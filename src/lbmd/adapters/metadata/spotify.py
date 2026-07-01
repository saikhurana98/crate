"""Spotify metadata resolver — anonymous ISRC lookup (SpotiFLAC method).

Spotify gates its web token behind a client-side TOTP (a hardcoded secret + a
30s code). We replicate it to mint an anonymous web-player token, then read a
track's ISRC from the internal ``spclient`` metadata endpoint — the public
``api.spotify.com/v1`` endpoints hard-rate-limit anonymous tokens, whereas
spclient does not (this is exactly why SpotiFLAC uses it). The track's base62
id is converted to the 128-bit hex GID that spclient expects.

Only track links/URIs resolve (free text falls back to MusicBrainz). HTTP +
clock are injected; the TOTP / base62 / parse helpers are pure.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import struct
import time
from collections.abc import Callable
from typing import Any

from lbmd.domain.meta import TrackMeta

# Spotify's client TOTP secret + version (as used by web-player token minting).
_TOTP_SECRET = "GM3TMMJTGYZTQNZVGM4DINJZHA4TGOBYGMZTCMRTGEYDSMJRHE4TEOBUG4YTCMRUGQ4DQOJUGQYTAMRRGA2TCMJSHE3TCMBY"  # noqa: E501
_TOTP_VERSION = 61
_TOTP_DIGITS = 6
_TOKEN_URL = "https://open.spotify.com/api/token"
_METADATA_URL = "https://spclient.wg.spotify.com/metadata/4/track/{gid}"
_B62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

GetJson = Callable[..., Any]
Clock = Callable[[], float]

_TRACK_ID_RE = re.compile(r"(?:open\.spotify\.com/track/|spotify:track:)([A-Za-z0-9]+)")


def totp(secret_b32: str, unix_seconds: int, period: int = 30, digits: int = _TOTP_DIGITS) -> str:
    """Standard RFC 6238 TOTP (SHA1) over a base32 secret."""
    key = base64.b32decode(secret_b32)
    counter = int(unix_seconds // period)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10**digits)).zfill(digits)


def generate_totp(unix_seconds: int) -> str:
    return totp(_TOTP_SECRET, unix_seconds)


def token_params(code: str) -> dict[str, str]:
    return {
        "reason": "init",
        "productType": "web-player",
        "totp": code,
        "totpServer": code,
        "totpVer": str(_TOTP_VERSION),
    }


def base62_to_gid(track_id: str) -> str:
    """Convert a Spotify base62 track id to the 128-bit hex GID spclient wants."""
    n = 0
    for ch in track_id:
        n = n * 62 + _B62.index(ch)
    return format(n, "032x")


def extract_track_id(query: str) -> str | None:
    """Return the Spotify track id from a URL/URI, or None for free text."""
    match = _TRACK_ID_RE.search(query)
    return match.group(1) if match else None


def parse_track(track: dict[str, Any]) -> TrackMeta:
    """Parse a spclient ``metadata/4/track`` response."""
    externals = track.get("external_id", [])
    isrc = next((e.get("id") for e in externals if e.get("type") == "isrc"), None)
    artists = track.get("artist", [])
    artist = artists[0].get("name", "Unknown") if artists else "Unknown"
    ms = track.get("duration")
    return TrackMeta(
        isrc=isrc,
        title=str(track.get("name", "Unknown")),
        artist=str(artist),
        album=str(track.get("album", {}).get("name", "")),
        duration_s=round(ms / 1000) if ms else None,
    )


def resolve(query: str, *, get_json: GetJson, clock: Clock = time.time) -> TrackMeta | None:
    """Resolve ISRC + metadata for a Spotify track link/URI. None for free text."""
    track_id = extract_track_id(query)
    if track_id is None:
        return None
    code = generate_totp(int(clock()))
    token = get_json(_TOKEN_URL, token_params(code), None)
    access = token.get("accessToken")
    if not access:
        return None
    gid = base62_to_gid(track_id)
    track = get_json(
        _METADATA_URL.format(gid=gid),
        {"market": "from_token"},
        {"Authorization": f"Bearer {access}", "Accept": "application/json"},
    )
    return parse_track(track)
