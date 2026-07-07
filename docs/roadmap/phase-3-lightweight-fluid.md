# Phase 3 — Lightweight & fluid

Goal: shallower dependency tree, smaller image, calmer network, UI that stays
fluid at 10× data and looks intentionally designed. No frameworks.

## 1. Dependency slimming (Python)

- Replace `typer` with plain `click` (4 subcommands, no rich output needed) —
  drops `rich`, `pygments`, `markdown-it-py`, `shellingham` (~9 MB, 4 tree
  levels). CLI surface identical (`crate download/serve/console/version`,
  `crate user add`).
- Evaluate `fastapi` → bare `starlette` for `ops_api` (pydantic stays for DTO
  validation): only if the diff stays mechanical; otherwise document and keep.
- `pyproject` `[project.optional-dependencies]`: `browser = [playwright]`,
  `telegram = [telethon]` so the core wheel is minimal.

## 2. Image split

- `Dockerfile` target `core`: python-slim + ffmpeg + yt-dlp + wheel — no
  Chromium/Xvfb/Bento4 (~250 MB). Target `full`: current everything (~630 MB).
- `release.yml`: build/push `crate:core` and `crate:full` (tag `latest` = full
  for compatibility). Compose on music-stack picks per box.

## 3. UI network discipline

- One `scheduler.ts`: single 1 s heartbeat decides which fetches are due
  (history 15 s, batches 3 s-when-visible, player 5 s-when-tab-active);
  **everything pauses on `document.hidden`**, resumes with immediate refresh.
- SSE frame gains `seq`; pollers skip when their data's `seq` unchanged.
- Kill the unconditional 1 Hz tick render for `uniform` tiers — animate the
  "next:" rotation with CSS only.

## 4. UI scale

- Virtualize History: fixed row height, render only the ~12 visible rows + 4
  overscan inside the 430 px viewport; total capped server-side at 500.
- Batch detail: paginate tracks 50 at a time (`/api/batches/{id}?offset=`),
  "show more" appends.
- Prune accumulators: `inflight` rows dropped on terminal SSE state,
  `enqueued` keys expire after 10 min, `batchDetails` LRU-capped at 8 batches.

## 5. Look & feel (the "great to look at" pass)

- Skeleton shimmer for every panel's first load (CSS only).
- Row lifecycle transitions: in-flight shimmer → 180 ms ease settle on
  done/failed; tier-win badge pop; upgraded-file pulse.
- Empty states with one-line hints (no bare "nothing here").
- Consistent focus rings + reduced-motion media query.
- Budget: bundle stays < 100 KB, zero runtime deps, CSS < 5 KB.

**Acceptance:** venv shrinks ≥ 15 MB and tree depth drops; `crate:core` ≤
260 MB; a hidden tab makes zero network calls; 500-row history scrolls at
60 fps (manual perf trace); Lighthouse perf ≥ 95 on the console.
