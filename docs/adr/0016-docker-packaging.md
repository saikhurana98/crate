# ADR-0016: Single-container Docker packaging

- **Status:** accepted
- **Date:** 2026-07-02
- **Deciders:** @saikhurana98

## Context

crate must ship as a Docker container so it can run next to Explo and a self-hosted music server on a home NAS/server. It is a single Node process ([ADR-0001](0001-typescript-node-pnpm.md)) but depends on several native binaries and a headless browser:

- `yt-dlp` (Tier 3), `ffmpeg` (transcode/probe), `fpcalc` (Chromaprint fingerprinting, [ADR-0005](0005-chromaprint-acoustid.md)).
- Playwright + Chromium for scraping Lucida/Monochrome ([ADR-0012](0012-playwright-browser-pool.md)).
- SQLite state ([ADR-0004](0004-sqlite-hand-rolled-migrations.md)) and an SMB/NFS-mounted NAS library ([ADR-0008](0008-nas-atomic-write.md)).

The container must expose three inbound surfaces: the slskd shim ([ADR-0014](0014-slskd-compat-shim.md)), the WebSocket API `:8787`, and ops/metrics `:8788`.

## Decision

**One image, one process, all runtime deps baked in.** No sidecar containers for ffmpeg/yt-dlp/Chromium.

- **Base:** `mcr.microsoft.com/playwright:v<pinned>-jammy` (Chromium + its system libs preinstalled and version-matched to the Playwright npm dep), or a Debian-slim base with `npx playwright install --with-deps chromium` — whichever keeps Chromium/Playwright versions locked together. Pin exact versions; no `latest`.
- **Multi-stage build:** stage 1 `pnpm install --frozen-lockfile` + `pnpm -r build`; stage 2 copies `dist/` + pruned prod `node_modules` + the native bins. `yt-dlp`/`ffmpeg`/`fpcalc` installed at pinned versions (apt or pinned release download, checksum-verified).
- **Ports exposed:** `2416` (slskd shim, default), `8787` (WebSocket), `8788` (ops/metrics).
- **Volumes:**
  - NAS library mount (SMB/NFS) — mounted by the host or via container mount; crate assumes a single mount point ([ADR-0008](0008-nas-atomic-write.md) single-mount invariant), so `.tmp` staging and final path are on the same filesystem for atomic rename.
  - Named volume for the SQLite DB + WAL (must **not** live on the NAS mount — SQLite + WAL over SMB/NFS is unsafe).
- **Config:** all via `CRATE_*` env, fail-fast at startup ([`.crate.env.example`](../../.crate.env.example) + [SECRETS.md](../../SECRETS.md)). Streaming creds (Qobuz/Spotify/Apple tokens) and `CRATE_SLSKD_SHIM_API_KEY` injected as env/secrets, never baked.
- **Runtime user:** non-root `crate` user; Chromium sandbox handled via the Playwright base image's recommended flags.
- **Healthcheck:** `HEALTHCHECK` hits the ops health endpoint on `:8788`.
- **Compose:** ship `docker-compose.yml` (crate alone) and `docker-compose.explo.yml` (crate + Explo wired: `SLSKD_URL=http://crate:2416`, shared `SLSKD_API_KEY`, `DOWNLOAD_SERVICES=slskd,youtube`, shared library volume). `.dockerignore` excludes `node_modules`, tests, docs.

## Consequences

- **Positive:** One `docker compose up` gives a working crate; the Explo compose file demonstrates the whole pipeline end-to-end. Version-locked Chromium avoids the classic Playwright/Chromium drift breakage. Deterministic builds via frozen lockfile + pinned bins.
- **Negative / accepted trade-offs:** Image is large (Chromium ≈ several hundred MB). Accepted — home-server target, not edge/serverless. Bundling all bins in one image couples their upgrade cadence to an crate release; accepted for operational simplicity over sidecar sprawl.
- **Neutral:** SQLite-off-NAS and single-mount constraints become documented deployment requirements (compose files encode them). ARM64 + amd64 multi-arch build is a follow-up, not v1.

## Alternatives considered

- **Sidecar containers** (separate ffmpeg/yt-dlp/browser services). More "microservice-pure" but adds IPC, network hops, and orchestration for binaries that are just CLI/library calls in-process. Rejected — over-engineered for a single-process app.
- **Distroless / Alpine base.** Smaller, but Chromium on musl (Alpine) and missing glibc/system libs on distroless make Playwright painful. The Playwright base image is the path of least resistance. Rejected for v1.
- **Host-installed binaries, thin image.** Smaller image but breaks "one command to run" and makes the environment non-reproducible across hosts. Rejected.

## References

- [ADR-0001](0001-typescript-node-pnpm.md), [ADR-0004](0004-sqlite-hand-rolled-migrations.md), [ADR-0008](0008-nas-atomic-write.md), [ADR-0012](0012-playwright-browser-pool.md)
- [ADR-0014](0014-slskd-compat-shim.md) — the shim port the container exposes to Explo.
