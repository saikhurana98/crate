# ADR-0021: Deduplicate purge moves files to a library trash (admin only)

- Status: accepted
- Date: 2026-09-14
- Related: ADR-0018 (management console), ADR-0020 (background jobs)

## Context

The dedup index (PRs #69–#71) finds copies of the same recording and ranks which
one to keep. Removing the rest is the first feature that deletes user files, on an
NFS-mounted library that Navidrome serves and that download records point into. A
mistake has to be recoverable, and nothing should happen that the user didn't see.

## Decision

1. **Admin only.** Every route that moves or deletes files returns 403 unless the
   caller is an admin:
   - a logged-in user whose Spotify id is in `CRATE_ADMIN_SPOTIFY_IDS`;
   - the automation API key;
   - the single-user console.

   Everyone can still read the groups, A/B the copies, and ignore groups.
2. **The console only queues work; `serve` does it.** The console mounts the
   library read-only. Purge, restore and empty run as jobs on the `mutate` lane in
   `serve`, one at a time, with their logs on the jobs WebSocket.
3. **Check again at the last moment.** Each queued copy carries the size and mtime
   the user saw. Right before moving it, `serve` refuses the copy when any of these
   hold:
   - it changed;
   - it or the keeper is gone;
   - it is the keeper;
   - a queued or running review upgrade targets it.
4. **Trash, not delete.** A purge renames the file to
   `<root>/.crate-trash/<date>/<original path>`.
   - It is a same-filesystem `os.rename`. A cross-device move (`EXDEV`) stops the
     job; there is never a copy followed by a delete.
   - The trash is a dot dir with a `.ndignore`, so neither the scan nor Navidrome
     indexes it.
5. **Journal, then one transaction.** A `trash_items` row is written as `pending`
   before the rename. One transaction then:
   - repoints `downloads` rows at the keeper, copying the keeper's quality columns
     so lossless checks stay right;
   - repoints review upgrade targets;
   - marks the index row missing;
   - marks the journal row `trashed`.

   If that transaction fails, the file is renamed back. Every mutate job first
   settles rows a crash left `pending`, moving the file back.
6. **Restore and retention.**
   - Restore renames the file back, or to `… (restored-N)` if the path was taken
     meanwhile. It bumps the file's analysis time so the next grouping compares it
     again.
   - A daily `trash_empty` job deletes trash older than
     `CRATE_TRASH_RETENTION_DAYS` (default 30).
   - Emptying everything needs an admin and a typed `EMPTY`.
7. **Navidrome.** If `CRATE_NAVIDROME_URL/USER/PASS` are set, a purge or restore
   asks Navidrome to rescan (Subsonic token auth). Otherwise its own schedule
   catches up. Navidrome playlists and stars attach to the file they point at, so
   trashing a starred copy loses that star on the Navidrome side.

## Consequences

- A wrong purge is a restore away for 30 days. Only emptying is irreversible, and
  it is doubly gated.
- Download history stays accurate: records follow the surviving copy.
- The trash uses disk until retention clears it. The Dedup page shows its size.
- `serve` needs the library mounted read-write on the same filesystem as the
  trash, which already holds for the staging dir (ADR-0008).
