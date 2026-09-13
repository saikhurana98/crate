"""Library Sync: the ``library_sync`` job (every 2 h, plus "Run now") and the
Sync page's toggles.

For every linked user, one run:

1. **Playlist sync-state.** Records the user's playlists plus Liked Songs.
   A playlist's tracks are refetched only when its Spotify ``snapshot_id``
   changed (Liked Songs has none, so a hash of its contents stands in).
   Counts what's already in the library and flags playlists the user dropped.
2. **Affinity tiers.** Recomputes tier 2 (constants) and tier 3 (flings)
   from the play ledger once the history export is in. Until then, they're
   approximated from Spotify's top-tracks lists. Membership changes are
   recorded, never deleted.
3. **Auto-sync.** Queues every not-yet-downloaded track that an auto-synced
   scope wants, and records it in that scope's ledger so it's queued only
   once. A song another scope already queued isn't queued again. A scope is
   Liked Songs, a playlist, or tier 2 or 3.
   - Each run queues at most ``max_enqueue`` tracks, tier 1 first; the rest
     wait for the next run.
   - These requests run at priority 1, behind the user's own clicks.
   - With no queue, or a cap of 0, the run is a dry run that only logs
     "would enqueue".

Switching auto-sync on for a scope can **baseline** it: the tracks it's
currently missing are written to the ledger without being queued, so only
future additions download. Leave the baseline out to backfill them too.

Log lines that carry personal data (playlist names, tier sizes) are scoped
to their user.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
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
TIER_LABELS = {1: "Playlists & Liked Songs", 2: "Long-term constants", 3: "Short-term flings"}
_ALL = 100_000  # "every member" for tier reads


def playlist_scope(playlist_id: str) -> str:
    return f"playlist:{playlist_id}"


def tier_scope(tier: AffinityTier | int) -> str:
    return f"affinity:{int(tier)}"


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
    def set_auto_sync(self, user_id: str, tier: AffinityTier, on: bool) -> None: ...
    def apply_members(
        self,
        user_id: str,
        tier: AffinityTier,
        members: Iterable[TierMember],
        *,
        approximate: bool = False,
    ) -> tuple[list[str], list[str]]: ...
    def members(
        self,
        user_id: str,
        tier: AffinityTier,
        *,
        include_left: bool = False,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]: ...
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
    def set_sync_mode(self, user_id: str, spotify_id: str, mode: str) -> bool: ...


class SyncQueue(Protocol):
    """The auto-sync ledger + the download queue (implemented over SQLite)."""

    def ledger_keys(self, user_id: str, scope: str) -> set[str]: ...
    def queued_keys(self, user_id: str) -> set[str]: ...
    def baseline(self, user_id: str, scope: str, keys: Iterable[str]) -> int: ...
    def enqueue_tracks(
        self,
        user_id: str,
        scope: str,
        name: str,
        items: Sequence[tuple[str, Mapping[str, Any]]],
    ) -> int: ...


@dataclass(frozen=True)
class SyncDeps:
    """Everything one ``library_sync`` run reads and writes."""

    reader: SyncReader
    history: PlayHistory
    tiers: TierStore
    playlists: PlaylistStateStore
    index: LibraryIndex
    queue: SyncQueue | None = None  # None -> dry run


@dataclass(frozen=True)
class Wanted:
    scope: str
    track: Mapping[str, Any]


@dataclass
class UserSync:
    playlists: int = 0
    changed: int = 0
    removed: int = 0
    errors: int = 0
    tier1_tracks: int = 0
    tier1_in_library: int = 0
    tiers: ListeningTiers | None = None
    wanted: dict[str, Wanted] = field(default_factory=dict)  # merge key -> first scope
    labels: dict[str, str] = field(default_factory=dict)  # scope -> display name


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
        have = {_mkey(t): (t, index.has_track(t)) for t in diff_playlist(set(), tracks)}
        in_lib = sum(1 for _, present in have.values() if present)
        store.set_counts(user.id, pid, track_count=len(have), in_library=in_lib)
        generated = pl.get("owner_id") == SPOTIFY_OWNER
        if not generated:
            tier1.update({key: present for key, (_, present) in have.items()})
        mode = str((known.get(pid) or {}).get("sync_mode") or "inherit")
        if effective_mode(mode, generated=generated, tier1_auto=tier1_auto):
            scope = playlist_scope(pid)
            out.labels[scope] = str(pl.get("name") or "Playlist")
            for key, (track, present) in have.items():
                if not present:
                    out.wanted.setdefault(key, Wanted(scope, track))
    out.playlists = len(entries)
    out.removed = len(store.mark_removed(user.id, {str(e["id"]) for e in entries}))
    out.tier1_tracks = len(tier1)
    out.tier1_in_library = sum(tier1.values())
    return out


def _member_track(m: TierMember) -> dict[str, Any]:
    return {
        "id": m.spotify_id,
        "artist": m.artist,
        "title": m.title,
        "query": f"{m.artist or ''} {m.title or ''}".strip(),
    }


def _compute_tiers(
    ctx: JobContext, user: User, deps: SyncDeps, now: datetime
) -> ListeningTiers | None:
    """Exact tiers from the export; else approximate from top tracks; else None."""
    if deps.history.history_status(user.id).get("export"):
        th = deps.tiers.thresholds(user.id)
        return compute_listening_tiers(deps.history.track_plays(user.id), th, now)
    if TOP_READ_SCOPE in deps.reader.missing_scopes(user.id):
        ctx.warn(
            f"{user.display_name}: log in again to grant top-tracks access; tiers 2-3 skipped",
            code="relogin",
            user_id=user.id,
        )
        return None
    top = {r: deps.reader.top_tracks(user.id, r) for r in TOP_RANGES}
    for time_range, rows in top.items():
        ids = [str(t["id"]) for t in rows if t.get("id")]
        deps.tiers.record_top_snapshot(user.id, time_range, ids)
    return approximate_tiers(top)


def sync_tiers(
    ctx: JobContext, user: User, deps: SyncDeps, now: datetime
) -> tuple[ListeningTiers | None, dict[str, Wanted]]:
    """Recompute tiers 2-3, record membership changes, and collect what their
    auto-sync wants. ``(None, {})`` when neither source is usable."""
    tiers = _compute_tiers(ctx, user, deps, now)
    if tiers is None:
        return None, {}
    settings = deps.tiers.tier_settings(user.id)
    wanted: dict[str, Wanted] = {}
    for tier, members in (
        (AffinityTier.CONSTANT, tiers.constants),
        (AffinityTier.FLING, tiers.flings),
    ):
        entered, left = deps.tiers.apply_members(
            user.id, tier, members, approximate=tiers.approximate
        )
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
                if m.spotify_id and not deps.index.has_key(m.merge_key):
                    wanted.setdefault(m.merge_key, Wanted(tier_scope(tier), _member_track(m)))
    return tiers, wanted


def sync_user(ctx: JobContext, user: User, deps: SyncDeps, now: datetime) -> UserSync:
    tier1_auto = bool(deps.tiers.tier_settings(user.id)[int(AffinityTier.CORE)]["auto_sync"])
    result = sync_playlists(
        ctx, user, deps.reader, deps.playlists, deps.index, tier1_auto=tier1_auto
    )
    result.tiers, tier_wanted = sync_tiers(ctx, user, deps, now)
    for key, want in tier_wanted.items():
        result.wanted.setdefault(key, want)
    for tier in (AffinityTier.CONSTANT, AffinityTier.FLING):
        result.labels[tier_scope(tier)] = TIER_LABELS[int(tier)]
    return result


def plan_enqueue(
    wanted: Mapping[str, Wanted], ledger: Callable[[str], set[str]], queued: set[str]
) -> dict[str, list[tuple[str, Mapping[str, Any]]]]:
    """Group what's wanted by scope. Drops keys the scope already queued or
    baselined, and keys auto-sync already queued for any scope (one download per
    song). ``ledger(scope)`` is read once per scope."""
    seen: dict[str, set[str]] = {}
    out: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
    for key, want in wanted.items():
        if want.scope not in seen:
            seen[want.scope] = ledger(want.scope)
        if key in seen[want.scope] or key in queued:
            continue
        out.setdefault(want.scope, []).append((key, want.track))
    return out


def _scope_rank(scope: str) -> tuple[int, str]:
    """Tier 1 first under the per-run cap: Liked Songs, playlists, then tiers 2-3."""
    if scope == playlist_scope(LIKED_ID):
        return (0, scope)
    return (1, scope) if scope.startswith("playlist:") else (2, scope)


def _enqueue_plan(
    ctx: JobContext,
    user: User,
    plan: Mapping[str, list[tuple[str, Mapping[str, Any]]]],
    labels: Mapping[str, str],
    queue: SyncQueue,
    budget: int,
) -> tuple[int, int]:
    """Queue ``plan`` in scope order within ``budget``; ``(queued, deferred)``."""
    queued = deferred = 0
    for scope in sorted(plan, key=_scope_rank):
        items = plan[scope]
        take = items[: max(0, budget - queued)]
        deferred += len(items) - len(take)
        if not take:
            continue
        label = labels.get(scope, scope)
        queue.enqueue_tracks(user.id, scope, f"Auto-sync: {label}", take)
        queued += len(take)
        ctx.log(
            f"{user.display_name}: queued {len(take)} track(s) from {label}",
            code="enqueued",
            user_id=user.id,
            data={"scope": scope, "count": len(take)},
        )
    if deferred:
        ctx.warn(
            f"{user.display_name}: {deferred} track(s) wait for the next run "
            "(per-run cap CRATE_SYNC_MAX_ENQUEUE)",
            code="deferred",
            user_id=user.id,
        )
    return queued, deferred


def _dry_run_log(
    ctx: JobContext, user: User, plan: Mapping[str, list[tuple[str, Mapping[str, Any]]]]
) -> int:
    pending = sum(len(items) for items in plan.values())
    if pending:
        ctx.log(
            f"{user.display_name}: would enqueue {pending} track(s) (dry run)",
            code="would_enqueue",
            user_id=user.id,
            data={"total": pending, "scopes": {s: len(v) for s, v in plan.items()}},
        )
    return pending


_SUMMARY_KEYS = (
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
    "enqueued",
    "deferred",
)


def _tally(totals: Counter[str], r: UserSync) -> None:
    totals["users"] += 1
    totals["playlists"] += r.playlists
    totals["playlists_changed"] += r.changed
    totals["playlists_removed"] += r.removed
    totals["playlist_errors"] += r.errors
    totals["tier1_tracks"] += r.tier1_tracks
    totals["tier1_in_library"] += r.tier1_in_library
    if r.tiers is not None:
        totals["constants"] += len(r.tiers.constants)
        totals["flings"] += len(r.tiers.flings)
        totals["rising"] += len(r.tiers.rising)
        totals["approximate_users"] += 1 if r.tiers.approximate else 0


def _no_ledger(_scope: str) -> set[str]:
    return set()


def run_library_sync(
    ctx: JobContext,
    users: Iterable[User],
    deps: SyncDeps,
    *,
    now: datetime,
    max_enqueue: int = 300,
) -> dict[str, Any]:
    """The ``library_sync`` job body. A failure for one user is logged and
    skipped; a cancel request stops the run between users and playlists."""
    everyone = list(users)
    linked = [u for u in everyone if deps.reader.connected(u.id)]
    queue = deps.queue
    dry = queue is None or max_enqueue <= 0
    totals: Counter[str] = Counter()
    for i, user in enumerate(linked):
        ctx.check_cancelled()
        ctx.progress(i, len(linked), f"user {i + 1}/{len(linked)}")
        try:
            r = sync_user(ctx, user, deps, now)
        except JobCancelled:
            raise
        except Exception as exc:
            totals["failed"] += 1
            ctx.warn(f"{user.display_name}: sync failed: {exc}", code="user_failed")
            continue
        _tally(totals, r)
        if not r.wanted:
            continue
        if queue is None:
            totals["would_enqueue"] += _dry_run_log(
                ctx, user, plan_enqueue(r.wanted, _no_ledger, set())
            )
            continue
        plan = plan_enqueue(
            r.wanted,
            partial(queue.ledger_keys, user.id),
            queue.queued_keys(user.id),
        )
        if dry:
            totals["would_enqueue"] += _dry_run_log(ctx, user, plan)
            continue
        budget = max_enqueue - totals["enqueued"]
        queued, deferred = _enqueue_plan(ctx, user, plan, r.labels, queue, budget)
        totals["enqueued"] += queued
        totals["deferred"] += deferred
    ctx.progress(len(linked), len(linked))
    if dry:
        tail = f"{totals['would_enqueue']} track(s) would be queued (dry run)"
    else:
        tail = f"queued {totals['enqueued']} track(s)" + (
            f", {totals['deferred']} deferred" if totals["deferred"] else ""
        )
    ctx.log(
        f"synced {totals['users']} user(s): {totals['playlists_changed']} playlist(s) changed, "
        + tail,
        code="summary",
    )
    summary: dict[str, Any] = {k: totals[k] for k in _SUMMARY_KEYS}
    return summary | {"skipped": len(everyone) - len(linked), "dry_run": dry}


class LibrarySyncService:
    """The Sync page's reads and auto-sync toggles (console process).

    Toggling never queues anything itself; the serve-side job does all the
    queueing. Switching a scope on returns how many of its tracks are missing
    from the library. Unless ``backfill`` is set, those tracks are
    **baselined**, so only future additions download."""

    def __init__(
        self,
        playlists: PlaylistStateStore,
        tiers: TierStore,
        queue: SyncQueue,
        index: Callable[[], LibraryIndex],
    ) -> None:
        self._playlists = playlists
        self._tiers = tiers
        self._queue = queue
        self._index = index

    def _tier1_auto(self, user_id: str) -> bool:
        return bool(self._tiers.tier_settings(user_id)[int(AffinityTier.CORE)]["auto_sync"])

    def _playlist_missing(self, user_id: str, pid: str, index: LibraryIndex) -> list[str]:
        tracks = diff_playlist(set(), self._playlists.items(user_id, pid))
        return [_mkey(t) for t in tracks if not index.has_track(t)]

    def _tier_missing(self, user_id: str, tier: AffinityTier, index: LibraryIndex) -> list[str]:
        return [
            str(m["merge_key"])
            for m in self._tiers.members(user_id, tier, limit=_ALL)
            if m.get("spotify_id") and not index.has_key(str(m["merge_key"]))
        ]

    def _inheriting(self, user_id: str) -> list[str]:
        """Ids of playlists that follow tier 1's toggle (inherit, not Spotify-made)."""
        return [
            str(p["spotify_id"])
            for p in self._playlists.playlists(user_id)
            if p["sync_mode"] == "inherit" and not p["generated"]
        ]

    def missing(self, user_id: str, scope: str) -> int:
        """How many tracks switching ``scope`` on would bring in (``core`` = tier 1)."""
        index = self._index()
        if scope == "core":
            keys: set[str] = set()
            for pid in self._inheriting(user_id):
                keys.update(self._playlist_missing(user_id, pid, index))
            return len(keys)
        kind, _, arg = scope.partition(":")
        if kind == "affinity" and arg in ("2", "3"):
            return len(self._tier_missing(user_id, AffinityTier(int(arg)), index))
        if kind == "playlist" and arg:
            return len(self._playlist_missing(user_id, arg, index))
        raise ValueError(f"unknown scope {scope!r}")

    def set_tier_auto_sync(
        self, user_id: str, tier: int, on: bool, *, backfill: bool
    ) -> dict[str, Any]:
        t = AffinityTier(tier)
        was = bool(self._tiers.tier_settings(user_id)[int(t)]["auto_sync"])
        self._tiers.set_auto_sync(user_id, t, on)
        missing = baselined = 0
        if on and not was:
            index = self._index()
            if t is AffinityTier.CORE:
                scopes = [
                    (playlist_scope(pid), self._playlist_missing(user_id, pid, index))
                    for pid in self._inheriting(user_id)
                ]
            else:
                scopes = [(tier_scope(t), self._tier_missing(user_id, t, index))]
            missing = len({k for _, keys in scopes for k in keys})
            if not backfill:
                baselined = sum(self._queue.baseline(user_id, s, keys) for s, keys in scopes)
        return {
            "tier": int(t),
            "auto_sync": on,
            "missing": missing,
            "baselined": baselined,
            "backfill": backfill,
        }

    def set_playlist_mode(
        self, user_id: str, playlist_id: str, mode: str, *, backfill: bool
    ) -> dict[str, Any] | None:
        rows = self._playlists.playlists(user_id, include_removed=True)
        row = next((p for p in rows if p["spotify_id"] == playlist_id), None)
        if row is None:
            return None
        tier1 = self._tier1_auto(user_id)
        generated = bool(row["generated"])
        before = effective_mode(str(row["sync_mode"]), generated=generated, tier1_auto=tier1)
        self._playlists.set_sync_mode(user_id, playlist_id, mode)
        after = effective_mode(mode, generated=generated, tier1_auto=tier1)
        missing = baselined = 0
        if after and not before:
            keys = self._playlist_missing(user_id, playlist_id, self._index())
            missing = len(keys)
            if not backfill:
                baselined = self._queue.baseline(user_id, playlist_scope(playlist_id), keys)
        return {
            "playlist_id": playlist_id,
            "mode": mode,
            "auto_sync": after,
            "missing": missing,
            "baselined": baselined,
        }

    def tier_members(
        self, user_id: str, tier: int, *, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        index = self._index()
        rows = self._tiers.members(user_id, AffinityTier(tier), limit=limit, offset=offset)
        return [r | {"in_library": index.has_key(str(r["merge_key"]))} for r in rows]

    def _tier_card(self, user_id: str, tier: AffinityTier, index: LibraryIndex) -> dict[str, Any]:
        members = self._tiers.members(user_id, tier, limit=_ALL)
        return {
            "tier": int(tier),
            "label": TIER_LABELS[int(tier)],
            "count": len(members),
            "in_library": sum(1 for m in members if index.has_key(str(m["merge_key"]))),
            "approximate": any(m["approximate"] for m in members),
        }

    def overview(self, user_id: str) -> dict[str, Any]:
        """Tier cards + the playlist list for the Sync page."""
        index = self._index()
        settings = self._tiers.tier_settings(user_id)
        tier1_auto = bool(settings[int(AffinityTier.CORE)]["auto_sync"])
        rows = self._playlists.playlists(user_id)
        core: dict[str, bool] = {}
        for p in rows:
            if not p["generated"]:
                items = self._playlists.items(user_id, str(p["spotify_id"]))
                core.update({_mkey(t): index.has_track(t) for t in diff_playlist(set(), items)})
        tier1 = {
            "tier": 1,
            "label": TIER_LABELS[1],
            "count": len(core),
            "in_library": sum(core.values()),
            "approximate": False,
        }
        cards = [tier1] + [
            self._tier_card(user_id, t, index) for t in (AffinityTier.CONSTANT, AffinityTier.FLING)
        ]
        tiers = [card | settings[int(card["tier"])] for card in cards]
        playlists = [
            p
            | {
                "auto_sync": effective_mode(
                    str(p["sync_mode"]), generated=bool(p["generated"]), tier1_auto=tier1_auto
                )
            }
            for p in rows
        ]
        return {"tiers": tiers, "playlists": playlists}
