# ADR-0014: slskd-compatible REST shim for Explo integration

- **Status:** accepted
- **Date:** 2026-07-02
- **Deciders:** @saikhurana98

## Context

A new goal: make crate a drop-in downloader for [Explo](https://github.com/LumePart/Explo), a self-hosted "Discover Weekly" clone. Explo pulls weekly recommendations from ListenBrainz (or imported Spotify/Apple playlists), downloads each track, tags it, and builds a playlist on the music server (Jellyfin/Navidrome/Plex/Subsonic/Emby/MPD).

Explo acquires audio through `DOWNLOAD_SERVICES`, a **priority-ordered list of backends**. Upstream ships exactly two: `slskd` and `youtube`. Explo is written in Go; the backend set is compiled in. The `slskd` backend talks to a running [slskd](https://github.com/slskd/slskd) instance over its **REST API**, configured via `SLSKD_URL` + `SLSKD_API_KEY`, and filters candidates with `MIN_BITRATE` (default 256), `EXTENSIONS` (default `flac,mp3`), plus `TRACK_EXTENSION`, `MIGRATE_DOWNLOADS`, `USE_SUBDIRECTORY`.

We want crate's multi-source tier cascade (see [ADR-0015](0015-tier-cascade-scheduler.md)) to be the thing Explo actually downloads from, without forking Explo and without maintaining a Go patch against upstream.

Three integration strategies were evaluated:

- **A — slskd-compatible API shim.** crate exposes an HTTP server that speaks the subset of the slskd REST API that Explo drives. Explo points `SLSKD_URL` at crate and is none the wiser.
- **B — native Explo backend.** Fork Explo, add a first-class `crate` entry to `DOWNLOAD_SERVICES` in Go, call crate's own WebSocket/HTTP API. Cleaner semantics, but we own a fork forever.
- **C — directory handoff.** crate pre-populates the library; Explo runs local-only (`skip` mode). Loosest coupling, but duplicates ListenBrainz discovery and is brittle.

## Decision

**Strategy A. crate ships an inbound `slskd-shim` transport** — an HTTP server that emulates the slskd REST endpoints Explo calls, translating each into an crate tier-cascade download job. Explo sets `SLSKD_URL=http://crate:<port>`, `SLSKD_API_KEY=<shared-secret>`, `DOWNLOAD_SERVICES=slskd,youtube` (youtube stays as Explo's own last-ditch net).

### Endpoint mapping (the subset Explo drives)

| slskd endpoint | crate shim behaviour |
|---|---|
| `POST /api/v0/searches` `{searchText}` | Start an internal tier-cascade job keyed to `searchText`. Return `SearchState{ id, searchText, state: "InProgress", fileCount: 0, responseCount: 0 }` where `id` is the crate job `local_id` (ULID). |
| `GET /api/v0/searches/{id}` | Return current `SearchState`. Flip `state` to `"Completed"` and set `fileCount: 1, responseCount: 1` once the cascade has a **verified** candidate; keep `"InProgress"` while racing. |
| `GET /api/v0/searches/{id}/responses` | Return a **single synthetic `SearchResponseItem`**: `username: "crate"`, `hasFreeUploadSlot: true`, one `SearchFile` describing the resolved file (`filename`, `size`, `bitRate`, `bitDepth`, `sampleRate`, `length`, extension `.flac`). Values are the real resolved file's, so Explo's `MIN_BITRATE`/`EXTENSIONS` filters pass. |
| `POST /api/v0/transfers/downloads` `{username:"crate", files:[...]}` | Commit the download: run the cascade to completion, write the verified file into `DOWNLOAD_DIR` under Explo's expected layout (respect `USE_SUBDIRECTORY`, playlist name passed through). Return `201`. |
| `GET /api/v0/transfers/downloads/{username}` | Return a `Transfer` whose per-file `state` maps from crate quality state: `Queued` (pending) → `InProgress`/`Transferring` (racing) → `Completed` (verified + written) → `Failed` (all tiers exhausted). |

### Why the synthetic single-response works

The whole tier race (Squid/Lucida/Monochrome → Spotify/Apple → yt-dlp) runs **inside** crate. Explo never sees N Soulseek peers to pick between; it sees exactly one excellent result from user `crate`. This sidesteps having to fake per-peer queue positions and upload speeds, and lets crate's own scoring/verification pick the winner instead of Explo's naive bitrate filter.

### Progress deadline

Explo skips a track after **15 minutes of no download progress**. The shim must therefore surface monotonic progress (or fail fast) within that window: on `GET .../transfers/downloads/{username}` it reports the cascade's aggregate percent, and if the cascade cannot produce a verified file it returns `Failed` well before 15 min rather than hanging.

### Auth + transport

- Bearer/`X-API-Key` header check against `CRATE_SLSKD_SHIM_API_KEY`; reject mismatches `401`. Local-network only, but a shared secret keeps it honest and matches Explo's config shape.
- Separate port from the WebSocket API (`:8787`) and ops (`:8788`); default `CRATE_SLSKD_SHIM_PORT=2416`.
- The shim is a **thin inbound adapter**: it owns HTTP framing + slskd DTO mapping only, and delegates all work to the `download-cascade` application use-case ([ADR-0015](0015-tier-cascade-scheduler.md)) — the same use-case an interactive client would call. No business logic in the transport.

## Consequences

- **Positive:** Zero Explo fork. Works against upstream Explo as-is and survives its updates. crate becomes Explo's priority-0 service; Explo's `youtube` backend remains a genuine fallback if crate is down. The synthetic-response design keeps the shim small — no need to model Soulseek's peer/queue semantics.
- **Negative / accepted trade-offs:** We are pinned to slskd's REST **contract**, not just the published API — Explo may call endpoints or fields the docs omit, or a future Explo/slskd version may drift. Mitigation: the contract test for this adapter is seeded from **captured live Explo→slskd request traces**, not only the published schema, and drift is tracked in `WORK.md`. If the shim proves too lossy, Strategy B (native backend) is the documented escape hatch (`future-work.md`).
- **Neutral:** The shim introduces an inbound HTTP transport alongside the WebSocket API, so crate is no longer WebSocket-only. `ARCHITECTURE.md` §1 non-goals updated accordingly. `MIGRATE_DOWNLOADS`/`USE_SUBDIRECTORY` layout parity is a known follow-up.

## Alternatives considered

- **B — native Explo backend (fork).** Cleanest semantics and full control over the handoff, but a permanent Go fork to rebase against upstream Explo. Rejected for v1; kept as the escape hatch if the shim can't faithfully represent crate's richer state.
- **C — directory handoff (local-only Explo).** crate would have to independently pull the same ListenBrainz recommendations Explo resolves, duplicating discovery and racing Explo's own run. Brittle and redundant. Rejected.
- **Emulating full slskd peer semantics** (N responses, per-peer queue/speed). Unnecessary complexity — crate already picks the best source internally, so one synthetic response is strictly simpler and gives crate control of the winner.

## References

- Explo: <https://github.com/LumePart/Explo> · config wiki: <https://github.com/LumePart/Explo/wiki/4.-Configuration-Parameters>
- slskd REST API: <https://slskd-api.readthedocs.io/en/latest/api.html>
- [ADR-0015](0015-tier-cascade-scheduler.md) — the download engine the shim fronts.
- [ADR-0009](0009-websocket-headless-protocol.md) — the other inbound transport.
