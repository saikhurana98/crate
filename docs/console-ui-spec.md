# crate Console — UI Specification

A self-contained brief for building the crate management console UI. No prior
context required. Backend endpoints and data shapes are given verbatim so the UI
can bind to them directly.

---

## 1. Product context

**crate** is a self-hosted, tiered music downloader. When something requests a
track (usually the discovery service **Explo**, sometimes a manual CLI call), crate
runs a **tier cascade**: it tries Tier 1 sources first, then Tier 2, then Tier 3,
and the first source to produce a verified file wins. Each tier is a *pool* of
swappable **source modules** with a fan-out **strategy**.

- **Tier 1** — best quality (hi-res FLAC): `qobuz`, `monochrome`
- **Tier 2** — good quality (FLAC / lossless-ish): `deezer`, `soulseek`, `telegram`
- **Tier 3** — fallback (lossy): `ytdlp` (always present, last resort)

The **console** is the operator's window into this: see what's downloading and
what has been downloaded, how each track was acquired, how sources are
performing, and — the headline feature — **configure the sources and arrange them
into tiers with drag-and-drop**.

## 2. Goals

1. **Observe** — live queue, full historical record, per-source statistics.
2. **Understand each download** — for any track: which sources were tried, in
   what order, which won, and the resulting audio quality.
3. **Manage** — enable/disable sources, move them between tiers, reorder them,
   set each tier's fan-out strategy, and edit each source's settings and
   credentials. **Drag-and-drop is the primary arrangement interaction.**

## 3. Design & tech constraints

- **Stack:** vanilla **HTML + TypeScript**, built with **Vite**. **No UI
  framework** (no React/Vue/Svelte). State is plain TS; DOM is built directly.
  A tiny drag-and-drop helper is fine — native HTML5 DnD API or a small library
  like SortableJS. No heavy component kits.
- **Self-contained:** the built output is static assets served by the backend.
  All CSS/JS bundled; no external CDNs, fonts, or network calls except the crate
  API (same-origin `/api/*`).
- **Theme:** dark, information-dense but calm. Current palette (keep or refine):
  `--bg:#0e1116` `--panel:#171b22` `--panel-2:#1e232c` `--border:#2a313c`
  `--text:#e6e9ef` `--muted:#8b95a5` `--accent:#6ea8fe` `--good:#4ade80`
  `--warn:#fbbf24` `--bad:#f87171`. System font stack.
- **Responsive:** primarily desktop (operator dashboard) but must not break on a
  tablet/narrow window — panels reflow, tables scroll horizontally inside their
  own container, the drag-drop board stacks its columns.
- **Accessibility:** keyboard-operable drag-drop alternative (e.g. move buttons or
  a keyboard reorder mode), visible focus states, ARIA on interactive controls,
  sufficient contrast.
- **Auth is handled by the reverse proxy** — the UI makes same-origin `/api/*`
  calls and does **not** manage tokens or a login screen.

## 4. Information architecture

Single-page app, four top-level tabs (hash routes):

1. **Overview** — `#/overview` — stats + source/tier health at a glance
2. **Live** — `#/live` — real-time queue of in-flight downloads
3. **History** — `#/history` — searchable log of every request + drill-in detail
4. **Sources & Tiers** — `#/sources` — the drag-and-drop configuration board

Persistent top bar: product name (`crate console`) + the four tabs. Active tab
highlighted.

---

## 5. Screen specs

### 5.1 Overview

Purpose: answer "is the system healthy and productive?" in one glance.

- **Stat cards** (row of 6): Requests (total), Success rate (%), In flight,
  Lossless (%), Downloaded (total bytes), Avg time (s). Each card: big value,
  label, optional sub-line (e.g. "4 done · 1 failed").
- **Panels** (grid, reflow):
  - **Wins by source** — horizontal bars, source → count.
  - **Wins by tier** — bars, Tier 1/2/3 → count.
  - **Source reliability** — bars, source → % of attempts that succeeded.
  - **Sources & tiers** — compact table: source, tier, quality (FLAC/any),
    credential status badge. (A read-only mirror of the config board.)
- States: loading skeleton; empty ("No downloads yet — the console fills as Explo
  runs.").

Binds to: `GET /api/stats`, `GET /api/sources`.

### 5.2 Live

Purpose: watch downloads happen in real time.

- **Connection indicator** — dot + "live"/"reconnecting…" driven by the SSE
  stream; a compact one-line summary (total · success rate · in-flight count).
- **In-flight table** — one row per active download: query (with an animated
  spinner), where it came from (`explo`/`cli`), how long it's been running.
- Empty state: "Nothing downloading right now."
- Updates arrive via **Server-Sent Events** (`EventSource`) — no manual polling.

Binds to: `GET /api/events` (SSE; see §8).

> Future (nice-to-have, not required Phase 1): show per-source progress within a
> single download. The current backend reports request-level state only.

### 5.3 History

Purpose: the permanent record — every requested song and exactly how it was
downloaded.

- **Toolbar:** search box (debounced, filters by query text), status filter
  (All / Done / Failed / Downloading), prev/next pagination.
- **Table:** query · status badge · won (e.g. "T1 · qobuz") · quality
  (e.g. "FLAC" or "lossy opus 160 kbps") · size · when (relative time). Rows are
  clickable.
- **Detail drawer** (slides in from the right on row click):
  - Header: query, source (`explo`/`cli`), status, time.
  - **Cascade trail** — the ordered list of every source attempt with ✓/✗ and a
    detail string (quality label on success, error message on failure). This is
    the core "how it was downloaded" story.
  - **Winner facts** — file path, quality label, codec + bit-depth + sample rate,
    size, duration, elapsed download time. For failures: the error. For in-flight:
    "In progress."
- States: loading, empty ("No requests match.").

Binds to: `GET /api/requests`, `GET /api/requests/{id}`.

### 5.4 Sources & Tiers — the drag-and-drop board (headline feature)

Purpose: arrange which sources live in which tier, in what order, with what
strategy — visually, by dragging.

**Layout:** a board of **four columns**:

- **Tier 1**, **Tier 2**, **Tier 3** — each a droppable column holding an ordered
  list of **source cards**.
- **Available / Disabled** — a tray of source cards that are not currently in any
  tier.

**Source card** shows:
- Source name (`qobuz`, `deezer`, …) + a small type/quality badge (`FLAC` if it
  only accepts lossless, `any` otherwise).
- **Credential status** dot/badge: `ok` (green), `partial` (amber), `missing`
  (red), `none` (no credentials needed — e.g. `monochrome`, `ytdlp`).
- A **gear/⋯ affordance** opening the source's config panel (§5.5).
- A **drag handle**.

**Interactions:**
- **Drag a card between columns** → changes its tier (or disables it if dropped in
  the tray).
- **Drag within a column** → reorders. Order matters: for the `uniform` strategy
  it's the round-robin sequence; visually it also communicates priority.
- **Column header** shows the tier name and a **strategy toggle**:
  - **All (broadcast)** — try every source in the tier; first verified file wins.
    Maximises coverage.
  - **Round-robin (uniform)** — spread requests across sources one at a time to
    reduce per-source rate-limit/ban risk; falls through on failure.
- **Constraints to enforce in the UI:**
  - `ytdlp` is the **pinned final fallback** — it stays in Tier 3 and cannot be
    disabled or moved out (show it as locked; drag disabled). Other sources move
    freely, including into/out of the disabled tray.
  - A source with `credentials: "missing"` may be placed but should show a clear
    "won't run until configured" warning.
- **Persistence UX:** either **auto-save on drop** (optimistic, with a toast +
  rollback on error) or an explicit **Save / Discard** bar that appears when the
  layout is dirty. Prefer auto-save with a subtle "saved" confirmation; show
  errors inline. After a successful save the backend hot-reloads the cascade — no
  restart — so surface a small "applied" confirmation.
- **Empty/again states:** dragging shows a drop-placeholder in the target column;
  invalid drops (e.g. onto pinned `ytdlp`'s tier when it would violate a rule) are
  rejected with a subtle shake/return animation.

Binds to: `GET /api/config` (current layout), `PUT /api/config` (save layout),
`GET /api/sources` (card metadata + credential status). See §8 for shapes.

### 5.5 Source config panel

Opened from a source card's gear. A side panel or modal for one source.

- **Header:** source name, tier, quality type.
- **Enable toggle** (redundant with tray placement; keep in sync).
- **Settings** — source-specific fields (see the per-source list in §8.4). Example:
  Soulseek has a "minimum bitrate" number; a tier's strategy is set on the board,
  not here.
- **Credentials** — the fields each source needs (e.g. Qobuz: App ID, App Secret,
  Token; Deezer: ARL). **Never display stored secret values** — show only status
  ("configured" / "missing") and masked inputs to set/replace. Saving credentials
  writes them **encrypted at rest** server-side.
- **Validate / Test** (nice-to-have): a button that checks the credentials against
  the source and reports ok/fail.
- States: dirty/save/saving/saved/error.

Binds to: `GET /api/sources` (status), `PUT /api/sources/{name}` (settings +
credentials). See §8.5.

---

## 6. Shared components / design system

- **Cards, panels** — rounded (12px), `--panel` bg, `--border` 1px.
- **Bars** — simple horizontal bar (label / track / value); no charting library.
- **Badges** — pill; variants `good` / `warn` / `bad` / `muted` for statuses.
- **Status dot** — on/off/neutral.
- **Table** — dense, hover highlight for clickable rows, header in `--muted`.
- **Drawer** — right-side slide-in, ~460px, dim/scrim optional, close button.
- **Toolbar controls** — search input, select, buttons (`--panel-2` bg).
- **Toast/inline confirmations** — for save/apply/errors on the config board.
- **Spinner** — small CSS spinner for in-flight rows and saving states.

## 7. Global states

Every data view must handle: **loading** (skeleton or "Loading…"), **empty**
(friendly, tells the operator why it's empty), **error** (the message, retry
affordance), and for Live, **disconnected** (reconnecting indicator). The config
board additionally has **dirty**, **saving**, **saved**, and **conflict/error**.

---

## 8. API contract (bind targets)

Same-origin JSON under `/api`. Auth header is injected by the proxy; the UI sends
plain `fetch`. **Read endpoints (§8.1–8.4) exist today.** Config write endpoints
(§8.5) are the Phase-2 backend work that will be built to match this spec — the UI
can be built against these shapes.

### 8.1 `GET /api/stats`
```jsonc
{
  "total": 6, "done": 4, "failed": 1, "in_flight": 1,
  "success_rate": 0.8,          // 0..1
  "lossless": 3, "lossless_rate": 0.75,
  "total_bytes": 197000000, "avg_elapsed_s": 5.1,
  "wins_by_source": { "qobuz": 2, "deezer": 1, "ytdlp": 1 },
  "wins_by_tier":   { "1": 2, "2": 1, "3": 1 },
  "attempts_by_source": { "qobuz": { "ok": 2, "total": 3 }, "monochrome": { "ok": 0, "total": 1 } }
}
```

### 8.2 `GET /api/requests?status=&q=&limit=&offset=`
`status` ∈ {`done`,`failed`,`downloading`} (optional); `q` substring on query;
`limit` 1..500 (default 50); `offset` ≥ 0. Returns an array, newest first:
```jsonc
[
  {
    "id": "5f…", "query": "Daft Punk Get Lucky", "requested_via": "explo",
    "status": "done", "tier_won": 1, "source_won": "qobuz",
    "error": null, "created_at": "2026-07-04T18:00:00Z", "finished_at": "2026-07-04T18:00:05Z",
    "quality_label": "lossless 24-bit / 96 kHz flac", "size_bytes": 94000000,
    "lossless": 1, "elapsed_s": 5.1
  }
]
```

### 8.3 `GET /api/requests/{id}`
Full record + attempt trail + winning download:
```jsonc
{
  "id": "5f…", "query": "Daft Punk Get Lucky", "requested_via": "explo",
  "status": "done", "tier_won": 1, "source_won": "qobuz", "error": null,
  "created_at": "…", "finished_at": "…",
  "attempts": [
    { "id": 1, "source": "monochrome", "tier": 1, "ok": 0, "detail": "Turnstile JWT required", "created_at": "…" },
    { "id": 2, "source": "qobuz",      "tier": 1, "ok": 1, "detail": "lossless 24-bit / 96 kHz flac", "created_at": "…" }
  ],
  "download": {
    "path": "/nas/qobuz.flac", "size_bytes": 94000000, "codec": "flac",
    "bitrate": 1800000, "bit_depth": 24, "sample_rate": 96000,
    "duration_s": 231.0, "elapsed_s": 5.1, "lossless": 1,
    "quality_label": "lossless 24-bit / 96 kHz flac"
  }
}
```
`download` is `null` for failed/in-flight requests. `404` if id unknown.

### 8.4 `GET /api/sources`
One record per source (drives cards + the read-only sources table). Reports
credential **status only — never values**:
```jsonc
[
  {
    "name": "qobuz", "tier": 1, "enabled": true, "lossless": true,
    "credentials": "ok",            // ok | partial | missing | none
    "missing_env": [],
    "required_env": ["CRATE_QOBUZ_APP_ID","CRATE_QOBUZ_APP_SECRET","CRATE_QOBUZ_TOKEN"]
  },
  { "name": "monochrome", "tier": 1, "enabled": false, "lossless": true, "credentials": "none", "missing_env": [], "required_env": [] },
  { "name": "deezer",     "tier": 2, "enabled": true,  "lossless": true, "credentials": "ok", "missing_env": [], "required_env": ["CRATE_DEEZER_ARL"] },
  { "name": "soulseek",   "tier": 2, "enabled": true,  "lossless": false, "credentials": "ok", "missing_env": [], "required_env": ["CRATE_SLSKD_URL","CRATE_SLSKD_API_KEY","CRATE_SLSKD_DOWNLOADS"] },
  { "name": "telegram",   "tier": 2, "enabled": false, "lossless": false, "credentials": "missing", "missing_env": ["CRATE_TG_API_ID","CRATE_TG_API_HASH","CRATE_TG_SESSION","CRATE_TG_BOT"], "required_env": ["CRATE_TG_API_ID","CRATE_TG_API_HASH","CRATE_TG_SESSION","CRATE_TG_BOT"] },
  { "name": "ytdlp",      "tier": 3, "enabled": true,  "lossless": false, "credentials": "none", "missing_env": [], "required_env": [] }
]
```

### 8.5 Config endpoints (Phase 2 — to be built to this shape)

**`GET /api/config`** — the current tier layout:
```jsonc
{
  "tiers": [
    { "tier": 1, "strategy": "all",     "sources": ["qobuz", "monochrome"] },
    { "tier": 2, "strategy": "uniform", "sources": ["deezer", "soulseek"] },
    { "tier": 3, "strategy": "all",     "sources": ["ytdlp"] }
  ],
  "disabled": ["telegram"]
}
```
`strategy` ∈ {`all`,`uniform`}. Order within `sources` is significant.

**`PUT /api/config`** — save a new layout (same shape). Server validates
(e.g. `ytdlp` must remain in Tier 3), persists, and **hot-reloads the cascade**.
Response: the applied config (echo) or `{ "error": "…" }` with a 4xx.

**`PUT /api/sources/{name}`** — per-source settings + credentials:
```jsonc
{
  "enabled": true,
  "settings": { "min_bitrate_kbps": 256 },     // source-specific; see below
  "credentials": { "CRATE_QOBUZ_TOKEN": "…" }   // write-only; stored encrypted; omit to leave unchanged
}
```
Response reflects updated status via the `GET /api/sources` shape (never echoes
secret values).

**Per-source settings the panels expose** (beyond credentials):
- `qobuz` — credentials only (App ID, App Secret, Token).
- `monochrome` — no credentials (browser-based); enable toggle only.
- `deezer` — credential: ARL.
- `soulseek` — credentials: slskd URL, API key, downloads dir; setting:
  `min_bitrate_kbps`.
- `telegram` — credentials: API ID, API hash, session, bot handle.
- `ytdlp` — none; pinned/always-on.

---

## 9. What exists vs what to build

- **Built (Phase 1, live):** Overview, Live, History (with detail drawer), the
  read API (§8.1–8.4), and a working-but-plain implementation of those three
  screens in vanilla TS. Treat the existing screens as a **functional baseline to
  redesign/polish**, not a constraint.
- **To build (this brief):**
  1. **Sources & Tiers drag-and-drop board** (§5.4) — the new headline screen.
  2. **Source config panels** (§5.5) — settings + credential editing.
  3. A **visual redesign pass** across all four screens to a cohesive, polished
     system.
  The config endpoints (§8.5) will be implemented on the backend to match; build
  the UI against these shapes.

## 10. Out of scope

- Auth / login (handled by the proxy).
- User accounts, multi-tenant, roles.
- Editing the music library or triggering discovery (that's Explo's job); a
  manual "request a track" box is a possible future addition, not required here.
- Per-byte download progress (backend reports request-level state only for now).
