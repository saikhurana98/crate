# Architecture — Load-Balanced Music Downloader (crate)

> Headless backbone service. Acquires high-quality audio (FLAC target) from a load-balanced pool of sources for live DJ sets and background catalog building. Exposes a WebSocket API. Stores files on NAS, tracks state in SQLite.

This document is the master architecture. Each significant decision has its own ADR under [`docs/adr/`](docs/adr/). Read this for the bird's-eye view; read the ADRs for context and alternatives considered.

> **⚠️ Pivot (2026-07-02):** the project pivoted to a **Python CLI, test-first** — see [ADR-0017](docs/adr/0017-pivot-python-cli-tdd.md). It supersedes the TypeScript/Node/pnpm, WebSocket-protocol, and debug-UI decisions. The tiered-cascade, verification, hexagonal, SQLite, NAS, and Explo-shim design below still holds; the language/transport/build-process details in this doc trail ADR-0017 until reworked. **ADR-0017 is authoritative on any conflict.**

---

## 1. Goals & non-goals

### Primary goals
- **Fast time-to-playable** during live sets: a crowd request becomes a playable file (≥320 kbps) in ≤10 s.
- **FLAC as endgoal**: every track eventually lands as 24-bit / 192 kHz FLAC where source allows; lossy is provisional.
- **Multi-source load balancing**: pool of HTTP, P2P (Soulseek), Telegram-bot, and stream-rip sources. Backbone is opaque to clients.
- **Bulk acquisition**: playlists and albums fan out under bounded concurrency; albums prefer source-cohesion.
- **Multiple metadata fetchers**: chained resolvers (MusicBrainz, Spotify, Apple, …) with first-success semantics; same model for cover art.
- **Test-first contracts**: every port has a contract test suite that any adapter must satisfy.
- **Explo compatibility (2026-07-02)**: act as a drop-in downloader for [Explo](https://github.com/LumePart/Explo) via an slskd-compatible REST shim, honouring an explicit Tier 1→2→3 quality cascade. See [ADR-0014](docs/adr/0014-slskd-compat-shim.md), [ADR-0015](docs/adr/0015-tier-cascade-scheduler.md), [ADR-0016](docs/adr/0016-docker-packaging.md).

### Non-goals (v1)
- No UI in this app. Clients connect over WebSocket; a second inbound transport (the slskd shim, [ADR-0014](docs/adr/0014-slskd-compat-shim.md)) exists solely for Explo — WebSocket is no longer the *only* inbound surface, but the shim adds no business logic.
- No multi-instance / horizontal scaling. Single Node process per NAS.
- No production-grade auth. Local network trusted.
- No resume of cancelled / interrupted downloads (deferred — see [`docs/adr/future-work.md`](docs/adr/future-work.md)).
- No background re-encoding / catalog migrations.

---

## 2. Bounded contexts

Three contexts, hard boundaries. Dependencies flow one way: **App → Resolution → Acquisition → Verification → Catalog**.

| Context | Responsibility | Key ports |
|---|---|---|
| **Resolution** | Turn raw input into a canonical `Track` + ranked candidates | `InputParser`, `MetadataResolver`, `BulkExpander` |
| **Acquisition** | Fetch bytes from sources, race-pick winners, write to disk | `SourceAdapter`, `Scheduler`, `FileWriter`, `BrowserSessionPool` |
| **Verification & Catalog** | Tag, fingerprint, decide acceptance, persist state | `AudioVerifier`, `CoverArtFetcher`, `TrackStore`, `Tagger` |

The composition root (`composition/container.ts`) wires concrete adapters into use-cases. Use-cases never import adapters or infrastructure directly.

```
WS request ──► use-case ──► port ──► adapter ──► (HTTP / Telegram / Playwright / sqlite / fs)
                              ▲
                              │ contract test ensures every adapter behaves identically
```

See [ADR-0002](docs/adr/0002-hexagonal-ports-adapters.md).

---

## 3. Module dependency graph

```
domain/         ◄── pure types, zero deps
  └─ result, error, track, job, quality, candidate

ports/          ◄── interfaces only, depends on domain/
  └─ source-adapter, metadata-resolver, audio-verifier,
     track-store, file-writer, input-parser, bulk-expander,
     cover-art-fetcher, browser-session, clock, logger

application/    ◄── use-cases, depends on domain + ports
  └─ download-for-live-set, download-bulk, upgrade-quality,
     verify-track, scheduler/{race, eligibility, health},
     resolver-chain, cover-art-chain, upgrade-daemon, purge-daemon

adapters/       ◄── implements ports, isolated by capability
  └─ sources/{onthespot, ytdlp-ytmusic, squid-qobuz, lucida,
              monochrome, soulseek}
     metadata/{musicbrainz, spotify, apple, ytmusic}
     fingerprint/{chromaprint-acoustid}
     parsers/{spotify-link, apple-link, ytmusic-link, free-text}
     bulk/{spotify, apple, ytmusic}
     cover-art/{coverartarchive, spotify-art, apple-art, lastfm-art}

infrastructure/ ◄── framework / IO concerns
  └─ sqlite/{store, migrations}
     nas/{writer}
     browser/{playwright-pool}
     ws/{server, protocol-validator}
     ops/{metrics, health, logger-pino}

composition/    ◄── DI wiring; the only place that touches all layers
  └─ container, config

index.ts        ◄── entry point: load config, build container, start daemons + WS
```

No backward edges. Lint rule (biome custom or `dependency-cruiser`) enforces.

---

## 4. Data flow per input type

### 4a. Spotify / Apple / YouTube Music **track** link
```
WS request.track {input: "https://open.spotify.com/track/..."}
        │
        ▼
InputParser.parse → kind: "track-link", ref: spotify:track:abc
        │
        ▼
ResolverChain.resolveByRef(ref) → Track (canonical, MBID-tagged where possible)
        │
        ▼
DownloadForLiveSet(track, mode) ──┬─► RipPipeline   (parallel)
                                  └─► EndgoalPipeline (parallel)
        │                              │
        │                              ▼
        │                        write provisional → emit job.playable, job.progress
        │                              │
        ▼                              ▼
  endgoal stream done → AudioVerifier (health + identity vs provisional fingerprint)
        │
        ▼
  decision = accept → atomic rename → emit job.upgraded → status `final`
                                       delete provisional
```

### 4b. Free-text query
```
WS request.track {input: "porter robinson sad machine"}
        │
        ▼
InputParser.parse → kind: "free-text", query
        │
        ▼
ResolverChain.search(query) → ReleaseCandidate[]
        │
        ├─ if 1 candidate, confidence ≥ 0.95           → straight to download (above flow)
        ├─ if multiple                                  → emit `disambiguate`, await client
        └─ if autoSelect="highest-confidence"           → pick top, download
```

### 4c. Playlist / album link
```
WS request.bulk {input: "https://open.spotify.com/playlist/..."}
        │
        ▼
InputParser.parse → kind: "playlist-link" | "album-link"
        │
        ▼
BulkExpander.expand(ref) → ExpandedGroup { kind, title, tracks[] }
        │
        ▼
DownloadBulk(group)
        │
        ▼
fan-out: for each track create Job, run pipeline (above)
         bounded by groupLimit (8 live-set / 3 background)
         album-cohesion: bias endgoal scheduler toward source that won track 1
```

---

## 5. Source adapter contract

Single port covers all six source backends — HTTP scrapers, Telegram bot wrappers, Soulseek, Spotify ripper, yt-dlp wrapper.

```ts
export interface SourceAdapter {
  readonly id: SourceId;
  readonly tier: "rip" | "endgoal";
  readonly capabilities: SourceCapabilities;

  search(ref: TrackRef, signal: AbortSignal): Promise<Result<SourceListing[], SourceError>>;
  probe(listing: SourceListing, signal: AbortSignal): Promise<Result<ProbeResult, SourceError>>;
  fetch(listing: SourceListing, signal: AbortSignal): Promise<Result<FetchHandle, SourceError>>;
}
```

**Three steps, separated:** search → probe → fetch.
- `search` returns one or more candidate listings on this source.
- `probe` is cheap (HEAD-equivalent / Telegram message check) — used by scheduler for pre-filter.
- `fetch` returns a `ReadableStream<Uint8Array>` with `AbortSignal` honoured for race-cancel.

**Tagged `SourceError`:** `not_found | rate_limited | auth_required | quality_unavailable | transport | cancelled`. Scheduler keys eligibility off `tag`.

**Per-source concurrency** lives inside the adapter (`p-limit` instance, configurable). Scheduler stays dumb.

**Telegram / Playwright requirements** are declared via `capabilities.requires` so the composition root injects a `TelegramSession` or `BrowserSessionPool` only into adapters that need them.

See [ADR-0002](docs/adr/0002-hexagonal-ports-adapters.md) and [`docs/adr/0012-playwright-browser-pool.md`](docs/adr/0012-playwright-browser-pool.md).

---

## 6. Two-tier scheduler

Live DJ sets demand fast time-to-playable. FLAC fetches are slow. Run two pipelines per request, in parallel.

```
crowd request
     │
     ├─► RIP TIER (latency-optimised)
     │      sources: onthespot, ytdlp-ytmusic
     │      strategy: pure parallel fetch, first-complete wins, cancel losers
     │      output: ≥320 kbps lossy file → live-incoming/<job-id>.<ext>
     │      emits: job.playable
     │
     └─► ENDGOAL TIER (quality-optimised)
            sources: squid-qobuz, lucida, monochrome, soulseek
            strategy: pure parallel fetch race (bandwidth not a concern),
                      first-byte wins, cancel losers
            output: FLAC → library/.tmp/ → atomic rename to library/<Artist>/<Album>/<Track>.flac
            verify: fingerprint vs provisional; health-check; fake-FLAC detector
            emits: job.upgraded → status `final`
```

**Bandwidth is free (per requirements)** — pure race. Other adapters' streams aborted on first winner.

**Per-source concurrency caps** still apply (telegram-bot rate-limits, Spotify session caps).

**Eligibility filter** runs synchronously before every race:
- `capabilities.maxQuality >= req.minAcceptableQuality`
- `source_health.cooldown_until` not in future
- `source_health.success_rate >= 0.3` (rolling)
- `capabilities.supportsLink` includes input link kind, OR `capabilities.acceptsCanonical`

See [ADR-0003](docs/adr/0003-two-tier-scheduler.md).

---

## 7. Bulk download (playlists & albums)

`JobGroup` aggregates `Job`s. `BulkExpander` returns ordered `TrackRef[]` from a playlist/album URL.

| Mode | groupLimit (parallel jobs) | Album cohesion |
|---|---|---|
| live-set | 8 | enabled for `kind=album` |
| background | 3 | enabled for `kind=album` |

**Album cohesion**: first track's winning endgoal source becomes `group.preferred_source`. Subsequent tracks add +30 score bonus to that source — same mastering, gapless playback intact.

**Failure isolation**: one track failure doesn't block the group. `group.completed` fires when every job has terminal status (final / final-native / permanent-provisional / all-failed).

**Manifest**: playlists generate an m3u8 at `library/_playlists/<title>__<id>.m3u8`. Provisional tracks live at `library/_provisional/<job-id>.<ext>`; on upgrade, file moves and m3u8 is rewritten in the same DB transaction.

---

## 8. Quality state machine

```
                  ┌─────────┐
   request ─────► │ pending │
                  └────┬────┘
              rip ✓    │     rip ✗ AND endgoal ✓ first
       ┌──────────────┼──────────────────┐
       ▼              ▼                  ▼
  ┌─────────────┐  (skipped)             │
  │ provisional │                        │
  └──────┬──────┘                        ▼
         │ endgoal ✓ + verify accept   ┌──────────────┐
         ▼                             │ final-native │
  ┌─────────────┐                      └──────────────┘
  │   final     │
  └─────────────┘
         ▲
         │ upgrade-daemon (rescan permanent-provisional via cooldown ladder)
  ┌──────┴──────┐
  │  permanent- │ ◄── endgoal exhausted (5 attempts: 15m,1h,6h,24h,7d)
  │  provisional│
  └─────────────┘

   review-pending ◄── verification decision = "review" (background mode, 0.75 ≤ similarity < 0.95)
   all-failed     ◄── rip ✗ AND endgoal ✗  → DJ-facing "unavailable"
```

**Idempotency**: two endgoal candidates landing within ms — first to acquire DB row lock wins. SQL: `UPDATE jobs SET status='final', ... WHERE id=? AND status='provisional' RETURNING id`. Loser sees 0 rows updated, deletes its tmp file, exits clean. (Optimistic locking — single-process assumption, see [ADR-0006](docs/adr/0006-result-type-hand-rolled.md) and parking-lot.)

**`live-set` mode** bypasses cooldown for the **first** endgoal attempt; cooldown applies only to daemon-driven retries.

See [ADR-0003](docs/adr/0003-two-tier-scheduler.md) for the rip+endgoal model.

---

## 9. Verification

Two distinct checks, both run on every endgoal arrival except where opted out.

| Check | What it answers | When skipped |
|---|---|---|
| **Identity** (chromaprint similarity vs provisional fingerprint) | "Is this the same track?" | First arrival (no provisional); trusted source (config) |
| **Health** (FLAC decode test) | "Is this file usable?" | Trusted source skip (config); else never |
| **Fake-FLAC** (spectrum cutoff < threshold ⇒ upscaled lossy) | "Is this really FLAC?" | Skipped for `costClass: rate-limited` (paid mirrors don't fake) |

**Threshold function** (`application/verification/decide.ts`):

```
live-set mode:    similarity ≥ 0.95 → accept; else reject
background mode:  ≥ 0.95 → accept; ≥ 0.75 → review; else reject
```

`review` decision parks file in `library/.review/<job-id>-<source-id>.flac` with sidecar `.review.json`. WS event `job.review-pending` surfaces to verbose subscribers. Auto-reject after 24 h (configurable).

See [ADR-0005](docs/adr/0005-chromaprint-acoustid.md).

---

## 10. WebSocket protocol

Single endpoint: `ws://host:8787/v1/stream`. JSON messages, validated with zod at the boundary, tagged unions both directions.

**Versioned envelope** on every server message: `{ v: 1, ...payload }`. Additive evolution within `v: 1`; breaking changes bump major.

**Opaque-by-default progress** — internals (tier, sources, races, retries) hidden. Clients subscribe with optional `verbose: true` for stats-for-nerds detail events.

**Subscription scopes**: `{ kind: "job"|"group"|"all", verbose?: boolean }`. Group-verbose inherits to child jobs.

**Replay**: server keeps last 200 detail events per active job in a ring buffer (~20 KB/job). Client toggling `verbose: false → true` mid-session replays from buffer head.

**Backpressure**: when `ws.bufferedAmount > 8 MB`, drop `*.progress` events for that client; lifecycle events (`playable`, `upgraded`, `failed`) never dropped.

**Auth**: none in v1; trusted-network assumption.

See [ADR-0009](docs/adr/0009-websocket-headless-protocol.md) and [ADR-0010](docs/adr/0010-opaque-progress-stats-for-nerds.md). Full message schema in [`contracts/ws-protocol.schema.json`](contracts/ws-protocol.schema.json).

### Strict failure enums

```
not_found | all_sources_failed | verification_failed | service_restart |
user_cancelled | rate_limited_global | fingerprint_mismatch_strict |
network_unreachable | unsupported_input
```

Closed set; client maps to human-readable strings via `FAILURE_MESSAGES` registry. Adding a reason = contract bump.

---

## 11. Multi-resolver chain (metadata + cover art)

Two ordered chains, both first-success by default:

| Chain | Default order | Purpose |
|---|---|---|
| Metadata | `[musicbrainz, spotify, apple, ytmusic]` | Canonical track identity, MBID, ISRC, album/disc/track numbers |
| Cover art | `[coverartarchive, spotify-art, apple-art, lastfm-art]` | Album artwork, ≥1000×1000 preferred (smaller rejected, try next) |

**AcoustID** acts as a fallback MBID resolver: if MusicBrainz direct lookup fails, post the track's chromaprint to AcoustID, get back MBID candidates.

**Tracks without MBID**: allowed. `tracks.mbid` nullable; synthetic ULID `tracks.local_id` always present, used as canonical key when MBID absent. Covers DJ edits, bootlegs, very-recent releases.

See [ADR-0013](docs/adr/0013-multi-resolver-chain.md).

---

## 12. Storage

### NAS layout

```
NAS_ROOT/
├── library/
│   ├── _provisional/<job-id>.<ext>          # lossy, awaiting upgrade
│   ├── _playlists/<title>__<id>.m3u8        # generated manifests
│   ├── _cover-art/<mbid>__<source>.jpg      # cached artwork
│   ├── .tmp/<job-id>-<source>.partial       # in-flight downloads
│   ├── .review/<job-id>-<source>.flac       # review-pending
│   └── <Artist>/<Album>/01 - <Title>.flac
├── live-incoming/<job-id>.<ext>             # ephemeral DJ-set rips, purged after upgrade or 7d
└── .crate/
    ├── catalog.db (+ -wal, -shm)
    └── lock                                  # advisory single-process lock
```

### Atomic write

```
1. Stream into library/.tmp/<job-id>-<source>.partial  (open with O_EXCL)
2. fsync(file)
3. fs.rename(tmp, finalPath)  — atomic on same mount
4. fsyncDir(parent)
```

`.tmp/` and final dir always on the same NAS mount → rename is atomic; **never** put `.tmp/` on a different mount, that degrades to copy+delete and re-introduces races. SMB & NFS caveats documented in [ADR-0008](docs/adr/0008-nas-atomic-write.md).

### SQLite

WAL mode, `synchronous=NORMAL`, `foreign_keys=ON`, mmap 256 MB, busy_timeout 5 s.

Hand-rolled migration runner reading numbered `.sql` files from `infrastructure/sqlite/migrations/`. No ORM. Raw SQL repository pattern. Future option: `kysely` for query-builder ergonomics.

#### Schema (summary; full SQL in `infrastructure/sqlite/migrations/001_init.sql`)

| Table | Purpose | Key columns |
|---|---|---|
| `tracks` | Canonical identity | `mbid` PK (nullable), `local_id` always set, `fingerprint`, `isrc` |
| `external_refs` | Spotify/Apple/YT → MBID mapping | `ref` PK, `service`, `mbid` FK |
| `job_groups` | Playlists/albums | `id`, `kind`, `external_ref`, `mode`, `preferred_source` |
| `jobs` | One per acquisition | `id`, `mbid` FK, `status`, `provisional_*`, `final_*`, `endgoal_attempts`, `next_retry_at`, `live_set_request` |
| `attempts` | Per-source try log | `job_id` FK, `source_id`, `tier`, `outcome`, `duration_ms`, `bytes_received` |
| `source_health` | EWMA per source | `source_id` PK, `ewma_latency_ms`, `success_rate`, `cooldown_until` |
| `cover_art` | Cached artwork per (mbid, source) | composite PK |
| `schema_migrations` | Numeric versioning | `version` PK, `applied_at` |

Indexes: `idx_jobs_status_retry` on `(status, next_retry_at)` partial for sweeps, `idx_jobs_mbid_active`, `idx_attempts_source_recent` for health derivation.

See [ADR-0004](docs/adr/0004-sqlite-hand-rolled-migrations.md).

---

## 13. Failure modes & retry policy

### Failure taxonomy (closed enum, see §10)

Per-attempt outcomes (`attempts.outcome`): `success | rate_limited | auth_required | quality_unavailable | transport_error | timeout | cancelled_lost_race | cancelled_user | cancelled_shutdown | fingerprint_mismatch | fake_flac_detected | health_check_failed | source_unavailable`.

Per-job final reasons surfaced to clients: see §10.

### Retry policy

Three classes:

1. **Per-source cooldown (in-attempt)**: 1 transport error = no cooldown; 2 consecutive = 30 s; 3+ exponential to 2 h cap. `rate_limited` outcome respects adapter-reported `retryAfterMs`.
2. **Per-job retry (background daemon)**: ladder `15 m → 1 h → 6 h → 24 h → 7 d`, then `permanent-provisional` (rescan every 30 d).
3. **Group-level retry**: none implicit. Failed children re-enter per-job retry independently; client can re-request group.

### Cancellation

- **User cancel**: aborts in-flight via `AbortController`, deletes provisional, emits failure. **For groups**: cancels `pending` and in-flight only; jobs at `provisional` are preserved (DJ may want playable file). Endgoal pursuit halted.
- **Lost-race**: loser must abort within 100 ms of winner's first chunk (test contract).
- **Shutdown (SIGTERM)**: 10 s grace. Near-done rip allowed to complete; endgoal aborted; remaining marked `service_restart`. WAL checkpoint, clean DB close.

### Crash recovery

Clean-slate on startup. No magic resume. Pending jobs marked `service_restart`; stale `.partial` files unlinked; `live-incoming/` purged where stale; source health restored from DB. Resume capability deferred ([`docs/adr/future-work.md`](docs/adr/future-work.md)).

---

## 14. Observability

Three layers, all opt-in (zero-cost-when-off where possible).

### Structured logs (always on)

`pino` JSON to stderr. `CRATE_LOG_LEVEL=debug|info|warn|error` (default `info`). Every log line carries `evt`, `request_id`, `job_id|group_id|source_id` where applicable, `duration_ms` for span events. Syslog captures.

### Metrics (Prometheus exposition)

`GET :8788/metrics`. Counters, gauges, histograms keyed by `source_id`, `tier`, `outcome`. Histogram buckets `[0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120]` s — DJ-relevant range. Bound to `0.0.0.0` (local-network trusted).

### Tracing (OpenTelemetry, optional)

If `OTEL_EXPORTER_OTLP_ENDPOINT` set, init OTel SDK. Spans: `request.handle → input.parse → resolver.chain → pipeline.{rip,endgoal} → attempt.<source> → verify → write.atomic`. Disabled by default.

### Health endpoints

- `GET :8788/healthz` → 200 always (process alive)
- `GET :8788/readyz` → 200 only if DB open + WAL active + NAS root writable + ≥1 healthy adapter per tier; else 503 with JSON of failed checks
- `GET :8788/version` → build info

---

## 15. Configuration

Single file `crate.config.json` loaded at startup, validated with zod, fail-fast on invalid. Source overrides nested under `sources.<id>.*`. Example in [`config/crate.config.example.json`](config/crate.config.example.json).

Top-level keys: `ws`, `ops`, `nas`, `db`, `log`, `browser`, `concurrency`, `scheduler`, `verification`, `retry`, `purge`, `resolvers`, `sources`.

---

## 16. Repository layout (monorepo, pnpm workspaces)

```
load-balanced-music-downloader/
├── pnpm-workspace.yaml
├── package.json                     # workspace root
├── tsconfig.base.json
├── biome.json
├── ARCHITECTURE.md                  # this document
├── core-design.md                   # original requirements (preserved)
├── contracts/
│   └── ws-protocol.schema.json      # source of truth for WS messages
├── docs/
│   └── adr/                         # architecture decision records
├── packages/
│   └── core/                        # @crate/core — the headless service
│       ├── package.json
│       ├── tsconfig.json
│       ├── vitest.config.ts
│       ├── src/
│       │   ├── domain/
│       │   ├── ports/
│       │   ├── application/
│       │   ├── adapters/
│       │   ├── infrastructure/
│       │   ├── composition/
│       │   ├── protocol/            # zod schemas for WS messages, CI-validated against contracts/
│       │   └── index.ts
│       └── tests/                   # mirrors src/, contract tests + use-case tests
└── apps/
    └── debug-ui/                    # @crate/debug-ui — independent, ZERO imports from core
        ├── package.json
        ├── vite.config.ts
        ├── index.html
        ├── src/
        │   ├── main.ts              # solid-js
        │   ├── ws-client.ts         # uses its OWN protocol types (CI-validated against contracts/)
        │   ├── components/
        │   └── styles.css
        └── tests/
```

**Decoupling**: debug-ui has zero imports from `@crate/core`. Both packages independently validate their `protocol/messages.ts` against `contracts/ws-protocol.schema.json` in CI. Drift = build fail. See [ADR-0011](docs/adr/0011-monorepo-decoupled-debug-ui.md).

---

## 17. Stack constraints

**Non-negotiable** (locked in ADRs):

- TypeScript strict (`strict`, `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`)
- Test-first with vitest; every port has a contract test suite that any adapter must pass
- Hexagonal / ports-and-adapters (ADR-0002)
- zod for runtime validation at every boundary (ADR-0007)
- Hand-rolled `Result<T, E>` (ADR-0006)
- Explicit DI via constructor injection; no decorators, no reflect-metadata
- biome for lint+format
- pnpm workspaces

**Concurrency primitives**: native `AbortController` + `Promise.race` + `p-limit` per source. No `effect` / RxJS — keep it boring and standard.

**Browser automation**: Playwright with a managed pool (ADR-0012); not all adapters need it.

---

## 18. Test strategy

- **Domain**: pure unit tests, zero mocks.
- **Ports**: each port has a contract test suite (`tests/ports/<port>.contract.ts`) parametrised over every adapter. New adapter = wire its factory into the suite, run, fix failures.
- **Application (use-cases)**: tests pin behaviour against in-memory adapter fakes (live in `tests/fakes/`).
- **Adapters**: integration tests guarded by env (e.g., `CRATE_TEST_LIVE_SOURCES=1`); skipped in CI by default.
- **Infrastructure**: SQLite tests use temp file DB; NAS writer tests use temp dir; Playwright pool tests use a real headless Chromium.

CI gate: `pnpm typecheck && pnpm lint && pnpm test`. All must pass; tests should fail loudly with "not implemented" assertions in the initial scaffold (test-first).

---

## 19. Deferred / future work

Captured in [`docs/adr/future-work.md`](docs/adr/future-work.md):

- Resume of cancelled / interrupted downloads (range requests, partial-stream replay)
- Multi-instance daemon (requires switch optimistic → pessimistic locking)
- HTTP companion endpoint for one-shot queries
- Attempts-table retention policy
- Cover-art re-fetch on higher-res availability
- Production auth (when exposed beyond LAN)
- Album-artist split-load (huge album rip use case)

---

## 20. ADR index

| # | Title |
|---|---|
| [0001](docs/adr/0001-typescript-node-pnpm.md) | Language, runtime, package manager: TypeScript + Node + pnpm |
| [0002](docs/adr/0002-hexagonal-ports-adapters.md) | Hexagonal / ports-and-adapters layout |
| [0003](docs/adr/0003-two-tier-scheduler.md) | Two-tier scheduler: rip + endgoal pipelines in parallel |
| [0004](docs/adr/0004-sqlite-hand-rolled-migrations.md) | SQLite with hand-rolled migrations; no ORM |
| [0005](docs/adr/0005-chromaprint-acoustid.md) | Chromaprint + AcoustID for fingerprinting and MBID fallback |
| [0006](docs/adr/0006-result-type-hand-rolled.md) | Hand-rolled `Result<T, E>` for typed errors at module boundaries |
| [0007](docs/adr/0007-zod-runtime-validation.md) | zod for runtime validation at every boundary |
| [0008](docs/adr/0008-nas-atomic-write.md) | NAS atomic-write strategy: temp file + rename, SMB/NFS caveats |
| [0009](docs/adr/0009-websocket-headless-protocol.md) | WebSocket as the single client protocol |
| [0010](docs/adr/0010-opaque-progress-stats-for-nerds.md) | Opaque progress by default, stats-for-nerds opt-in |
| [0011](docs/adr/0011-monorepo-decoupled-debug-ui.md) | Monorepo with decoupled debug UI |
| [0012](docs/adr/0012-playwright-browser-pool.md) | Playwright browser pool as a managed resource |
| [0013](docs/adr/0013-multi-resolver-chain.md) | Multiple metadata resolvers chained, first-success |
| [0014](docs/adr/0014-slskd-compat-shim.md) | slskd-compatible REST shim for Explo integration |
| [0015](docs/adr/0015-tier-cascade-scheduler.md) | Tier-cascade scheduler (Explo / batch mode) |
| [0016](docs/adr/0016-docker-packaging.md) | Single-container Docker packaging |
| [future](docs/adr/future-work.md) | Deferred decisions / parking lot |
