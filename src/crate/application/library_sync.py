"""Library Sync: the ``library_sync`` job (every 2 h, plus "Run now").

For every linked user, one run:

1. **Playlist sync-state.** Records the user's playlists plus Liked Songs.
   A playlist's tracks are refetched only when its Spotify ``snapshot_id``
   changed (Liked Songs has none, so a hash of its contents stands in).
   Counts what's already in the library and flags playlists the user dropped.
2. **Affinity tiers.** Recomputes tier 2 (constants) and tier 3 (flings)
   from the play ledger once the history export is in. Until then, they're
   approximated from Spotify's top-tracks lists. Membership changes are
   recorded, never deleted.
3. **Auto-sync plan.** Works out which not-yet-downloaded tracks the
   auto-synced scopes want. For now this is a **dry run**: it logs "would
   enqueue" counts and queues nothing.

Log lines that carry personal data (playlist names, tier sizes) are scoped
to their user.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from crate.application.affinity import (
    TOP_RANGES,
    ListeningTiers,
    TierMember,
    TrackPlays,
    approximate_tiers,
    compute_listening_tiers,
)
from crate.application.auth import User
from crate.application.jobs import JobCancelled, JobContext
from crate.domain.affinity import AffinityThresholds, AffinityTier
from crate.domain.listening import merge_key
from crate.domain.matching import norm_tokens

LIKED_ID = "liked"  # the pseudo-playlist id for Liked Songs
SPOTIFY_OWNER = "spotify"  # owner id of Spotify-made playlists (Discover Weekly, mixes)
SYNC_MODES = ("inherit", "on", "off")
TOP_READ_SCOPE = "user-top-read"


def track_key(track: dict[str, Any]) -> str:
    """A stable dedupe key for a playlist track: its ISRC when present (the
    strongest identity), else the normalized artist+title tokens (the same
    normalizer the cascade's matcher uses). Keyed strings are prefixed so an ISRC
    can never collide with a token key. Empty/uninformative tracks collapse to
    ``tok:`` — the watcher skips those before enqueueing."""
    isrc = str(track.get("isrc") or "").strip()
    if isrc:
        return f"isrc:{isrc.upper()}"
    tokens = norm_tokens(str(track.get("artist") or "")) + norm_tokens(
        str(track.get("title") or "")
    )
    return "tok:" + " ".join(tokens)


def diff_playlist(known: set[str], current: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The tracks in ``current`` whose :func:`track_key` isn't already in ``known``.

    Preserves playlist order, de-dupes within the current snapshot (a track listed
    twice enqueues once), and drops uninformative rows (an empty ``tok:`` key — no
    ISRC and no usable artist/title). ``known`` is the enqueued-ledger key set."""
    seen = set(known)
    out: list[dict[str, Any]] = []
    for track in current:
        key = track_key(track)
        if key == "tok:" or key in seen:
            continue
        seen.add(key)
        out.append(track)
    return out


def content_snapshot(tracks: Iterable[Mapping[str, Any]]) -> str:
    """A stand-in ``snapshot_id`` for Liked Songs, which Spotify gives none: a short
    hash of the ordered track keys, so an unchanged list reads as unchanged."""
    joined = "\n".join(track_key(dict(t)) for t in tracks)
    return "c:" + hashlib.sha1(joined.encode(), usedforsecurity=False).hexdigest()[:16]


def effective_mode(mode: str, *, generated: bool, tier1_auto: bool) -> bool:
    """Whether a playlist auto-syncs. An explicit ``on``/``off`` wins; ``inherit``
    follows tier 1's toggle, except for Spotify-made playlists (they churn weekly)."""
    if mode == "on":
        return True
    if mode == "off":
        return False
    return tier1_auto and not generated


def _mkey(track: Mapping[str, Any]) -> str:
    return merge_key(track.get("artist"), track.get("title"))


@dataclass(frozen=True)
class LibraryIndex:
    """What's already downloaded: ISRCs + merge keys of completed requests."""

    isrcs: frozenset[str] = frozenset()
    keys: frozenset[str] = frozenset()

    def has_key(self, key: str) -> bool:
        return key in self.keys

    def has_track(self, track: Mapping[str, Any]) -> bool:
        isrc = str(track.get("isrc") or "").strip().upper()
        return bool(isrc and isrc in self.isrcs) or _mkey(track) in self.keys


class SyncReader(Protocol):
    """A user's Spotify, as the sync needs it (implemented by ``SpotifyUser``)."""

    def connected(self, user_id: str) -> bool: ...
    def missing_scopes(self, user_id: str) -> list[str]: ...
    def my_playlists(self, user_id: str) -> list[dict[str, Any]]: ...
    def liked_songs(self, user_id: str, max_tracks: int = 2000) -> list[dict[str, Any]]: ...
    def playlist_tracks(
        self, user_id: str, playlist_id: str, max_tracks: int = 2000
    ) -> list[dict[str, Any]]: ...
    def top_tracks(
        self, user_id: str, time_range: str, limit: int = 100
    ) -> list[dict[str, Any]]: ...


class PlayHistory(Protocol):
    def history_status(self, user_id: str) -> dict[str, Any]: ...
    def track_plays(self, user_id: str) -> list[TrackPlays]: ...


class TierStore(Protocol):
    def thresholds(self, user_id: str) -> AffinityThresholds: ...
    def tier_settings(self, user_id: str) -> dict[int, dict[str, Any]]: ...
    def apply_members(
        self,
        user_id: str,
        tier: AffinityTier,
        members: Iterable[TierMember],
        *,
        approximate: bool = False,
    ) -> tuple[list[str], list[str]]: ...
    def record_top_snapshot(self, user_id: str, time_range: str, ids: list[str]) -> None: ...


class PlaylistStateStore(Protocol):
    def playlists(self, user_id: str, *, include_removed: bool = False) -> list[dict[str, Any]]: ...
    def save_playlist(self, user_id: str, row: Mapping[str, Any], *, changed: bool) -> None: ...
    def set_items(
        self, user_id: str, spotify_id: str, tracks: Sequence[Mapping[str, Any]]
    ) -> None: ...
    def items(self, user_id: str, spotify_id: str) -> list[dict[str, Any]]: ...
    def set_counts(
        self, user_id: str, spotify_id: str, *, track_count: int, in_library: int
    ) -> None: ...
    def mark_removed(self, user_id: str, present: set[str]) -> list[str]: ...


@dataclass
class UserSync:
    playlists: int = 0
    changed: int = 0
    removed: int = 0
    errors: int = 0
    tier1_tracks: int = 0
    tier1_in_library: int = 0
    tiers: ListeningTiers | None = None
    wanted: dict[str, str] = field(default_factory=dict)  # merge key -> scope wanting it


def _refresh_playlist(
    user: User,
    pl: Mapping[str, Any],
    prev: Mapping[str, Any] | None,
    reader: SyncReader,
    store: PlaylistStateStore,
    liked: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Save one playlist's state; refetch its tracks only if its snapshot moved."""
    pid = str(pl["id"])
    snapshot = pl.get("snapshot_id")
    changed = prev is None or not snapshot or prev.get("snapshot_id") != snapshot
    if pid == LIKED_ID:
        tracks = liked
    elif changed:
        tracks = reader.playlist_tracks(user.id, pid)
    else:
        tracks = store.items(user.id, pid)
    owner = pl.get("owner_id")
    row = {
        "spotify_id": pid,
        "name": str(pl.get("name") or "Playlist"),
        "owner_id": owner,
        "owned": pid == LIKED_ID or (owner is not None and owner == user.spotify_id),
        "generated": owner == SPOTIFY_OWNER,
        "image": pl.get("image"),
        "snapshot_id": snapshot,
    }
    store.save_playlist(user.id, row, changed=changed)
    if changed:
        store.set_items(user.id, pid, tracks)
    return tracks, changed


def sync_playlists(
    ctx: JobContext,
    user: User,
    reader: SyncReader,
    store: PlaylistStateStore,
    index: LibraryIndex,
    *,
    tier1_auto: bool,
) -> UserSync:
    """Refresh the user's playlist sync-state and collect what auto-sync wants."""
    out = UserSync()
    known = {str(p["spotify_id"]): p for p in store.playlists(user.id, include_removed=True)}
    liked = reader.liked_songs(user.id)
    entries: list[Mapping[str, Any]] = [
        {
            "id": LIKED_ID,
            "name": "Liked Songs",
            "owner_id": user.spotify_id,
            "image": None,
            "snapshot_id": content_snapshot(liked),
        },
        *reader.my_playlists(user.id),
    ]
    tier1: dict[str, bool] = {}  # merge key -> already in the library
    for pl in entries:
        ctx.check_cancelled()
        pid = str(pl["id"])
        try:
            tracks, changed = _refresh_playlist(user, pl, known.get(pid), reader, store, liked)
        except Exception as exc:  # one unreadable playlist must not stop the rest
            out.errors += 1
            ctx.warn(
                f"{user.display_name}: playlist {pl.get('name')!r} skipped: {exc}",
                code="playlist_failed",
                user_id=user.id,
            )
            continue
        out.changed += 1 if changed else 0
        unique = diff_playlist(set(), tracks)
        have = {_mkey(t): index.has_track(t) for t in unique}
        store.set_counts(user.id, pid, track_count=len(unique), in_library=sum(have.values()))
        generated = pl.get("owner_id") == SPOTIFY_OWNER
        if not generated:
            tier1.update(have)
        mode = str((known.get(pid) or {}).get("sync_mode") or "inherit")
        if effective_mode(mode, generated=generated, tier1_auto=tier1_auto):
            for key, present in have.items():
                if not present:
                    out.wanted.setdefault(key, f"playlist:{pid}")
    out.playlists = len(entries)
    out.removed = len(store.mark_removed(user.id, {str(e["id"]) for e in entries}))
    out.tier1_tracks = len(tier1)
    out.tier1_in_library = sum(tier1.values())
    return out


def sync_tiers(
    ctx: JobContext,
    user: User,
    reader: SyncReader,
    history: PlayHistory,
    store: TierStore,
    index: LibraryIndex,
    now: datetime,
) -> tuple[ListeningTiers | None, dict[str, str]]:
    """Recompute tiers 2-3 (exact from the export, else approximate) and collect
    what their auto-sync wants. ``(None, {})`` when neither source is usable."""
    if history.history_status(user.id).get("export"):
        tiers = compute_listening_tiers(
            history.track_plays(user.id), store.thresholds(user.id), now
        )
    elif TOP_READ_SCOPE in reader.missing_scopes(user.id):
        ctx.warn(
            f"{user.display_name}: log in again to grant top-tracks access; tiers 2-3 skipped",
            code="relogin",
            user_id=user.id,
        )
        return None, {}
    else:
        top = {r: reader.top_tracks(user.id, r) for r in TOP_RANGES}
        for time_range, rows in top.items():
            store.record_top_snapshot(
                user.id, time_range, [str(t["id"]) for t in rows if t.get("id")]
            )
        tiers = approximate_tiers(top)
    settings = store.tier_settings(user.id)
    wanted: dict[str, str] = {}
    for tier, members in (
        (AffinityTier.CONSTANT, tiers.constants),
        (AffinityTier.FLING, tiers.flings),
    ):
        entered, left = store.apply_members(user.id, tier, members, approximate=tiers.approximate)
        label = "constants" if tier is AffinityTier.CONSTANT else "flings"
        ctx.log(
            f"{user.display_name}: {len(members)} {label} (+{len(entered)} / -{len(left)})"
            + (" (approximate)" if tiers.approximate else ""),
            code="tier",
            user_id=user.id,
            data={
                "tier": int(tier),
                "count": len(members),
                "entered": len(entered),
                "left": len(left),
            },
        )
        if settings[int(tier)]["auto_sync"]:
            for m in members:
                if m.spotify_id and not index.has_key(m.merge_key):
                    wanted.setdefault(m.merge_key, f"affinity:{int(tier)}")
    return tiers, wanted


def sync_user(
    ctx: JobContext,
    user: User,
    reader: SyncReader,
    history: PlayHistory,
    tiers_store: TierStore,
    playlists: PlaylistStateStore,
    index: LibraryIndex,
    now: datetime,
) -> UserSync:
    tier1_auto = bool(tiers_store.tier_settings(user.id)[int(AffinityTier.CORE)]["auto_sync"])
    result = sync_playlists(ctx, user, reader, playlists, index, tier1_auto=tier1_auto)
    result.tiers, tier_wanted = sync_tiers(ctx, user, reader, history, tiers_store, index, now)
    for key, scope in tier_wanted.items():
        result.wanted.setdefault(key, scope)
    if result.wanted:
        ctx.log(
            f"{user.display_name}: would enqueue {len(result.wanted)} track(s) (dry run)",
            code="would_enqueue",
            user_id=user.id,
            data={"total": len(result.wanted), "scopes": dict(Counter(result.wanted.values()))},
        )
    return result


def run_library_sync(
    ctx: JobContext,
    users: Iterable[User],
    reader: SyncReader,
    history: PlayHistory,
    tiers: TierStore,
    playlists: PlaylistStateStore,
    index: LibraryIndex,
    *,
    now: datetime,
) -> dict[str, Any]:
    """The ``library_sync`` job body. A failure for one user is logged and
    skipped; a cancel request stops the run between users and playlists."""
    everyone = list(users)
    linked = [u for u in everyone if reader.connected(u.id)]
    totals: Counter[str] = Counter()
    for i, user in enumerate(linked):
        ctx.check_cancelled()
        ctx.progress(i, len(linked), f"user {i + 1}/{len(linked)}")
        try:
            r = sync_user(ctx, user, reader, history, tiers, playlists, index, now)
        except JobCancelled:
            raise
        except Exception as exc:
            totals["failed"] += 1
            ctx.warn(f"{user.display_name}: sync failed: {exc}", code="user_failed")
            continue
        totals["users"] += 1
        totals["playlists"] += r.playlists
        totals["playlists_changed"] += r.changed
        totals["playlists_removed"] += r.removed
        totals["playlist_errors"] += r.errors
        totals["tier1_tracks"] += r.tier1_tracks
        totals["tier1_in_library"] += r.tier1_in_library
        totals["would_enqueue"] += len(r.wanted)
        if r.tiers is not None:
            totals["constants"] += len(r.tiers.constants)
            totals["flings"] += len(r.tiers.flings)
            totals["rising"] += len(r.tiers.rising)
            totals["approximate_users"] += 1 if r.tiers.approximate else 0
    ctx.progress(len(linked), len(linked))
    ctx.log(
        f"synced {totals['users']} user(s): {totals['playlists_changed']} playlist(s) changed, "
        f"{totals['would_enqueue']} track(s) would be queued (dry run)",
        code="summary",
    )
    keys = [
        "users",
        "failed",
        "playlists",
        "playlists_changed",
        "playlists_removed",
        "playlist_errors",
        "tier1_tracks",
        "tier1_in_library",
        "constants",
        "flings",
        "rising",
        "approximate_users",
        "would_enqueue",
    ]
    return {k: totals[k] for k in keys} | {"skipped": len(everyone) - len(linked), "dry_run": True}
