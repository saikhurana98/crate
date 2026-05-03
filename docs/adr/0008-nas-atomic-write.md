# ADR-0008: NAS atomic-write strategy — temp file + rename, with SMB / NFS caveats

- **Status:** accepted
- **Date:** 2026-05-03

## Context

The NAS is the single source of truth for downloaded audio. Files land in two phases: provisional (lossy from the rip tier) and final (FLAC from the endgoal tier). Multiple endgoal candidates may finish writing within milliseconds of each other (all racing for the same final path). DJ applications (Rekordbox, Serato, Traktor, file managers) may be reading the directory at any time. We must guarantee:

1. **No partial files visible to readers**. A file appearing in `library/<Artist>/<Album>/` is always complete and decodable.
2. **At-most-one final file per `(artist, album, title)`** even under concurrent writers.
3. **Crash safety**: a kill -9 mid-write leaves no zombie file in the final library.
4. **Cross-FS-protocol behaviour known**: NAS could be exported via SMB or NFS depending on user setup.

POSIX `rename(2)` is atomic on the same filesystem. SMB and NFS each have wrinkles.

## Decision

The atomic write protocol, implemented in `infrastructure/nas/writer.ts`:

```
1. Stream payload into  library/.tmp/<job-id>-<source-id>.partial
   - Open with O_EXCL (fail if file exists — avoids stale-partial collisions)
2. fsync(file)
3. Ensure parent dir of finalPath exists (mkdir -p)
4. fs.rename(tmpPath, finalPath)   — atomic on same mount
5. fsync(parentDir)                 — ensures rename is durable
6. (DB tx commits status='final' transition only after step 5 succeeds)
```

**Critical invariant**: `library/.tmp/` and `library/<Artist>/<Album>/` are always on the **same NAS mount**. If they were on different mounts, `rename` degrades silently to copy+delete, breaking atomicity. We enforce this by construction (everything under one `NAS_ROOT`) and document it loudly.

**Concurrent endgoal winners**: two adapters may both finish a FLAC for the same job within ms. We rely on the optimistic DB row update to gate the rename:

```sql
UPDATE jobs
   SET status = 'final', final_path = ?, ...
 WHERE id = ?
   AND status = 'provisional'
RETURNING id;
```

Whichever transaction commits first wins. The losing transaction returns 0 rows — that adapter then `unlink`s its tmp and exits clean. The rename itself never races because only the winning transaction performs the file move.

### SMB caveat

`fsync` on SMB shares only flushes the SMB-client cache to the server; the server's own write-back behaviour depends on its config. For music files (re-downloadable on loss) we accept SMB's default durability. If users need stronger guarantees, they can configure their SMB mount with `cache=none` or use NFS.

`O_EXCL` semantics on SMB are usually correct, but old SMB1 dialects can mishandle it. We require SMB ≥ 2.1.

### NFS caveat

NFS `fsync` flushes client → server. To force server-side durability, mount with `sync` (slow) or trust async; we trust async for music.

NFS has historical issues with `rename(2)` being non-atomic across some Linux NFS client versions; in practice on modern Linux ≥ 5.x with NFSv4 this is a non-issue.

### Cross-FS rename guard

The writer asserts at startup that `library/.tmp/` and a sample `library/_provisional/` are on the same mount, by calling `fs.statSync(...).dev` on both and comparing. Mismatch = fail-fast with a descriptive error. Same check at composition root, not at every write call.

## Consequences

- **Positive**: Readers never see torn files. Crashes leave only `.partial` in `.tmp/`, which is cleaned up on next startup. Concurrency is gated by the DB, not by file-level locks (which are unreliable on NAS).
- **Negative / accepted trade-offs**: We do not handle the case where the NAS mount disappears mid-write (network blip). The write fails, the job goes to `failed`, the user re-requests. Could be smoothened with retry, deferred to v2.
- **Neutral**: The mount-uniformity invariant is a deployment constraint we must document for users.

## Alternatives considered

- **Write directly to final path then truncate-on-failure**: not atomic; readers would see partials.
- **File locks (`flock`, `fcntl`)**: unreliable on NFS / SMB. The DB row is a better lock substrate.
- **Linux `linkat(O_EXCL)`** to atomically create-if-not-exists: works on local FS, brittle on NAS. Stick with rename.
- **Move-then-fsync** (rename before fsync): violates POSIX guarantees; rename without prior fsync may persist a rename of empty/partial data.
- **Chunked writes with checksums and verify-after-write**: overkill for music; adds cost without addressing a real failure mode at our scale.

## References

- `man 2 rename` on POSIX atomicity.
- LWN: "The trouble with rename" (covers NFS and SMB semantics).
