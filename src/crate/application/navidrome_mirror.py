"""Mirror imported Spotify playlists into each user's own Navidrome account.

A user links their Navidrome login once on the Sync page (stored encrypted), and
importing a playlist marks it for mirroring. The ``navidrome_push`` job (serve;
on import, and every 30 min) then handles each mirrored playlist:
1. takes its tracks in Spotify order;
2. keeps the ones already in the library (the rest can't be in Navidrome yet);
3. finds each track's Navidrome song: from the cache, else one ``search3``;
4. creates the playlist in the user's account, or replaces its songs, in order.

Tracks still downloading join on a later run. The playlist belongs to the user's
Navidrome account, so it shows up in their players (Feishin, Symfonium, …)."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from crate.application.jobs import JobContext
from crate.application.library_sync import LibraryIndex
from crate.domain.listening import merge_key
from crate.domain.navidrome import NavidromeLogin, pick_song, search_query
from crate.infrastructure.navidrome.client import ERR_NOT_FOUND, NavidromeError

SONG_CACHE_DAYS = 3  # re-check cached song ids after this (files move, get trashed)
MAX_SEARCHES_PER_RUN = 2000  # search3 calls per job; the rest wait for the next run


class NavidromeApi(Protocol):
    def search_songs(self, query: str, count: int = 20) -> list[dict[str, Any]]: ...
    def save_playlist(
        self, name: str, song_ids: Iterable[str], playlist_id: str | None = None
    ) -> str: ...


class MirrorStore(Protocol):
    def account_users(self) -> list[str]: ...
    def credentials(self, user_id: str) -> NavidromeLogin | None: ...
    def links(self, user_id: str, *, enabled_only: bool = False) -> list[dict[str, Any]]: ...
    def record_push(
        self,
        user_id: str,
        spotify_id: str,
        *,
        navidrome_id: str | None = None,
        matched: int = 0,
        in_library: int = 0,
        total: int = 0,
        error: str | None = None,
    ) -> None: ...
    def song_ids(self, keys: Iterable[str], *, fresh_after: str) -> dict[str, str]: ...
    def put_song(self, key: str, song_id: str) -> None: ...


ClientFactory = Callable[[NavidromeLogin], NavidromeApi]
TracksFn = Callable[[str, str], list[dict[str, Any]]]  # (user_id, spotify_id) -> tracks


@dataclass
class PushResult:
    navidrome_id: str
    matched: int
    in_library: int
    total: int
    searched: int


@dataclass
class _Budget:
    searches: int


def _key(track: Mapping[str, Any]) -> str:
    return merge_key(track.get("artist"), track.get("title"))


def push_playlist(
    api: NavidromeApi,
    store: MirrorStore,
    link: Mapping[str, Any],
    tracks: list[dict[str, Any]],
    index: LibraryIndex,
    *,
    now: datetime,
    budget: _Budget,
) -> PushResult:
    """Bring one mirrored playlist up to date in Navidrome."""
    have = [(_key(t), t) for t in tracks if index.has_track(t)]
    fresh_after = (now - timedelta(days=SONG_CACHE_DAYS)).isoformat()
    cached = store.song_ids({k for k, _ in have}, fresh_after=fresh_after)
    ids: list[str] = []
    searched = 0
    for key, track in have:
        song = cached.get(key)
        if song is None and budget.searches > 0:
            budget.searches -= 1
            searched += 1
            song = pick_song(track, api.search_songs(search_query(track)))
            if song:
                store.put_song(key, song)
                cached[key] = song
        if song and song not in ids:
            ids.append(song)
    name = str(link["name"])
    current = link.get("navidrome_id")
    try:
        nid = api.save_playlist(name, ids, str(current) if current else None)
    except NavidromeError as exc:
        if exc.code != ERR_NOT_FOUND or not current:
            raise
        nid = api.save_playlist(name, ids)  # deleted in Navidrome: make it again
    return PushResult(nid, len(ids), len(have), len(tracks), searched)


def run_navidrome_push(
    ctx: JobContext,
    store: MirrorStore,
    *,
    client_for: ClientFactory,
    tracks_for: TracksFn,
    index: LibraryIndex,
    now: datetime,
    user_id: str | None = None,
    playlist_id: str | None = None,
    max_searches: int = MAX_SEARCHES_PER_RUN,
) -> dict[str, Any]:
    """Push every mirrored playlist (or one user's, or one playlist)."""
    budget = _Budget(max_searches)
    totals = {"users": 0, "playlists": 0, "matched": 0, "tracks": 0, "searched": 0, "failed": 0}
    for uid in [user_id] if user_id else store.account_users():
        creds = store.credentials(uid)
        links = [
            link
            for link in store.links(uid, enabled_only=True)
            if playlist_id is None or link["spotify_id"] == playlist_id
        ]
        if creds is None or not links:
            continue
        totals["users"] += 1
        api = client_for(creds)
        for link in links:
            ctx.check_cancelled()
            sid = str(link["spotify_id"])
            try:
                res = push_playlist(
                    api, store, link, tracks_for(uid, sid), index, now=now, budget=budget
                )
            except NavidromeError as exc:
                totals["failed"] += 1
                store.record_push(uid, sid, error=str(exc))
                if exc.bad_credentials:
                    ctx.warn(
                        "Navidrome rejected this user's saved login; reconnect it on the Sync page",
                        code="navidrome_auth",
                        user_id=uid,
                    )
                    break
                ctx.warn(
                    f"{link['name']}: Navidrome said {exc}", code="navidrome_error", user_id=uid
                )
                continue
            except Exception as exc:  # Spotify or network hiccup: try the next playlist
                totals["failed"] += 1
                store.record_push(uid, sid, error=str(exc) or type(exc).__name__)
                ctx.warn(f"{link['name']}: push failed ({exc})", code="push_failed", user_id=uid)
                continue
            store.record_push(
                uid,
                sid,
                navidrome_id=res.navidrome_id,
                matched=res.matched,
                in_library=res.in_library,
                total=res.total,
            )
            totals["playlists"] += 1
            totals["matched"] += res.matched
            totals["tracks"] += res.total
            totals["searched"] += res.searched
            ctx.log(
                f"{link['name']}: {res.matched} of {res.total} songs in Navidrome"
                + (
                    f" ({res.in_library - res.matched} not found there yet)"
                    if res.in_library > res.matched
                    else ""
                ),
                user_id=uid,
            )
    if budget.searches <= 0:
        ctx.warn("hit the search limit for one run; the rest continue next run", code="budget")
    return totals
