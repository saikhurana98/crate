"""Listening-ledger use-cases: poll every linked user's recently-played plays
into the ledger (``listen_poll`` job). The streaming-history import (PR 5) is the
other writer.

Spotify only keeps a user's **last 50 plays**, so the poll runs every 30 minutes
(``CRATE_LISTEN_POLL_INTERVAL``) and follows each user's ``after`` cursor. A page
that is still full when paging stops means 50+ plays since the last poll, so
some may be missing; that case is logged as a possible gap.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from crate.application.auth import User
from crate.application.jobs import JobContext
from crate.domain.listening import Play, merge_key, norm_played_at

RECENT_PAGE = 50  # Spotify's recently-played page (and retention) size
_MAX_PAGES = 4


class RecentReader(Protocol):
    def connected(self, user_id: str) -> bool: ...

    def recent_plays_after(
        self, user_id: str, after_ms: int | None
    ) -> tuple[list[dict[str, Any]], int | None]: ...


class LedgerStore(Protocol):
    def recent_cursor(self, user_id: str) -> int | None: ...

    def set_recent_cursor(self, user_id: str, after_ms: int | None) -> None: ...

    def add_plays(self, user_id: str, plays: Iterable[Play], source: str) -> int: ...

    def upsert_tracks(self, tracks: Iterable[dict[str, Any]]) -> None: ...


def play_from_recent(item: dict[str, Any]) -> Play | None:
    """A ledger row from one recently-played item, or ``None`` when it has no
    title or a usable timestamp."""
    played, title = item.get("played_at"), item.get("title")
    if not played or not title:
        return None
    try:
        at = norm_played_at(str(played))
    except ValueError:
        return None
    artist = item.get("artist") or None
    return Play(
        played_at=at,
        merge_key=merge_key(artist, str(title)),
        track_uri=item.get("uri") or None,
        artist=artist,
        title=str(title),
        album=item.get("album") or None,
    )


@dataclass(frozen=True)
class PollResult:
    fetched: int
    inserted: int
    pages: int
    gap_suspected: bool


def poll_user(
    user_id: str, reader: RecentReader, store: LedgerStore, *, max_pages: int = _MAX_PAGES
) -> PollResult:
    """Append ``user_id``'s plays since their cursor, then advance the cursor.

    Pages forward while pages come back full and the cursor moves. If the last
    page is still full after an existing cursor, flag a possible gap. A full page
    on the very first poll is expected (it's just the last 50 plays)."""
    cursor = store.recent_cursor(user_id)
    had_cursor = cursor is not None
    fetched = inserted = pages = 0
    full = False
    while pages < max_pages:
        items, next_cursor = reader.recent_plays_after(user_id, cursor)
        pages += 1
        fetched += len(items)
        plays = [p for p in (play_from_recent(i) for i in items) if p is not None]
        inserted += store.add_plays(user_id, plays, "recent")
        store.upsert_tracks(i for i in items if i.get("id"))
        full = len(items) >= RECENT_PAGE
        advanced = next_cursor is not None and next_cursor != cursor
        cursor = next_cursor
        if not full or not advanced:
            break
    store.set_recent_cursor(user_id, cursor)
    return PollResult(fetched, inserted, pages, gap_suspected=had_cursor and full)


def run_listen_poll(
    ctx: JobContext, users: Iterable[User], reader: RecentReader, store: LedgerStore
) -> dict[str, Any]:
    """The ``listen_poll`` job body: poll every connected user. A failure for one
    user (expired token, Spotify hiccup) is logged and skipped. Log lines carry
    counts only, never track names, so they're safe to show every viewer."""
    polled = new = skipped = failed = 0
    for user in users:
        ctx.check_cancelled()
        if not reader.connected(user.id):
            skipped += 1
            continue
        try:
            result = poll_user(user.id, reader, store)
        except Exception as exc:
            failed += 1
            ctx.warn(f"{user.display_name}: poll failed: {exc}", code="poll_failed")
            continue
        polled += 1
        new += result.inserted
        if result.gap_suspected:
            ctx.warn(
                f"{user.display_name}: 50+ plays since the last poll — some may be missing "
                "(lower CRATE_LISTEN_POLL_INTERVAL)",
                code="gap",
            )
        if result.inserted:
            ctx.log(f"{user.display_name}: +{result.inserted} new play(s)", code="plays")
    ctx.log(f"polled {polled} user(s), {new} new play(s)", code="summary")
    return {"users": polled, "new_plays": new, "skipped": skipped, "failed": failed}
