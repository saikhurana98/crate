# Deferred decisions / future work

This file tracks decisions and features that were considered during the v1 architecture design and consciously deferred. Each entry includes what is needed to revisit it.

## 1. Resume of cancelled / interrupted downloads

- **Today**: cancellation aborts the in-flight stream and deletes the partial file. Service restart marks all in-flight jobs `failed { service_restart }`; clients re-request from scratch.
- **Why deferred**: low priority for the live-set use case (re-rip is fast); high implementation cost (range-request support varies wildly across sources, P2P resume semantics are gnarly).
- **What's needed**: a resumable-source capability bit, partial-file persistence with byte ranges in the DB, `If-Range`/`Range` plumbing through the source adapter contract, end-to-end test against at least two sources that support range requests.

## 2. Multi-instance daemon

- **Today**: single-process, optimistic concurrency. The DB row update is the source of truth for "who owns this job"; second daemon would race itself.
- **Why deferred**: not needed for one NAS, one DJ rig. Adding it costs a switch from optimistic to pessimistic locking, lease columns on every row, and a heartbeat / lease-extension protocol.
- **What's needed**: `claimed_by`, `claim_expires_at` columns on `jobs` and `job_groups`; lease-based UPDATE patterns; advisory NAS-level lock removal (currently single-instance enforced via `.lbmd/lock`); a story for split-brain.

## 3. HTTP companion endpoint for one-shot queries

- **Today**: WS only. Dashboards and cURL debugging require a WS client.
- **Why deferred**: WS covers all current use cases; the debug UI is sufficient for ad-hoc inspection.
- **What's needed**: a small Express / Hono server on the same `:8788` ops port (or alongside `:8787`), exposing `GET /v1/jobs/:id`, `GET /v1/groups/:id`, `GET /v1/jobs?status=...`. Reuse the same zod schemas.

## 4. Attempts-table retention policy

- **Today**: every attempt row kept forever. Cheap until the table reaches millions of rows.
- **Why deferred**: not yet a real concern; we don't know the access patterns of the analytics we'll want to run yet.
- **What's needed**: a retention rule (e.g., keep all `success` rows, delete others older than 90 days), implemented as a periodic job, with a backup-before-delete safety net. Or: archive deleted rows to a Parquet / JSONL file on NAS.

## 5. Cover-art re-fetch on higher-resolution availability

- **Today**: one-shot per `(mbid, source)`. Manual refresh via debug UI.
- **Why deferred**: marginal value; cover-art sources rarely upgrade resolution after initial publish.
- **What's needed**: opt-in periodic rescan of the cover-art chain for tracks whose stored art is below a threshold (e.g., < 1500×1500), with a "highest-res-wins" replace policy.

## 6. Production auth (when exposed beyond LAN)

- **Today**: zero auth, local network trusted.
- **Why deferred**: the deployment model is single-user, single-NAS, single-LAN.
- **What's needed**: pick a model (static API key per client, OAuth via existing IDP, mTLS); enforce on WS upgrade and on `:8788` if exposing metrics/health publicly. Encrypt transport (`wss://`, TLS terminator like Caddy in front).

## 7. Album-artist split-load (huge album rip use case)

- **Today**: each track downloads from a single source. Album cohesion biases all tracks to the same source post-track-1.
- **Why deferred**: most albums are < 1 hour total audio; the speedup from chunking each track across sources doesn't offset the coordination complexity.
- **What's needed**: per-track range coordination, source-side range support (most don't have it), interleaved write-and-verify, and a clear use case (long ambient mixes? boxed sets?).

## 8. Telegram bot UX hardening

- **Today**: Telegram adapters are envisioned but not designed in detail. Per-source concurrency lives in the adapter; rate-limit responses respected.
- **Why deferred**: each Telegram bot is a unique conversational protocol; we want to design after touching at least two real ones.
- **What's needed**: Telegram client session storage, account rotation, anti-abuse fingerprint avoidance, dry-run mode against a sandbox bot.

## 9. Catalog import / library reconciliation

- **Today**: assumes we own the entire library. No story for "I already have 5000 FLACs on disk, please scan them."
- **Why deferred**: scope creep beyond the acquisition role.
- **What's needed**: a separate `application/import-existing-library.ts` use case, scanner that reads tags + fingerprints existing files, populates `tracks` and `jobs` rows in `final-native` state.

## 10. Merge-all metadata resolver strategy

- **Today**: first-success only. ADR-0013 documents the alternate `merge-all` strategy as a port capability but ships no use of it.
- **Why deferred**: first-success covers initial use cases; merge-all introduces conflict-resolution complexity without a clear win.
- **What's needed**: per-field priority order, conflict logging, opt-in flag on `MetadataResolver` chain config.
