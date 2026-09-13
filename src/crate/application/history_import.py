"""Spotify streaming-history import: the one-time backfill behind tiers 2/3.

The user uploads Spotify's **Extended streaming history** export (a zip of
``Streaming_History_Audio_*.json``); the ``history_import`` job turns every real
listen into a ledger row with ``source='export'``. The export is authoritative
for the time span it covers:

- An export row **replaces** a polled (``recent``) row with the same
  (time, song). This happens at insert time, so an exact duplicate is never
  lost.
- After the import, any polled rows still inside the export's span are
  near-duplicates (timestamps a few seconds apart) and are deleted.

The short 1-year "account data" export (``StreamingHistory_music_*.json``: no
track ids, minute precision) is also accepted, with a warning.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any, Protocol

from crate.application.jobs import JobContext
from crate.domain.listening import Play, merge_key, norm_played_at

MIN_PLAY_MS = 30_000  # Spotify's own "counts as a stream" threshold
MIN_SKIPPED_MS = 60_000  # a skip only counts if it still played a full minute
_BATCH = 5_000


class ExportFile(Protocol):
    @property
    def name(self) -> str: ...

    def load(self) -> list[dict[str, Any]]: ...


class HistoryStore(Protocol):
    def add_plays(self, user_id: str, plays: Iterable[Play], source: str) -> int: ...

    def delete_recent_in_range(self, user_id: str, start: str, end: str) -> int: ...

    def set_export_range(self, user_id: str, start: str, end: str) -> None: ...


def detect_format(entry: dict[str, Any]) -> str | None:
    """``extended`` (full history, has track ids), ``short`` (1-year account
    data), or ``None`` for anything else."""
    if "ts" in entry and (
        "master_metadata_track_name" in entry
        or "spotify_track_uri" in entry
        or "spotify_episode_uri" in entry
    ):
        return "extended"
    if "endTime" in entry and "trackName" in entry:
        return "short"
    return None


def _ms(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _skip_reason(entry: dict[str, Any], fmt: str, title: Any, ms: int) -> str | None:
    """Why an export row isn't a listen, or ``None`` if it is. The podcast check
    runs first: podcast rows have no track name, and would otherwise be
    reported as ``no_track``."""
    if fmt == "extended" and any(
        entry.get(k)
        for k in ("spotify_episode_uri", "episode_name", "audiobook_title", "audiobook_uri")
    ):
        return "podcast"
    if not title:
        return "no_track"
    if ms < MIN_PLAY_MS:
        return "too_short"
    if fmt == "extended" and entry.get("skipped") and ms < MIN_SKIPPED_MS:
        return "skipped"
    return None


def parse_entry(entry: dict[str, Any], fmt: str) -> tuple[Play | None, str | None]:
    """One export row -> ``(play, None)`` when it counts as a listen, else
    ``(None, reason)``, where reason is ``podcast`` / ``no_track`` /
    ``too_short`` / ``skipped`` / ``bad_time``. Local files are kept, with no
    track uri."""
    if fmt == "extended":
        title = entry.get("master_metadata_track_name")
        artist = entry.get("master_metadata_album_artist_name")
        album = entry.get("master_metadata_album_album_name") or None
        raw_ts, uri, ms = (
            entry.get("ts"),
            entry.get("spotify_track_uri"),
            _ms(entry.get("ms_played")),
        )
    else:
        title, artist, album = entry.get("trackName"), entry.get("artistName"), None
        raw_ts, uri, ms = entry.get("endTime"), None, _ms(entry.get("msPlayed"))
    reason = _skip_reason(entry, fmt, title, ms)
    if reason is not None:
        return None, reason
    try:
        at = norm_played_at(str(raw_ts))
    except (TypeError, ValueError):
        return None, "bad_time"
    track_uri = str(uri) if uri and str(uri).startswith("spotify:track:") else None
    artist_s = str(artist) if artist else None
    return Play(
        at, merge_key(artist_s, str(title)), track_uri, artist_s, str(title), album, ms
    ), None


def run_history_import(
    ctx: JobContext, user_id: str, files: Sequence[ExportFile], store: HistoryStore
) -> dict[str, Any]:
    """The ``history_import`` job body. Loads one export file at a time (a full
    history is tens of MB of JSON), writes plays in batches, then records the
    covered span and drops polled near-duplicates inside it."""
    total, rows, written = len(files), 0, 0
    skipped: Counter[str] = Counter()
    formats: set[str] = set()
    lo: str | None = None
    hi: str | None = None
    ctx.log(f"importing {total} history file(s)", code="import_start")
    for index, export_file in enumerate(files, 1):
        ctx.check_cancelled()
        batch: list[Play] = []
        file_written = 0
        for entry in export_file.load():
            rows += 1
            fmt = detect_format(entry)
            if fmt is None:
                skipped["unknown_format"] += 1
                continue
            formats.add(fmt)
            play, reason = parse_entry(entry, fmt)
            if play is None:
                skipped[reason or "unknown"] += 1
                continue
            lo = play.played_at if lo is None or play.played_at < lo else lo
            hi = play.played_at if hi is None or play.played_at > hi else hi
            batch.append(play)
            if len(batch) >= _BATCH:
                file_written += store.add_plays(user_id, batch, "export")
                batch = []
        if batch:
            file_written += store.add_plays(user_id, batch, "export")
        written += file_written
        ctx.progress(index, total, f"{export_file.name}: +{file_written} plays")
    if "short" in formats:
        ctx.warn(
            "this is Spotify's 1-year account-data export (no track ids); for full history, "
            "request 'Extended streaming history' in Spotify's privacy settings",
            code="short_export",
        )
    if lo is None or hi is None:
        ctx.warn("no importable plays found in the upload", code="empty_import")
        return {
            "files": total,
            "rows": rows,
            "imported": 0,
            "skipped": dict(skipped),
            "range": None,
        }
    replaced = store.delete_recent_in_range(user_id, lo, hi)
    store.set_export_range(user_id, lo, hi)
    ctx.log(
        f"history {lo[:10]} to {hi[:10]}: {written} plays written, "
        f"{replaced} overlapping polled plays folded into the export",
        code="import_done",
    )
    return {
        "files": total,
        "rows": rows,
        "imported": written,
        "skipped": dict(skipped),
        "range": [lo, hi],
    }
