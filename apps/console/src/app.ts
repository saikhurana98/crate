// crate console — single-screen app (cascade map + listening/live/wins/history
// rail + detail drawer + source config panel). Ported from the v4 design, wired
// to the real /api. No framework: one state object, reconciling re-render, event
// delegation. See docs/console-ui-spec.md.
import "./styles.css";
import {
  api,
  type Batch,
  type BatchDetail,
  type FeedRow,
  type Layout,
  type LiveEvent,
  type Me,
  type PlayerState,
  type Playlist,
  type PlayerTrack,
  type RequestDetail,
  type RequestRow,
  type ReviewCandidate,
  type ReviewItem,
  type ReviewStats,
  type Source,
  type SpotifyTrack,
  type TierCfg,
} from "./api";
import { bytes, dur, ago as relTime, pct, esc } from "./format";
import { createRenderer, effect } from "./reactive";

// Monolithic sage-on-black palette. Tier hierarchy = brightness steps of one hue.
const C = {
  good: "#e9ede3",
  warn: "#99a091",
  bad: "#ff4c38",
  accent: "#e9ede3",
  t1: "#e9ede3",
  t2: "#aeb5a3",
  t3: "#7c8371",
  muted: "#99a091",
  faint: "#5d6456",
  dim: "#464d40",
  text: "#e9ede3",
  red: "#ff4c38",
  light: "#dfe4d6",
  ink: "#14170f",
};

// Attention/warning accent for the fingerprint gate (needs_confirm + wrong-version
// chips). Reuses the file's existing softer coral (#ff6b58, already used for failure
// text) rather than inventing a new palette — a warmer "amber" that stays in-system
// and reads distinct from the hard-alarm red (#ff4c38).
// Amber/yellow "needs attention" — deliberately distinct from the failed red so
// CONFIRM never reads as FAILED.
const WARN = "#f2c14e";
const WARN_BG = "rgba(242,193,78,.10)";
const WARN_BD = "rgba(242,193,78,.42)";

// Per-source config panel definition: [envKey, label][] + non-secret settings.
interface PanelDef {
  creds: [string, string][];
  settings: [string, string, string, string][]; // key,label,type,default
  note?: string;
  validate?: boolean;
  pinned?: boolean;
}
const PANELDEF: Record<string, PanelDef> = {
  qobuz: {
    creds: [
      ["CRATE_QOBUZ_APP_ID", "App ID"],
      ["CRATE_QOBUZ_APP_SECRET", "App Secret"],
      ["CRATE_QOBUZ_TOKEN", "Token"],
    ],
    settings: [],
    validate: true,
  },
  monochrome: {
    creds: [],
    settings: [],
    note: "Browser-based source — no credentials required. Enable it and crate drives a headless session.",
  },
  deezer: { creds: [["CRATE_DEEZER_ARL", "ARL cookie"]], settings: [], validate: true },
  soulseek: {
    creds: [
      ["CRATE_SLSKD_URL", "slskd URL"],
      ["CRATE_SLSKD_API_KEY", "API key"],
      ["CRATE_SLSKD_DOWNLOADS", "Downloads dir"],
    ],
    settings: [["min_bitrate_kbps", "Minimum bitrate (kbps)", "number", "256"]],
    validate: true,
  },
  telegram: {
    creds: [
      ["CRATE_TG_API_ID", "API ID"],
      ["CRATE_TG_API_HASH", "API hash"],
      ["CRATE_TG_SESSION", "Session string"],
      ["CRATE_TG_BOT", "Bot handle"],
    ],
    settings: [],
    validate: true,
  },
  ytdlp: {
    creds: [],
    settings: [],
    note: "Pinned final fallback — always enabled, cannot be moved or disabled. No credentials needed.",
    pinned: true,
  },
};

type SaveState = "idle" | "saving" | "saved";

interface State {
  tick: number;
  stats: LiveEvent["stats"] | null;
  sources: Record<string, Source>;
  layout: Layout | null;
  history: RequestRow[];
  inflight: RequestRow[];
  histSearch: string;
  histStatus: string;
  selectedId: string | null;
  detail: RequestDetail | null;
  showLogs: boolean;
  panelSource: string | null;
  panelValues: Record<string, string>;
  panelSave: "idle" | "dirty" | "saving" | "saved";
  panelValidate: Record<string, { status: "testing" | "ok" | "fail"; detail: string }>; // source -> cred-test result
  panelTestQuery: string; // optional track to force through the source instead of the known test song
  drag: { source: string | null; overTier: string | null; overIndex: number | null };
  rejectShake: string | null;
  saveState: SaveState;
  toast: string | null;
  error: string | null;
  searchQuery: string;
  searchResults: SpotifyTrack[];
  searchBusy: boolean;
  searchErr: string | null;
  searchOff: boolean; // /api/search returned 503 — Spotify creds absent
  // Direct cross-source search dialog (the "not on Spotify?" fallback)
  srcOpen: boolean;
  srcQuery: string;
  srcBusy: boolean;
  srcResults: ReviewCandidate[] | null;
  srcErr: string | null;
  srcQueued: Record<string, boolean>; // candidate id -> queued
  searchFocus: boolean; // dropdown open only while the header field has focus
  enqueued: Record<string, boolean>; // track id -> already sent to the cascade
  spotifyConfigured: boolean; // /api/spotify/status.configured (user-OAuth set up)
  spotifyConnected: boolean; // refresh token stored -> Listening panel is live
  player: PlayerState | null; // now-playing + recently-played
  importOpen: boolean; // "Import from Spotify" section expanded
  importPlaylists: Playlist[] | null; // the user's playlists (lazy-loaded on open)
  importBusy: Record<string, boolean>; // "liked" | playlist id -> import in flight
  importErr: string | null;
  synced: Record<string, boolean>; // spotify playlist id -> continuously synced (Phase D)
  syncBusy: Record<string, boolean>; // playlist id -> sync toggle in flight
  feed: FeedRow[]; // shared activity feed (Phase C) — who requested what + latencies
  isMobile: boolean; // viewport <= 640px -> tabbed app layout
  activeTab: "cascade" | "activity" | "tracks"; // mobile bottom-nav selection
  page: "console" | "bulk" | "review" | "feed"; // top-level page
  bulkPanel: boolean; // right-side bulk drawer open
  bulkText: string; // paste box (playlist link / track list)
  bulkBusy: boolean; // submit in flight
  batches: Batch[]; // bulk batches (polled)
  expandedBatches: Record<string, boolean>; // batch id -> show track rows
  batchDetails: Record<string, BatchDetail>; // fetched track rows for expanded batches
  // review tab (user-assisted retry / re-download)
  reviewItems: ReviewItem[]; // failed + lossy items (polled while on the page)
  reviewStats: ReviewStats | null; // the 4 stat-card figures
  reviewOpen: Record<string, boolean>; // item id -> expanded
  reviewQ: Record<string, string>; // item id -> edited query
  reviewSearching: Record<string, boolean>; // item id -> search in flight
  reviewCands: Record<string, ReviewCandidate[]>; // item id -> found candidates
  reviewSel: Record<string, string>; // item id -> selected candidate id
  reviewDownloading: Record<string, boolean>; // item id -> re-download posted
  reviewIdentifying: Record<string, boolean>; // item id -> AcoustID identify in flight
  reviewHint: Record<string, { artist: string | null; title: string | null; mbid: string | null }>; // item id -> fingerprint hint (key present => identify ran)
  reviewLastCand: Record<string, ReviewCandidate>; // item id -> candidate last chosen (for a force re-send)
  reviewDetail: Record<string, RequestDetail>; // item id -> full provenance (origin + identity + attempt trail), fetched on expand
  me: Me | null; // the logged-in user (Login with Spotify), or null in single-user mode
  authRequired: boolean; // true when /api/me 401s => render the login gate instead of the app
}

const state: State = {
  tick: 0,
  stats: null,
  sources: {},
  layout: null,
  history: [],
  inflight: [],
  histSearch: "",
  histStatus: "all",
  selectedId: null,
  detail: null,
  showLogs: false,
  panelSource: null,
  panelValues: {},
  panelSave: "idle",
  panelValidate: {},
  panelTestQuery: "",
  drag: { source: null, overTier: null, overIndex: null },
  rejectShake: null,
  saveState: "idle",
  toast: null,
  error: null,
  searchQuery: "",
  searchResults: [],
  searchBusy: false,
  searchErr: null,
  searchOff: false,
  srcOpen: false,
  srcQuery: "",
  srcBusy: false,
  srcResults: null,
  srcErr: null,
  srcQueued: {},
  searchFocus: false,
  enqueued: {},
  spotifyConfigured: false,
  spotifyConnected: false,
  player: null,
  importOpen: false,
  importPlaylists: null,
  importBusy: {},
  importErr: null,
  synced: {},
  syncBusy: {},
  feed: [],
  isMobile: typeof matchMedia !== "undefined" ? matchMedia("(max-width: 640px)").matches : false,
  activeTab: "cascade",
  page: "console",
  bulkPanel: false,
  bulkText: "",
  bulkBusy: false,
  batches: [],
  expandedBatches: {},
  batchDetails: {},
  reviewItems: [],
  reviewStats: null,
  reviewOpen: {},
  reviewQ: {},
  reviewSearching: {},
  reviewCands: {},
  reviewSel: {},
  reviewDownloading: {},
  reviewIdentifying: {},
  reviewHint: {},
  reviewLastCand: {},
  reviewDetail: {},
  me: null,
  authRequired: false,
};

const root = document.getElementById("app")!;
let saveTimer: number | undefined;
let toastTimer: number | undefined;
let shakeTimer: number | undefined;
let histTimer: number | undefined;
let searchTimer: number | undefined;
let blurTimer: number | undefined;
let searchAbort: AbortController | null = null;

// ---------- helpers ----------
const tierColor = (t: number | null): string =>
  t === 1 ? C.t1 : t === 2 ? C.t2 : t === 3 ? C.t3 : C.faint;

function credMeta(c: string): { color: string; text: string } {
  if (c === "ok") return { color: C.good, text: "ready" };
  if (c === "partial") return { color: C.warn, text: "partial" };
  if (c === "missing") return { color: C.bad, text: "missing" };
  return { color: C.faint, text: "no auth" };
}
function statusMeta(s: string): { label: string; color: string; bg: string } {
  if (s === "done") return { label: "Done", color: C.good, bg: "rgba(233,237,227,.1)" };
  if (s === "failed") return { label: "Failed", color: C.bad, bg: "rgba(255,76,56,.1)" };
  if (s === "queued") return { label: "Queued", color: C.warn, bg: "rgba(153,160,145,.12)" };
  return { label: "Downloading", color: C.good, bg: "rgba(233,237,227,.1)" };
}
const badgeText = (loss: boolean): string => (loss ? "FLAC" : "ANY");
function badgeStyle(loss: boolean): string {
  return loss
    ? "background:rgba(233,237,227,.09);color:#e9ede3;border:1px solid transparent"
    : "background:transparent;color:#7c8371;border:1px solid #272c25";
}
function findTrack(id: string): SpotifyTrack | PlayerTrack | undefined {
  return (
    state.searchResults.find((t) => t.id === id) ||
    (state.player?.now_playing?.id === id ? state.player.now_playing : undefined) ||
    state.player?.recent.find((t) => t.id === id)
  );
}
// Prefer the structured identity ("Artist — Title") for a request row; fall back
// to the raw query when the new columns are absent (legacy rows).
function rowLabel(r: RequestRow): string {
  const artist = (r.artist ?? "").trim();
  const title = (r.title ?? "").trim();
  if (title) return artist ? `${artist} — ${title}` : title;
  return r.query;
}

// ---------- data ----------
async function loadStatic(): Promise<void> {
  const [sources, layout] = await Promise.all([api.sources(), api.config()]);
  state.sources = Object.fromEntries(sources.map((s) => [s.name, s]));
  state.layout = layout;
}
async function loadHistory(): Promise<void> {
  const p = new URLSearchParams({ limit: "60" });
  if (state.histSearch) p.set("q", state.histSearch);
  if (state.histStatus !== "all") p.set("status", state.histStatus);
  state.history = await api.requests(p);
}
function connectLive(): void {
  const es = new EventSource(api.eventsUrl);
  es.onmessage = (ev) => {
    try {
      const e = JSON.parse(ev.data) as LiveEvent;
      state.stats = e.stats;
      state.inflight = e.in_flight;
      if (e.feed) state.feed = e.feed;
      render();
      // a re-download finishing changes the review list — refresh it on the page
      if (state.page === "review") void loadReview().then(render);
    } catch {
      /* ignore */
    }
  };
}
async function loadSpotifyStatus(): Promise<void> {
  try {
    const st = await api.spotifyStatus();
    state.spotifyConfigured = st.configured;
    state.spotifyConnected = st.connected;
    if (st.connected) await loadPlayer();
  } catch {
    state.spotifyConfigured = false;
  }
}
async function loadPlayer(): Promise<void> {
  if (!state.spotifyConnected) return;
  try {
    state.player = await api.spotifyPlayer();
  } catch {
    /* 409 not connected / transient — leave the last snapshot */
  }
}

// ---------- library import ----------
async function toggleImport(): Promise<void> {
  state.importOpen = !state.importOpen;
  render();
  if (state.importOpen && state.importPlaylists === null) {
    state.importErr = null;
    try {
      const [playlists, synced] = await Promise.all([api.libraryPlaylists(), api.syncList()]);
      state.importPlaylists = playlists;
      state.synced = Object.fromEntries(
        synced.filter((s) => s.active).map((s) => [s.spotify_ref, true]),
      );
    } catch (err) {
      state.importErr = (err as Error).message;
    }
    render();
  }
}
// Toggle continuous sync for a playlist — subscribe (future additions auto-queue)
// or pause. Optimistic; reverts on failure.
async function toggleSync(pid: string, name: string): Promise<void> {
  if (state.syncBusy[pid]) return;
  const on = !state.synced[pid];
  state.syncBusy = { ...state.syncBusy, [pid]: true };
  state.synced = { ...state.synced, [pid]: on };
  render();
  try {
    if (on) {
      await api.syncAdd(pid, name);
      toast(`Syncing “${name}” — new tracks will queue automatically`);
    } else {
      await api.syncRemove(pid);
      toast(`Stopped syncing “${name}”`);
    }
  } catch (err) {
    state.synced = { ...state.synced, [pid]: !on }; // revert
    toast(`Sync failed: ${(err as Error).message}`);
  } finally {
    state.syncBusy = { ...state.syncBusy, [pid]: false };
    render();
  }
}
async function runImport(source: "liked" | "playlist", key: string, pid?: string, name?: string): Promise<void> {
  if (state.importBusy[key]) return;
  state.importBusy = { ...state.importBusy, [key]: true };
  render();
  try {
    const res = await api.importLibrary({ source, playlist_id: pid, name });
    toast(`Importing “${res.name}” — ${res.total} track${res.total === 1 ? "" : "s"} queued`);
    void loadBatches().then(render);
  } catch (err) {
    toast(`Import failed: ${(err as Error).message}`);
  } finally {
    state.importBusy = { ...state.importBusy, [key]: false };
    render();
  }
}

// ---------- review tab ----------
async function loadReview(): Promise<void> {
  try {
    const resp = await api.review();
    state.reviewItems = resp.items;
    state.reviewStats = resp.stats;
    // drop transient per-item state for items that resolved and left the list
    const live = new Set(resp.items.map((i) => i.id));
    for (const map of [
      state.reviewDownloading,
      state.reviewOpen,
      state.reviewCands,
      state.reviewSel,
      state.reviewQ,
      state.reviewSearching,
      state.reviewIdentifying,
      state.reviewHint,
      state.reviewLastCand,
      state.reviewDetail,
    ] as Record<string, unknown>[]) {
      for (const id of Object.keys(map)) if (!live.has(id)) delete map[id];
    }
  } catch {
    /* transient — keep the last snapshot */
  }
}

// Fetch the full provenance (origin + search identity + attempt chronology) the
// first time a review item is expanded, so a resolve decision has full context.
async function loadReviewDetail(id: string): Promise<void> {
  if (state.reviewDetail[id]) return;
  try {
    state.reviewDetail[id] = await api.request(id);
    render();
  } catch {
    /* transient — the block just stays collapsed to identity-only */
  }
}

async function rescanReview(id: string): Promise<void> {
  const item = state.reviewItems.find((i) => i.id === id);
  if (!item) return;
  const q = state.reviewQ[id] != null ? state.reviewQ[id] : item.query;
  state.reviewSearching[id] = true;
  render();
  try {
    state.reviewCands[id] = await api.reviewSearch(id, q);
  } catch (err) {
    toast(`Search failed: ${(err as Error).message}`);
  }
  state.reviewSearching[id] = false;
  render();
}

async function downloadCand(id: string): Promise<void> {
  const cands = state.reviewCands[id] ?? [];
  const cand = cands.find((c) => c.id === state.reviewSel[id]);
  if (!cand) return;
  stopClip(); // stop the audition once the pick is committed
  state.reviewLastCand[id] = cand; // remember it so a needs_confirm item can force-resend
  state.reviewDownloading[id] = true;
  render();
  try {
    await api.reviewDownload(id, cand);
    toast(`Re-download queued: ${state.reviewItems.find((i) => i.id === id)?.query ?? ""} · ${cand.source}`);
    void loadReview().then(render);
  } catch (err) {
    state.reviewDownloading[id] = false;
    toast(`Re-download failed: ${(err as Error).message}`);
  }
  render();
}

async function dismissReview(id: string): Promise<void> {
  const item = state.reviewItems.find((i) => i.id === id);
  try {
    await api.reviewDismiss(id);
    toast(item && item.kind === "lossy" ? "Kept current file" : "Dismissed");
    await loadReview();
  } catch (err) {
    toast(`Dismiss failed: ${(err as Error).message}`);
  }
  render();
}

// Advisory fingerprint identify (AcoustID). Result cached on the item row too, but
// a freshly returned hint is preferred; {} => "no fingerprint match".
async function identifyReview(id: string): Promise<void> {
  state.reviewIdentifying[id] = true;
  render();
  try {
    const hint = await api.reviewIdentify(id);
    state.reviewHint[id] = {
      artist: hint.canonical_artist ?? null,
      title: hint.canonical_title ?? null,
      mbid: hint.mbid ?? null,
    };
  } catch (err) {
    toast(`Identify failed: ${(err as Error).message}`);
  }
  state.reviewIdentifying[id] = false;
  render();
}

// "Replace anyway" on a needs_confirm item — re-send the last chosen candidate with
// force=true so the fingerprint gate is overridden.
async function replaceAnyway(id: string): Promise<void> {
  // Uses the candidate stored server-side (the one that failed the gate), so this
  // works even after a reload — no client-side candidate required.
  stopClip(); // stop the audition once the pick is committed
  state.reviewDownloading[id] = true;
  render();
  try {
    await api.reviewConfirm(id);
    toast(`Replacing anyway: ${state.reviewItems.find((i) => i.id === id)?.query ?? ""}`);
    void loadReview().then(render);
  } catch (err) {
    state.reviewDownloading[id] = false;
    toast(`Replace failed: ${(err as Error).message}`);
  }
  render();
}

// ---------- sample playback (one shared <audio> for the whole page) ----------
// A single element => starting any clip pauses whatever else was sounding. Playback
// state lives in module vars; audio events schedule a re-render so the scrubber +
// eq-bar track the element.
let audioEl: HTMLAudioElement | null = null;
let playKey: string | null = null; // which row is loaded, e.g. `cur:<id>` / `cand:<id>:<cid>`
let playPos = 0; // current time (s)
let playDur = 0; // duration (s)
let playPaused = false;

function ensureAudio(): HTMLAudioElement {
  if (audioEl) return audioEl;
  const a = new Audio();
  a.preload = "metadata";
  a.addEventListener("timeupdate", () => {
    playPos = a.currentTime;
    render();
  });
  const setDur = () => {
    playDur = isFinite(a.duration) ? a.duration : 0;
    render();
  };
  a.addEventListener("loadedmetadata", setDur);
  a.addEventListener("durationchange", setDur);
  a.addEventListener("play", () => {
    playPaused = false;
    render();
  });
  a.addEventListener("pause", () => {
    playPaused = true;
    render();
  });
  a.addEventListener("ended", () => {
    playKey = null;
    playPos = 0;
    render();
  });
  a.addEventListener("error", () => {
    playKey = null;
    render();
  });
  audioEl = a;
  return a;
}

function toggleClip(key: string, url: string): void {
  const a = ensureAudio();
  if (playKey === key) {
    if (a.paused) void a.play().catch(() => undefined);
    else a.pause();
    return;
  }
  playKey = key;
  playPos = 0;
  playDur = 0;
  playPaused = false;
  a.src = url;
  void a.play().catch(() => undefined); // stream/format errors surface via the error handler
  render();
}

function seekClip(key: string, frac: number): void {
  const a = ensureAudio();
  if (playKey !== key || !isFinite(a.duration) || a.duration <= 0) return;
  a.currentTime = Math.max(0, Math.min(1, frac)) * a.duration;
  playPos = a.currentTime;
  render();
}

// Halt whatever clip is sounding — called when a re-download is requested so the
// audition doesn't keep playing after the decision is made.
function stopClip(): void {
  if (!audioEl) return;
  audioEl.pause();
  playKey = null;
  playPos = 0;
  playPaused = true;
}

// ---------- bulk downloads ----------
async function loadBatches(): Promise<void> {
  try {
    state.batches = await api.batches();
    // refresh the track rows of any expanded batch
    await Promise.all(
      Object.keys(state.expandedBatches)
        .filter((id) => state.expandedBatches[id])
        .map(async (id) => {
          try {
            state.batchDetails[id] = await api.batch(id);
          } catch {
            /* batch gone */
          }
        }),
    );
  } catch {
    /* transient */
  }
}
async function submitBulk(): Promise<void> {
  const text = state.bulkText.trim();
  if (!text || state.bulkBusy) return;
  state.bulkBusy = true;
  render();
  try {
    const res = await api.bulk(text);
    state.bulkText = "";
    toast(`Batch “${res.name}” — ${res.total} track${res.total === 1 ? "" : "s"} queued`);
    await loadBatches();
    state.page = "bulk";
    state.bulkPanel = false;
  } catch (err) {
    toast(`Bulk failed: ${(err as Error).message}`);
  } finally {
    state.bulkBusy = false;
    render();
  }
}
async function toggleBatch(id: string): Promise<void> {
  const open = !state.expandedBatches[id];
  state.expandedBatches[id] = open;
  render();
  if (open && !state.batchDetails[id]) {
    try {
      state.batchDetails[id] = await api.batch(id);
      render();
    } catch {
      /* ignore */
    }
  }
}
async function batchAction(fn: () => Promise<unknown>): Promise<void> {
  try {
    await fn();
    await loadBatches();
  } catch (err) {
    toast(`Failed: ${(err as Error).message}`);
  }
  render();
}

// ---------- search + enqueue ----------
async function runSearch(q: string): Promise<void> {
  searchAbort?.abort();
  if (q.length < 2) {
    state.searchResults = [];
    state.searchBusy = false;
    render();
    return;
  }
  const ctrl = new AbortController();
  searchAbort = ctrl;
  state.searchBusy = true;
  state.searchErr = null;
  render();
  // Speculatively warm the cross-source cache for this query, so if the track
  // isn't on Spotify and the user opens the dialog, results are already ready.
  api.prewarmSources(q);
  try {
    const results = await api.search(q, ctrl.signal);
    if (ctrl.signal.aborted) return; // a newer keystroke superseded this one
    state.searchResults = results;
    state.searchBusy = false;
    render();
  } catch (err) {
    if (ctrl.signal.aborted) return;
    state.searchBusy = false;
    if ((err as Error).message.startsWith("503")) {
      state.searchOff = true;
    } else {
      state.searchErr = (err as Error).message;
    }
    render();
  }
}
async function enqueueTrack(track: SpotifyTrack): Promise<void> {
  if (state.enqueued[track.id]) return;
  state.enqueued[track.id] = true;
  render();
  try {
    // thread the structured identity so the cascade can match on ISRC/duration
    await api.enqueue({
      query: track.query,
      artist: track.artist || null,
      title: track.title || null,
      isrc: track.isrc ?? null,
      duration_s: track.duration_s ?? null,
    });
    toast(`Queued: ${track.title}`);
    void loadHistory().then(render); // surface the new queued row immediately
  } catch (err) {
    state.enqueued[track.id] = false;
    toast(`Couldn't queue: ${(err as Error).message}`);
    render();
  }
}

// ---------- direct cross-source search ("not on Spotify?" dialog) ----------
let srcTimer: number | undefined;
function openSourceDialog(seed?: string): void {
  state.srcOpen = true;
  state.srcQuery = (seed ?? state.searchQuery).trim();
  state.srcResults = null;
  state.srcErr = null;
  state.srcQueued = {};
  state.searchFocus = false; // close the Spotify dropdown behind it
  render();
  if (state.srcQuery.length >= 2) void runSourceSearch();
}
function closeSourceDialog(): void {
  state.srcOpen = false;
  render();
}
async function runSourceSearch(): Promise<void> {
  const q = state.srcQuery.trim();
  if (q.length < 2) return;
  state.srcBusy = true;
  state.srcErr = null;
  render();
  try {
    state.srcResults = await api.searchSources(q);
  } catch (err) {
    state.srcErr = (err as Error).message;
    state.srcResults = null;
  } finally {
    state.srcBusy = false;
    render();
  }
}
async function downloadSourceCandidate(id: string): Promise<void> {
  const c = state.srcResults?.find((x) => x.id === id);
  if (!c || state.srcQueued[id]) return;
  state.srcQueued = { ...state.srcQueued, [id]: true };
  render();
  try {
    await api.downloadSourceCandidate(state.srcQuery.trim(), c);
    toast(`Queued “${c.title}” from ${c.source}`);
    void loadHistory().then(render);
  } catch (err) {
    state.srcQueued = { ...state.srcQueued, [id]: false };
    toast(`Couldn't queue: ${(err as Error).message}`);
  } finally {
    render();
  }
}

// ---------- save flow ----------
function toast(msg: string): void {
  window.clearTimeout(toastTimer);
  state.toast = msg;
  toastTimer = window.setTimeout(() => {
    state.toast = null;
    render();
  }, 2600);
}
async function persistLayout(next: { tiers: TierCfg[]; disabled: string[] }): Promise<void> {
  const prev = state.layout;
  window.clearTimeout(saveTimer);
  state.saveState = "saving";
  render();
  try {
    state.layout = await api.putConfig(next);
    state.sources = Object.fromEntries((await api.sources()).map((s) => [s.name, s]));
    state.saveState = "saved";
    toast("Applied — cascade hot-reloaded");
    saveTimer = window.setTimeout(() => {
      state.saveState = "idle";
      render();
    }, 1800);
  } catch (err) {
    state.layout = prev; // rollback
    state.saveState = "idle";
    toast(`Save failed: ${(err as Error).message}`);
  }
  render();
}
function reject(name: string): void {
  window.clearTimeout(shakeTimer);
  state.rejectShake = name;
  shakeTimer = window.setTimeout(() => {
    state.rejectShake = null;
    render();
  }, 480);
  render();
}

function moveSource(name: string, targetKey: string, index: number | null): void {
  if (!state.layout) return;
  const cfg: { tiers: TierCfg[]; disabled: string[] } = JSON.parse(
    JSON.stringify({ tiers: state.layout.tiers, disabled: state.layout.disabled }),
  );
  let origKey: string | null = null;
  let origIdx = -1;
  cfg.tiers.forEach((t) => {
    const i = t.sources.indexOf(name);
    if (i >= 0) {
      origKey = String(t.tier);
      origIdx = i;
    }
  });
  const di = cfg.disabled.indexOf(name);
  if (di >= 0) {
    origKey = "disabled";
    origIdx = di;
  }
  cfg.tiers.forEach((t) => (t.sources = t.sources.filter((s) => s !== name)));
  cfg.disabled = cfg.disabled.filter((s) => s !== name);
  let idx = index == null || !isFinite(index) ? Infinity : index;
  if (origKey === String(targetKey) && origIdx >= 0 && origIdx < idx) idx -= 1;
  if (targetKey === "disabled") {
    cfg.disabled.splice(Math.min(idx, cfg.disabled.length), 0, name);
  } else {
    const t = cfg.tiers.find((t) => String(t.tier) === String(targetKey))!;
    t.sources.splice(Math.min(idx, t.sources.length), 0, name);
  }
  const changed =
    JSON.stringify(cfg) !== JSON.stringify({ tiers: state.layout.tiers, disabled: state.layout.disabled });
  if (changed) void persistLayout(cfg);
}

// ---------- shared SVG bits ----------
const DL_ICON = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11M6 10l6 6 6-6M5 20h14"></path></svg>`;
const PLAY_ICON = `<svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"></path></svg>`;
const PAUSE_ICON = `<svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><path d="M6 5h4v14H6zM14 5h4v14h-4z"></path></svg>`;
// "Now sounding" indicator — the console's eq-bar idiom (staggered eq1/eq2/eq3).
function eqBars(): string {
  return `<span style="display:inline-flex;align-items:flex-end;gap:2px;height:13px;flex-shrink:0" title="now sounding">
    <span style="width:2.5px;background:#e9ede3;border-radius:2px;animation:eq1 .9s ease-in-out infinite"></span>
    <span style="width:2.5px;background:#e9ede3;border-radius:2px;animation:eq2 1.1s ease-in-out infinite"></span>
    <span style="width:2.5px;background:#e9ede3;border-radius:2px;animation:eq3 .8s ease-in-out infinite"></span>
  </span>`;
}
// Small round play/pause IconButton bound to a playback key + stream url.
function playBtn(key: string, url: string, size: number): string {
  const sounding = playKey === key;
  const isPlaying = sounding && !playPaused;
  return `<button class="qbtn" data-act="playClip" data-key="${esc(key)}" data-url="${esc(url)}" title="${isPlaying ? "Pause" : "Play"}" style="width:${size}px;height:${size}px;border-radius:50%;border:1px solid ${sounding ? "#e9ede3" : "#272c25"};background:${sounding ? "rgba(233,237,227,.09)" : "none"};cursor:pointer;display:inline-flex;align-items:center;justify-content:center;color:${sounding ? "#e9ede3" : "#99a091"};flex-shrink:0;transition:border-color .15s,color .15s">${isPlaying ? PAUSE_ICON : PLAY_ICON}</button>`;
}
// Current-file player: play/pause + slim scrubber + mono timecode, streaming /audio.
function currentFilePlayer(item: ReviewItem): string {
  if (!item.has_audio) return "";
  const key = `cur:${item.id}`;
  const url = api.reviewAudioUrl(item.id);
  const sounding = playKey === key;
  const isPlaying = sounding && !playPaused;
  const pos = sounding ? playPos : 0;
  const total = sounding ? playDur : 0;
  const frac = total > 0 ? Math.min(100, (pos / total) * 100) : 0;
  return `<div style="display:flex;align-items:center;gap:11px;background:#0c0e0c;border:1px solid #272c25;border-radius:14px;padding:10px 13px;margin-bottom:10px">
    ${playBtn(key, url, 30)}
    <span style="font:500 10px 'JetBrains Mono';color:#5d6456;flex-shrink:0">current file</span>
    <div data-act="seekClip" data-key="${esc(key)}" style="flex:1;height:5px;background:#272c25;border-radius:3px;overflow:hidden;cursor:pointer"><div style="height:100%;width:${frac}%;background:#e9ede3;border-radius:3px;transition:width .15s linear;pointer-events:none"></div></div>
    <span style="font:700 11px 'JetBrains Mono';color:#99a091;flex-shrink:0">${dur(pos)} / ${dur(total)}</span>
    ${isPlaying ? eqBars() : ""}
  </div>`;
}
// strategy glyphs: parallel = fork/branch, uniform = rotate/round-robin
const iconParallel = (sz: number, sw: number): string =>
  `<svg width="${sz}" height="${sz}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${sw}" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v5"></path><path d="M12 8l-6 5v8"></path><path d="M12 8l6 5v8"></path><path d="M12 8v13"></path></svg>`;
const iconUniform = (sz: number, sw: number): string =>
  `<svg width="${sz}" height="${sz}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${sw}" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"></path><path d="M21 3v6h-6"></path></svg>`;

// ---------- view builders (return HTML strings) ----------
function statCardsHtml(): string {
  const s = state.stats;
  type Card = [string, string, string, boolean];
  const cards: Card[] = s
    ? [
        ["Requests", String(s.total), `${s.done} done · ${s.failed} failed · ${s.in_flight} live`, true],
        ["Success rate", pct(s.success_rate), `${s.done} of ${s.done + s.failed} completed`, false],
        ["In flight", String(s.in_flight), "live", false],
        ["Lossless", pct(s.lossless_rate), `${s.lossless} of ${s.done} downloads`, false],
        ["Downloaded", bytes(s.total_bytes), "across sources", false],
        ["Avg time", s.avg_elapsed_s ? `${s.avg_elapsed_s.toFixed(1)}s` : "—", "per completed", false],
      ]
    : Array<Card>(6).fill(["—", "—", "", false]);
  return `<div style="display:grid;grid-template-columns:repeat(${state.isMobile ? 2 : 6},1fr);gap:${state.isMobile ? 10 : 12}px;margin-bottom:16px">${cards
    .map(([label, value, sub, light]) => {
      const bg = light ? "#dfe4d6" : "#111411";
      const bd = light ? "#dfe4d6" : "#22261f";
      const lc = light ? "#4d5346" : "#5d6456";
      const vc = light ? "#14170f" : "#e9ede3";
      return `<div style="background:${bg};border:1px solid ${bd};border-radius:18px;padding:14px 16px">
        <div style="font:600 9.5px 'JetBrains Mono';letter-spacing:.14em;text-transform:uppercase;color:${lc}">${esc(label)}</div>
        <div style="font:800 27px 'Doto';margin-top:8px;line-height:1;color:${vc}">${esc(value)}</div>
        <div style="font:400 10.5px 'JetBrains Mono';color:${lc};margin-top:7px">${esc(sub)}</div>
      </div>`;
    })
    .join("")}</div>`;
}

function cardHtml(name: string, tierKey: string, index: number, active: boolean): string {
  const meta = state.sources[name];
  if (!meta) return "";
  const cm = credMeta(meta.credentials);
  const locked = name === "ytdlp";
  const dragging = state.drag.source === name;
  const loss = meta.lossless;
  const missing = meta.credentials === "missing";
  const shake = state.rejectShake === name ? "animation:shake .48s;" : "";
  const accent = tierColor(Number(tierKey) || null);
  const stubColor = active ? accent : "#272c25";
  const stubGlow = active ? `0 0 8px ${accent}` : "none";
  const stubAnim = active ? "flowpulse 1s ease-in-out infinite" : "none";
  const gearOrLock = locked
    ? `<svg title="Pinned final fallback — locked" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#5d6456" stroke-width="2" stroke-linecap="round"><rect x="4" y="11" width="16" height="10" rx="2"></rect><path d="M8 11V7a4 4 0 0 1 8 0v4"></path></svg>`
    : `<button class="gear" data-act="openPanel" data-source="${esc(name)}" title="Configure" style="background:none;border:none;cursor:pointer;color:#5d6456;padding:3px;border-radius:8px;display:inline-flex;transition:color .15s,background .15s">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="3"></circle><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.2 2.2M16.9 16.9l2.2 2.2M19.1 4.9l-2.2 2.2M7.1 16.9l-2.2 2.2"></path></svg>
      </button>`;
  return `<div class="dcard" draggable="${!locked}" data-drag-card data-source="${esc(name)}" data-tier="${esc(tierKey)}" data-index="${index}"
      style="flex:0 1 240px;min-width:168px;display:flex;align-items:center;gap:9px;background:#181c17;border:1px solid ${missing ? "rgba(255,76,56,.4)" : "#272c25"};border-radius:14px;padding:11px 12px;position:relative;z-index:1;cursor:${locked ? "default" : "grab"};opacity:${dragging ? ".4" : "1"};transition:border-color .15s,box-shadow .15s;${shake}">
      <span style="width:6px;height:6px;border-radius:50%;background:${stubColor};box-shadow:${stubGlow};animation:${stubAnim};flex-shrink:0"></span>
      <span style="color:${locked ? "#22261f" : "#5d6456"};font:600 12px 'JetBrains Mono';letter-spacing:-2px;user-select:none">${locked ? "" : "⣿"}</span>
      <div style="flex:1;min-width:0">
        <div style="display:flex;align-items:center;gap:7px">
          <span style="font:600 13px 'JetBrains Mono';color:#e9ede3">${esc(name)}</span>
          <span style="font:600 8.5px 'JetBrains Mono';letter-spacing:.1em;padding:2px 7px;border-radius:10px;${badgeStyle(loss)}">${badgeText(loss)}</span>
        </div>
        <div style="display:flex;align-items:center;gap:6px;margin-top:5px">
          <span style="width:5px;height:5px;border-radius:50%;background:${cm.color};flex-shrink:0"></span>
          <span style="font:500 10px 'JetBrains Mono';color:${cm.color}">${cm.text}</span>
          ${missing ? `<span style="font:400 10px 'JetBrains Mono';color:#ff4c38">· unconfigured</span>` : ""}
        </div>
      </div>
      ${gearOrLock}
    </div>`;
}

function tierHtml(t: TierCfg, showFall: boolean): string {
  const key = String(t.tier);
  const accent = tierColor(t.tier);
  const over = state.drag.overTier === key && state.drag.source;
  const isAll = t.strategy === "all";
  const n = t.sources.length;
  // uniform: highlight the rotating "active" source; parallel: all lit
  const activeIdx = isAll ? -1 : n > 0 ? state.tick % n : -1;
  const nextName = !isAll && n > 1 ? t.sources[(activeIdx + 1) % n] : null;
  const caption = isAll
    ? n <= 1
      ? "parallel · single source, first verified file wins"
      : `parallel · all ${n} fired at once, first verified file wins`
    : `uniform · one per request, rotating${nextName ? ` · next: ${nextName}` : ""}`;
  const tierBg = over ? "rgba(233,237,227,.03)" : "#0e100d";
  const cells: string[] = [];
  for (let i = 0; i <= n; i++) {
    if (over && state.drag.overIndex === i)
      cells.push(
        `<div style="flex:0 0 130px;min-height:56px;border:1px dashed #99a091;border-radius:14px;background:rgba(233,237,227,.04)"></div>`,
      );
    if (i < n) cells.push(cardHtml(t.sources[i], key, i, isAll || i === activeIdx));
  }
  const railColor = isAll ? "rgba(233,237,227,.3)" : "#272c25";
  const hub = `<span title="${isAll ? "Dispatch: broadcast to all sources in parallel" : "Dispatch: round-robin, uniformly distributed"}" style="width:26px;height:26px;border-radius:50%;background:#0c0e0c;border:1px solid ${accent};display:inline-flex;align-items:center;justify-content:center;color:${accent};flex-shrink:0">${isAll ? iconParallel(13, 2) : iconUniform(12, 2.2)}</span>`;
  const stratBtn = (strat: string, label: string, icon: string, active: boolean) =>
    `<button data-act="strategy" data-tier="${key}" data-strategy="${strat}" style="display:inline-flex;align-items:center;gap:5px;border:none;border-radius:16px;cursor:pointer;font:600 10px 'JetBrains Mono';letter-spacing:.04em;padding:5px 11px;background:${active ? "#e9ede3" : "transparent"};color:${active ? "#14170f" : C.faint};transition:.15s">${icon}${label}</button>`;
  return `<div style="position:relative">
    <div style="display:flex;align-items:flex-start;margin-top:12px">
      <span style="width:27px;display:flex;justify-content:center;flex-shrink:0;margin-top:10px;position:relative;z-index:1"><span style="width:27px;height:27px;border-radius:50%;background:#0c0e0c;border:1px solid ${accent};display:inline-flex;align-items:center;justify-content:center;font:800 11px 'Doto';color:${accent}">0${key}</span></span>
      <span style="width:14px;height:1px;background:#2c332a;flex-shrink:0;margin-top:23px"></span>
      <div data-drop-tier="${key}" style="flex:1;min-width:0;border:1px solid ${over ? "#99a091" : "#22261f"};border-radius:18px;background:${tierBg};padding:13px 16px 15px;transition:border-color .15s,background .15s">
        <div style="display:flex;align-items:center;gap:11px;flex-wrap:wrap">
          <span style="font:800 15px 'Doto';color:${accent};letter-spacing:.06em">0${key}</span>
          <span style="font:600 13px 'Instrument Sans'">Tier ${key}</span>
          <span style="font:500 10.5px 'JetBrains Mono';color:#5d6456">${n} ${n === 1 ? "source" : "sources"}</span>
          <div style="margin-left:auto;display:flex;align-items:center;gap:11px">
            <span style="font:400 10px 'JetBrains Mono';color:#5d6456">${esc(caption)}</span>
            <div style="display:flex;background:#0c0e0c;border:1px solid #272c25;border-radius:20px;padding:2px">
              ${stratBtn("all", "parallel", iconParallel(10, 2.4), isAll)}${stratBtn("uniform", "uniform", iconUniform(10, 2.4), !isAll)}
            </div>
          </div>
        </div>
        <div style="position:relative;margin-top:13px">
          <span style="position:absolute;left:0;right:6px;top:50%;height:1px;background:${railColor}"></span>
          <div style="position:relative;display:flex;align-items:center;gap:12px;min-height:64px">
            ${hub}
            ${cells.join("") || `<div style="position:relative;z-index:1;background:${tierBg};padding:8px 10px;font:400 11px 'JetBrains Mono';color:#464d40">Drop sources here</div>`}
          </div>
        </div>
      </div>
    </div>
    ${
      showFall
        ? `<div style="display:flex;align-items:center;gap:11px;margin-top:12px;position:relative">
        <span style="width:27px;display:flex;justify-content:center;flex-shrink:0"><span style="color:#464d40;font-size:9px;line-height:1;background:#111411;padding:3px 0">▼</span></span>
        <span style="font:500 9.5px 'JetBrains Mono';letter-spacing:.05em;color:#5d6456">no verified file · falls through</span>
      </div>`
        : ""
    }
  </div>`;
}

function trayHtml(): string {
  const list = state.layout?.disabled ?? [];
  const over = state.drag.overTier === "disabled" && state.drag.source;
  const cells: string[] = [];
  for (let i = 0; i <= list.length; i++) {
    if (over && state.drag.overIndex === i)
      cells.push(
        `<div style="flex:0 0 130px;border:1px dashed #99a091;border-radius:14px;background:rgba(233,237,227,.04)"></div>`,
      );
    if (i < list.length) cells.push(cardHtml(list[i], "disabled", i, false));
  }
  return `<div data-drop-tier="disabled" style="margin-top:18px;border:1px dashed ${over ? "#99a091" : "#22261f"};border-radius:18px;background:${over ? "rgba(233,237,227,.03)" : "#0e100d"};padding:12px 16px;transition:border-color .15s">
    <div style="display:flex;align-items:center;gap:9px;margin-bottom:9px">
      <span style="font:600 12px 'Instrument Sans';color:#99a091">Available / Disabled</span>
      <span style="font:500 10.5px 'JetBrains Mono';color:#5d6456">${list.length === 0 ? "empty" : list.length + (list.length === 1 ? " source" : " sources")}</span>
      <span style="margin-left:auto;font:400 10px 'JetBrains Mono';color:#464d40">not in the cascade</span>
    </div>
    <div style="display:flex;gap:10px;flex-wrap:wrap;min-height:56px">
      ${cells.join("") || `<div style="align-self:center;font:400 11px 'JetBrains Mono';color:#464d40;padding:12px 4px">Every source is in a tier</div>`}
    </div>
  </div>`;
}

function cascadeHtml(): string {
  const tiers = [...(state.layout?.tiers ?? [])].sort((a, b) => a.tier - b.tier);
  return `<section style="background:#111411;border:1px solid #22261f;border-radius:22px;padding:20px 22px 22px">
    <div style="margin-bottom:6px">
      <h1 style="margin:0;font:600 18px 'Instrument Sans';letter-spacing:-.01em">Cascade</h1>
      <p style="margin:4px 0 0;font:400 12.5px 'Instrument Sans';color:#99a091">Requests fall through tiers until a source produces a verified file. Drag sources between tiers; toggles set each tier's fan-out strategy.</p>
    </div>
    <div style="position:relative;margin-top:14px">
      <span style="position:absolute;left:13px;top:14px;bottom:14px;width:1px;background:#2c332a"></span>
      <div style="display:flex;align-items:center;gap:12px;position:relative">
        <span style="width:27px;display:flex;justify-content:center;flex-shrink:0"><span style="width:11px;height:11px;border-radius:50%;background:#ff4c38;border:4px solid #111411;box-sizing:content-box;animation:pulse 1.6s ease-in-out infinite"></span></span>
        <span style="display:inline-flex;align-items:center;padding:6px 14px;border-radius:20px;background:#181c17;border:1px solid #272c25;font:600 11px 'JetBrains Mono';letter-spacing:.05em;color:#e9ede3">request in</span>
      </div>
      ${tiers.map((t, i) => tierHtml(t, i < tiers.length - 1)).join("")}
      <div style="display:flex;align-items:center;gap:12px;margin-top:12px;position:relative">
        <span style="width:27px;display:flex;justify-content:center;flex-shrink:0"><span style="width:19px;height:19px;border-radius:50%;background:#dfe4d6;display:inline-flex;align-items:center;justify-content:center;font:700 10px 'JetBrains Mono';color:#14170f;border:4px solid #111411;box-sizing:content-box">✓</span></span>
        <span style="display:inline-flex;align-items:center;padding:6px 14px;border-radius:20px;background:#dfe4d6;font:600 11px 'JetBrains Mono';letter-spacing:.05em;color:#14170f">verified file → library</span>
      </div>
    </div>
    ${trayHtml()}
  </section>`;
}

// ---------- Listening panel (Spotify user-OAuth) ----------
function queueControl(t: PlayerTrack, size: number): string {
  if (t.in_library)
    return `<span title="Already in library" style="font:500 9.5px 'JetBrains Mono';color:#5d6456;padding:4px 9px;border-radius:12px;border:1px solid #272c25;flex-shrink:0">in library</span>`;
  if (state.enqueued[t.id])
    return `<span style="font:600 9.5px 'JetBrains Mono';color:#e9ede3;padding:4px 9px;border-radius:12px;background:rgba(233,237,227,.09);flex-shrink:0">✓ queued</span>`;
  return `<button class="qbtn" data-act="enqueue" data-id="${esc(t.id)}" title="Download this track" style="width:${size}px;height:${size}px;border-radius:50%;border:1px solid #272c25;background:none;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;color:#99a091;flex-shrink:0;transition:border-color .15s,color .15s">${DL_ICON}</button>`;
}

function listeningHtml(): string {
  if (!state.spotifyConfigured) return ""; // no user-OAuth set up -> hide entirely
  const wrap = (inner: string): string =>
    `<div style="background:#111411;border:1px solid #22261f;border-radius:18px;padding:15px 17px">
      <div style="display:flex;align-items:center;gap:9px;margin-bottom:11px">
        <span style="font:600 13px 'Instrument Sans'">Listening</span>
        <span style="font:400 10.5px 'JetBrains Mono';color:#5d6456">spotify · ${state.spotifyConnected ? "connected" : "not connected"}</span>
        <span style="margin-left:auto;font:400 10px 'JetBrains Mono';color:#464d40">grab what you're playing</span>
      </div>${inner}</div>`;
  if (!state.spotifyConnected) {
    return wrap(
      `<div style="background:#181c17;border:1px solid #272c25;border-radius:14px;padding:14px;font:400 11.5px 'Instrument Sans';color:#99a091">Log in with Spotify to see your now-playing and import your library.</div>`,
    );
  }
  const np = state.player?.now_playing ?? null;
  const recent = state.player?.recent ?? [];
  const nowBlock = np
    ? `<div style="display:flex;align-items:center;gap:12px;background:#181c17;border:1px solid #272c25;border-radius:14px;padding:11px 12px">
        <span style="width:40px;height:40px;border-radius:10px;background:#0c0e0c;border:1px solid #272c25;display:flex;align-items:flex-end;justify-content:center;gap:2.5px;padding-bottom:10px;flex-shrink:0">
          <span style="width:2.5px;background:#e9ede3;border-radius:2px;animation:eq1 .9s ease-in-out infinite"></span>
          <span style="width:2.5px;background:#e9ede3;border-radius:2px;animation:eq2 1.1s ease-in-out infinite"></span>
          <span style="width:2.5px;background:#e9ede3;border-radius:2px;animation:eq3 .8s ease-in-out infinite"></span>
        </span>
        <div style="flex:1;min-width:0">
          <div style="font:600 13px 'Instrument Sans';color:#e9ede3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(np.title)}</div>
          <div style="font:400 11px 'Instrument Sans';color:#99a091;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(np.artist)}</div>
          <div style="display:flex;align-items:center;gap:8px;margin-top:6px">
            <div style="flex:1;height:3px;background:#272c25;border-radius:3px;overflow:hidden"><div style="height:100%;width:${np.duration_ms ? Math.min(100, Math.round(((np.progress_ms ?? 0) / np.duration_ms) * 100)) : 0}%;background:#e9ede3;border-radius:3px;transition:width 1s linear"></div></div>
            <span style="font:700 11px 'Doto';color:#99a091;flex-shrink:0">${dur((np.progress_ms ?? 0) / 1000)}</span>
          </div>
        </div>
        ${queueControl(np, 30)}
      </div>`
    : `<div style="background:#181c17;border:1px solid #272c25;border-radius:14px;padding:14px;font:400 11.5px 'Instrument Sans';color:#99a091">Nothing playing on Spotify right now.</div>`;
  const recentBlock = recent.length
    ? `<div style="font:600 9px 'JetBrains Mono';letter-spacing:.14em;text-transform:uppercase;color:#464d40;margin:12px 2px 4px">Recently played</div>${recent
        .map((p) => {
          const letter = (p.title || "?").trim().charAt(0).toUpperCase();
          return `<div style="display:flex;align-items:center;gap:10px;padding:8px 2px;border-top:1px solid #1a1e19">
            <span style="width:30px;height:30px;border-radius:8px;background:#181c17;border:1px solid #272c25;display:flex;align-items:center;justify-content:center;font:800 12px 'Doto';color:#5d6456;flex-shrink:0">${esc(letter)}</span>
            <div style="flex:1;min-width:0">
              <div style="font:500 12px 'Instrument Sans';color:#e9ede3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(p.title)}</div>
              <div style="font:400 10px 'Instrument Sans';color:#5d6456;margin-top:1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(p.artist)}</div>
            </div>
            <span style="font:400 9.5px 'JetBrains Mono';color:#464d40;flex-shrink:0">${p.played_at ? relTime(p.played_at) : ""}</span>
            ${queueControl(p, 26)}
          </div>`;
        })
        .join("")}`
    : "";
  return wrap(nowBlock + recentBlock + importHtml());
}

// "Import from Spotify" — pull the user's Liked Songs or a playlist into the queue.
function importHtml(): string {
  const chevron = state.importOpen ? "▾" : "▸";
  const header = `<button data-act="toggleImport" class="ghost" style="margin-top:12px;width:100%;display:flex;align-items:center;gap:8px;background:#0c0e0c;border:1px solid #272c25;border-radius:14px;padding:11px 14px;cursor:pointer;color:#e9ede3;font:600 12px 'Instrument Sans';transition:border-color .15s">
    <span style="width:8px;height:8px;border-radius:50%;background:#1db954"></span>Import from Spotify
    <span style="margin-left:auto;color:#5d6456">${chevron}</span></button>`;
  if (!state.importOpen) return header;

  const likedBusy = !!state.importBusy["liked"];
  const liked = `<div style="display:flex;align-items:center;gap:10px;padding:9px 4px;border-top:1px solid #1a1e19">
    <span style="width:30px;height:30px;border-radius:8px;background:linear-gradient(135deg,#4300a0,#c13584);display:flex;align-items:center;justify-content:center;flex-shrink:0">♥</span>
    <div style="flex:1;min-width:0"><div style="font:600 12px 'Instrument Sans';color:#e9ede3">Liked Songs</div></div>
    ${importBtn("importLiked", likedBusy)}</div>`;

  let body: string;
  if (state.importErr) {
    body = `<div style="padding:10px 4px;font:400 11px 'Instrument Sans';color:#ff4c38">${esc(state.importErr)}</div>`;
  } else if (state.importPlaylists === null) {
    body = `<div style="padding:10px 4px;font:400 11px 'Instrument Sans';color:#5d6456">Loading playlists…</div>`;
  } else {
    body = state.importPlaylists
      .map((p) => {
        const busy = !!state.importBusy[p.id];
        const art = p.image
          ? `<img src="${esc(p.image)}" alt="" style="width:30px;height:30px;border-radius:8px;object-fit:cover;flex-shrink:0"/>`
          : `<span style="width:30px;height:30px;border-radius:8px;background:#181c17;border:1px solid #272c25;display:flex;align-items:center;justify-content:center;font:800 12px 'Doto';color:#5d6456;flex-shrink:0">${esc((p.name[0] || "?").toUpperCase())}</span>`;
        return `<div style="display:flex;align-items:center;gap:10px;padding:9px 4px;border-top:1px solid #1a1e19">
          ${art}
          <div style="flex:1;min-width:0"><div style="font:500 12px 'Instrument Sans';color:#e9ede3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(p.name)}</div><div style="font:400 10px 'JetBrains Mono';color:#5d6456">${p.count} track${p.count === 1 ? "" : "s"}</div></div>
          ${syncBtn(p.id, p.name)}
          ${importBtn("importPlaylist", busy, p.id, p.name)}</div>`;
      })
      .join("");
  }
  return header + liked + body;
}

// Per-playlist continuous-sync toggle: a small pill that reads "Sync" (off) or
// "Synced ✓" (on, tinted). Continuous sync tracks *future* additions — the Import
// button next to it is for pulling the current contents.
function syncBtn(pid: string, name: string): string {
  const on = !!state.synced[pid];
  const busy = !!state.syncBusy[pid];
  const col = on ? "#1db954" : "#5d6456";
  const bg = on ? "rgba(29,185,84,.10)" : "transparent";
  const bd = on ? "rgba(29,185,84,.35)" : "#272c25";
  return `<button data-act="toggleSync" data-pid="${esc(pid)}" data-name="${esc(name)}" ${busy ? "disabled" : ""} title="${on ? "Stop auto-queuing new tracks" : "Auto-queue tracks added later"}" style="flex-shrink:0;background:${bg};border:1px solid ${bd};border-radius:12px;color:${col};font:600 11px 'Instrument Sans';padding:7px 11px;cursor:${busy ? "default" : "pointer"};transition:.15s">${on ? "Synced ✓" : "Sync"}</button>`;
}

function importBtn(act: string, busy: boolean, pid?: string, name?: string): string {
  const attrs = pid ? ` data-pid="${esc(pid)}" data-name="${esc(name || "")}"` : "";
  return `<button data-act="${act}"${attrs} ${busy ? "disabled" : ""} style="flex-shrink:0;background:${busy ? "#181c17" : "rgba(29,185,84,.14)"};border:1px solid ${busy ? "#272c25" : "rgba(29,185,84,.4)"};border-radius:12px;color:${busy ? "#5d6456" : "#1db954"};font:600 11px 'Instrument Sans';padding:7px 13px;cursor:${busy ? "default" : "pointer"};transition:.15s">${busy ? "Queuing…" : "Import"}</button>`;
}

function liveHtml(): string {
  const rows = state.inflight;
  const body = rows.length
    ? rows
        .map((d) => {
          const queued = d.status === "queued";
          const el = queued ? "—" : dur((Date.now() - new Date(d.created_at).getTime()) / 1000);
          const spinner = queued
            ? `<span title="Queued" style="width:12px;height:12px;border:1.5px dashed #5d6456;border-radius:50%;flex-shrink:0"></span>`
            : `<span style="width:12px;height:12px;border:1.5px solid #272c25;border-top-color:#e9ede3;border-radius:50%;animation:spin .8s linear infinite;flex-shrink:0"></span>`;
          return `<div style="display:flex;align-items:center;gap:10px;padding:9px 0;border-top:1px solid #1a1e19">
        ${spinner}
        <span style="flex:1;min-width:0;font:500 12.5px 'Instrument Sans';overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(rowLabel(d))}</span>
        <span style="font:500 10px 'JetBrains Mono';color:#99a091;padding:2px 8px;background:#181c17;border-radius:10px">${esc(d.requested_via)}</span>
        <span style="font:700 13px 'Doto';color:#e9ede3">${el}</span>
      </div>`;
        })
        .join("")
    : `<div style="padding:14px 0 6px;text-align:center;color:#5d6456;font:400 12px 'Instrument Sans'">Nothing downloading right now.</div>`;
  return `<div style="background:#111411;border:1px solid #22261f;border-radius:18px;padding:15px 17px">
    <div style="display:flex;align-items:center;gap:9px;margin-bottom:10px">
      <span style="font:600 13px 'Instrument Sans'">Live</span>
      <span style="font:400 10.5px 'JetBrains Mono';color:#5d6456">${rows.length} in-flight · SSE</span>
    </div>${body}</div>`;
}

function winsHtml(): string {
  const wins = state.stats?.wins_by_tier ?? {};
  const max = Math.max(1, ...Object.values(wins));
  const bars = [1, 2, 3]
    .map((t) => {
      const v = wins[String(t)] ?? 0;
      return `<div style="display:flex;align-items:center;gap:12px;padding:4px 0">
        <span style="width:56px;font:500 10.5px 'JetBrains Mono';color:#4d5346;text-align:right;flex-shrink:0">Tier ${t}</span>
        <div style="flex:1;height:6px;background:rgba(20,23,15,.12);border-radius:6px;overflow:hidden"><div style="height:100%;width:${Math.round((v / max) * 100)}%;background:#14170f;opacity:${0.35 + 0.65 * (v / max)};border-radius:6px;transition:width .5s cubic-bezier(.2,.8,.2,1)"></div></div>
        <span style="width:24px;font:800 14px 'Doto';color:#14170f;text-align:right;flex-shrink:0">${v}</span>
      </div>`;
    })
    .join("");
  return `<div style="background:#dfe4d6;border:1px solid #dfe4d6;border-radius:18px;padding:15px 17px">
    <div style="font:600 13px 'Instrument Sans';color:#14170f;margin-bottom:10px">Wins by tier</div>${bars}</div>`;
}

function historyHtml(): string {
  const rows = state.history;
  const list = rows.length
    ? rows
        .map((r) => {
          const sm = statusMeta(r.status);
          const won =
            r.status === "done"
              ? `T${r.tier_won} · ${r.source_won}`
              : r.status === "failed"
                ? "failed"
                : "live";
          const wonColor =
            r.status === "done" ? tierColor(r.tier_won) : r.status === "failed" ? C.bad : C.good;
          const quality =
            r.status === "downloading" || r.status === "queued"
              ? "In progress"
              : r.lossless === 1
                ? "FLAC"
                : r.quality_label || "no file";
          const pulse =
            r.status === "downloading" ? "animation:pulse 1.4s ease-in-out infinite" : "";
          return `<div class="hrow" data-act="openDetail" data-id="${esc(r.id)}" style="display:flex;align-items:center;gap:10px;padding:10px 17px;border-top:1px solid #1a1e19;cursor:pointer;transition:background .12s">
        <span title="${esc(sm.label)}" style="width:6px;height:6px;border-radius:50%;background:${sm.color};flex-shrink:0;${pulse}"></span>
        <div style="flex:1;min-width:0">
          <div style="font:500 12.5px 'Instrument Sans';overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(rowLabel(r))}</div>
          <div style="font:400 10px 'JetBrains Mono';color:#5d6456;margin-top:2px">${esc(quality)} · ${bytes(r.size_bytes)}</div>
        </div>
        <span style="font:500 10px 'JetBrains Mono';color:${wonColor};flex-shrink:0">${esc(won)}</span>
        <span style="font:400 10px 'JetBrains Mono';color:#5d6456;flex-shrink:0;width:52px;text-align:right">${r.status === "downloading" || r.status === "queued" ? "live" : relTime(r.created_at)}</span>
      </div>`;
        })
        .join("")
    : `<div style="padding:32px 16px;text-align:center;color:#5d6456;font:400 13px 'Instrument Sans'">No requests match.</div>`;
  const opt = (v: string, label: string) =>
    `<option value="${v}"${state.histStatus === v ? " selected" : ""}>${label}</option>`;
  return `<div style="background:#111411;border:1px solid #22261f;border-radius:18px;overflow:hidden;display:flex;flex-direction:column">
    <div style="display:flex;align-items:center;gap:10px;padding:14px 17px 11px">
      <span style="font:600 13px 'Instrument Sans';flex-shrink:0">History</span>
      <div style="position:relative;flex:1;min-width:100px;display:flex;align-items:center">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#5d6456" stroke-width="2" stroke-linecap="round" style="position:absolute;left:10px"><circle cx="11" cy="11" r="7"></circle><path d="M21 21l-4.3-4.3"></path></svg>
        <input data-inp="search" value="${esc(state.histSearch)}" placeholder="Search…" style="width:100%;background:#0c0e0c;border:1px solid #272c25;border-radius:12px;color:#e9ede3;font:400 12px 'Instrument Sans';padding:7px 10px 7px 29px"/>
      </div>
      <select data-chg="status" style="background:#0c0e0c;border:1px solid #272c25;border-radius:12px;color:#e9ede3;font:500 11px 'Instrument Sans';padding:7px 8px;cursor:pointer">
        ${opt("all", "All")}${opt("done", "Done")}${opt("failed", "Failed")}${opt("downloading", "Downloading")}
      </select>
    </div>
    <div style="max-height:430px;overflow-y:auto">${list}</div>
  </div>`;
}

// synthetic download log from attempts (matches the design)
function buildLogs(r: RequestDetail): string {
  let t0 = Date.parse(r.created_at);
  const rows: string[] = [];
  const fmt = (ms: number) => new Date(ms).toISOString().slice(11, 19);
  const push = (level: string, msg: string) => {
    const lc = level === "ERROR" ? C.bad : level === "WARN" ? C.muted : level === "OK" ? C.good : C.dim;
    const mc = level === "ERROR" ? C.bad : level === "OK" ? C.text : C.muted;
    rows.push(
      `<div style="display:flex;gap:10px;font:400 11px 'JetBrains Mono';line-height:1.9;white-space:nowrap"><span style="color:#464d40;flex-shrink:0">${fmt(t0)}</span><span style="color:${lc};flex-shrink:0;width:38px;font-weight:600">${level}</span><span style="color:${mc}">${esc(msg)}</span></div>`,
    );
    t0 += 900 + ((rows.length * 347) % 1300);
  };
  push("INFO", `request accepted via ${r.requested_via} — "${r.query}"`);
  let lastTier: number | null = null;
  for (const a of r.attempts) {
    if (a.tier !== lastTier) {
      push("INFO", `tier ${a.tier} dispatch`);
      lastTier = a.tier;
    }
    if (a.ok) push("OK", `${a.source}: verified file — ${a.detail}`);
    else push("WARN", `${a.source}: ${a.detail}`);
  }
  if (r.status === "failed") push("ERROR", `cascade exhausted — ${r.attempts.length} attempts, no verified file`);
  else if (r.download) push("INFO", `wrote ${r.download.path} (${bytes(r.download.size_bytes)})`);
  return rows.join("");
}

function drawerHtml(): string {
  const open = !!state.detail;
  const d = state.detail;
  let inner = "";
  if (d) {
    const sm = statusMeta(d.status);
    const n = d.attempts.length;
    const trail = d.attempts
      .map((a, i) => {
        const okColor = a.ok ? C.good : C.bad;
        return `<div style="display:flex;gap:12px;padding-bottom:16px;position:relative">
        <div style="display:flex;flex-direction:column;align-items:center;flex-shrink:0">
          <span style="width:24px;height:24px;border-radius:50%;background:${a.ok ? "rgba(233,237,227,.07)" : "rgba(255,76,56,.07)"};border:1px solid ${okColor};display:flex;align-items:center;justify-content:center;font:700 11px 'JetBrains Mono';color:${okColor};z-index:1">${a.ok ? "✓" : "✗"}</span>
          ${i < n - 1 ? `<span style="width:1px;flex:1;background:#272c25;margin-top:2px;min-height:14px"></span>` : ""}
        </div>
        <div style="flex:1;min-width:0;padding-bottom:2px">
          <div style="display:flex;align-items:center;gap:8px"><span style="font:600 13px 'JetBrains Mono';color:#e9ede3">${esc(a.source)}</span><span style="font:800 11px 'Doto';color:${tierColor(a.tier)}">T${a.tier}</span></div>
          <div style="font:400 11.5px 'JetBrains Mono';color:${a.ok ? "#e9ede3" : C.muted};margin-top:3px;line-height:1.4">${esc(a.detail)}</div>
        </div>
      </div>`;
      })
      .join("");
    const dl = d.download;
    let facts = "";
    if (dl) {
      const codec = dl.bit_depth
        ? `${dl.codec} · ${dl.bit_depth}-bit / ${(dl.sample_rate ?? 0) / 1000} kHz`
        : `${dl.codec} · ${Math.round((dl.bitrate ?? 0) / 1000)} kbps`;
      const factRows: [string, string][] = [
        ["Path", dl.path],
        ["Quality", dl.quality_label],
        ["Codec", codec],
        ["Size", bytes(dl.size_bytes)],
        ["Duration", dur(dl.duration_s)],
        ["Download time", dl.elapsed_s != null ? `${dl.elapsed_s.toFixed(1)}s` : "—"],
      ];
      facts = `<div style="background:#0c0e0c;border:1px solid #272c25;border-radius:14px;overflow:hidden">${factRows
        .map(
          ([k, v]) => `<div style="display:flex;justify-content:space-between;gap:14px;padding:10px 14px;border-bottom:1px solid #171b16"><span style="font:500 10.5px 'JetBrains Mono';color:#5d6456;flex-shrink:0">${esc(k)}</span><span style="font:500 11.5px 'JetBrains Mono';color:#e9ede3;text-align:right;word-break:break-all">${esc(v)}</span></div>`,
        )
        .join("")}</div>`;
    } else if (d.status === "failed") {
      facts = `<div style="background:rgba(255,76,56,.07);border:1px solid rgba(255,76,56,.35);border-radius:14px;padding:14px;font:400 11.5px 'JetBrains Mono';color:#ff6b58;line-height:1.5">${esc(d.error || "No source produced a verified file.")}</div>`;
    } else {
      facts = `<div style="display:flex;align-items:center;gap:10px;background:#181c17;border:1px solid #272c25;border-radius:14px;padding:14px;font:500 11.5px 'JetBrains Mono';color:#e9ede3"><span style="width:12px;height:12px;border:1.5px solid #272c25;border-top-color:#e9ede3;border-radius:50%;animation:spin .8s linear infinite"></span>In progress — cascade still running.</div>`;
    }
    const factsTitle = d.status === "done" ? "Winner" : d.status === "failed" ? "Result" : "Status";
    inner = `<div style="padding:20px 22px;border-bottom:1px solid #1d211c">
      <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px">
        <div style="min-width:0">
          <div style="font:600 17px 'Instrument Sans';letter-spacing:-.01em;line-height:1.25">${esc(d.query)}</div>
          <div style="display:flex;align-items:center;gap:10px;margin-top:9px">
            <span style="display:inline-flex;align-items:center;gap:6px;padding:3px 11px;border-radius:20px;background:${sm.bg};font:600 10.5px 'JetBrains Mono';color:${sm.color}"><span style="width:5px;height:5px;border-radius:50%;background:${sm.color}"></span>${sm.label}</span>
            <span style="font:500 10.5px 'JetBrains Mono';color:#99a091;padding:2px 9px;background:#181c17;border-radius:10px">${esc(d.requested_via)}</span>
            <span style="font:400 10.5px 'JetBrains Mono';color:#5d6456">${d.status === "downloading" ? "live" : relTime(d.created_at)}</span>
          </div>
        </div>
        <button class="closebtn" data-act="closeDetail" style="background:#181c17;border:1px solid #272c25;border-radius:10px;color:#99a091;cursor:pointer;padding:6px 9px;flex-shrink:0;display:inline-flex"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"></path></svg></button>
      </div>
    </div>
    <div style="flex:1;overflow-y:auto;padding:20px 22px">
      <div style="font:600 9.5px 'JetBrains Mono';letter-spacing:.14em;text-transform:uppercase;color:#5d6456;margin-bottom:12px">Cascade trail</div>
      ${trail}
      <div style="font:600 9.5px 'JetBrains Mono';letter-spacing:.14em;text-transform:uppercase;color:#5d6456;margin:8px 0 12px">${factsTitle}</div>
      ${facts}
      <button class="ghost" data-act="toggleLogs" style="margin-top:16px;width:100%;display:flex;align-items:center;justify-content:space-between;background:#0c0e0c;border:1px solid #272c25;border-radius:14px;padding:11px 14px;cursor:pointer;color:#99a091;transition:border-color .15s">
        <span style="display:flex;align-items:center;gap:9px;font:600 10px 'JetBrains Mono';letter-spacing:.12em;text-transform:uppercase"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 6h16M4 12h16M4 18h10"></path></svg>Download log</span>
        <span style="font:500 10.5px 'JetBrains Mono';color:#5d6456">${state.showLogs ? "hide ▴" : "show ▾"}</span>
      </button>
      ${state.showLogs ? `<div style="margin-top:8px;background:#090b09;border:1px solid #1d211c;border-radius:14px;padding:12px 14px;overflow-x:auto;animation:fadein .2s ease">${buildLogs(d)}</div>` : ""}
    </div>`;
  }
  return sheetChrome(inner, open, "closeDetail", 50, "min(470px,94vw)");
}

// The "Test" button, reflecting an in-flight credential check.
function validateButton(name: string): string {
  const testing = state.panelValidate[name]?.status === "testing";
  return `<button class="ghost" data-act="validatePanel" ${testing ? "disabled" : ""} style="background:#181c17;border:1px solid #272c25;border-radius:14px;color:#e9ede3;font:600 13px 'Instrument Sans';padding:12px 16px;cursor:${testing ? "default" : "pointer"};display:inline-flex;align-items:center;gap:8px">${
    testing
      ? `<span style="width:12px;height:12px;border:1.5px solid #272c25;border-top-color:#e9ede3;border-radius:50%;animation:spin .8s linear infinite"></span>Testing…`
      : "Test"
  }</button>`;
}

// Optional track to force through this source. Empty = the known test song, so
// the button stays a one-click credential check until you need a specific track.
function testQueryField(): string {
  return `<div>
      <input data-inp="panelTestQuery" value="${esc(state.panelTestQuery)}" placeholder="Force a specific track through this source (optional)…" style="width:100%;background:#0c0e0c;border:1px solid #272c25;border-radius:12px;color:#e9ede3;font:400 11.5px 'Instrument Sans';padding:9px 12px"/>
    </div>`;
}

// Result of the last credential test for this source (downloads a known song).
function validationRow(name: string): string {
  const v = state.panelValidate[name];
  if (!v) return "";
  if (v.status === "testing")
    return `<div style="font:400 10.5px 'JetBrains Mono';color:${C.muted};display:flex;align-items:center;gap:8px"><span style="width:11px;height:11px;border:1.5px solid #272c25;border-top-color:#e9ede3;border-radius:50%;animation:spin .8s linear infinite"></span>${esc(v.detail)}</div>`;
  const ok = v.status === "ok";
  const col = ok ? "#9fce8a" : "#ff6b58";
  const bg = ok ? "rgba(159,206,138,.08)" : "rgba(255,107,88,.08)";
  const bd = ok ? "rgba(159,206,138,.3)" : "rgba(255,107,88,.3)";
  return `<div style="display:flex;align-items:flex-start;gap:8px;background:${bg};border:1px solid ${bd};border-radius:12px;padding:10px 12px;font:400 11px 'JetBrains Mono';color:#e9ede3;line-height:1.5"><span style="color:${col};flex-shrink:0;font-weight:700">${ok ? "✓" : "✗"}</span><span style="word-break:break-word">${esc(v.detail)}</span></div>`;
}

function panelHtml(): string {
  const name = state.panelSource;
  const open = !!name;
  let inner = "";
  if (name) {
    const def = PANELDEF[name];
    const meta = state.sources[name];
    const cm = credMeta(meta.credentials);
    const enabled = meta.enabled;
    const pinned = !!def.pinned;
    const loss = meta.lossless;
    const dirty = state.panelSave === "dirty";
    const saving = state.panelSave === "saving";
    const saved = state.panelSave === "saved";
    const settings = def.settings
      .map(
        ([key, label, type, dflt]) => `<div style="margin-bottom:16px">
        <label style="display:block;font:500 12px 'JetBrains Mono';color:#e9ede3;margin-bottom:6px">${esc(label)}</label>
        <input data-inp="panelField" data-key="${key}" value="${esc(state.panelValues[key] ?? dflt)}" type="${type}" style="width:100%;background:#0c0e0c;border:1px solid #272c25;border-radius:12px;color:#e9ede3;font:500 13px 'JetBrains Mono';padding:10px 13px"/>
      </div>`,
      )
      .join("");
    const configured = (env: string) => !meta.missing_env.includes(env);
    const creds = def.creds
      .map(([env, label]) => {
        const ok = configured(env);
        return `<div style="margin-bottom:15px">
        <label style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
          <span style="font:500 12px 'JetBrains Mono';color:#e9ede3">${esc(label)}</span>
          <span style="display:inline-flex;align-items:center;gap:5px;font:500 10px 'JetBrains Mono';color:${ok ? C.good : C.bad}"><span style="width:5px;height:5px;border-radius:50%;background:${ok ? C.good : C.bad}"></span>${ok ? "configured" : "not set"}</span>
        </label>
        <input data-inp="panelField" data-key="${env}" value="${esc(state.panelValues[env] ?? "")}" type="password" placeholder="${ok ? "•••••••• configured — type to replace" : "not set"}" style="width:100%;background:#0c0e0c;border:1px solid #272c25;border-radius:12px;color:#e9ede3;font:500 13px 'JetBrains Mono';padding:10px 13px"/>
        <div style="font:400 10px 'JetBrains Mono';color:#464d40;margin-top:5px">${env}</div>
      </div>`;
      })
      .join("");
    const tierLabel = pinned
      ? "Tier 3 · pinned fallback"
      : `${enabled ? "Tier " + meta.tier : "Disabled"} · ${cm.text} credentials`;
    inner = `<div style="padding:20px 22px;border-bottom:1px solid #1d211c">
      <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px">
        <div>
          <div style="display:flex;align-items:center;gap:9px"><span style="font:600 18px 'JetBrains Mono';letter-spacing:-.01em">${esc(name)}</span><span style="font:600 8.5px 'JetBrains Mono';letter-spacing:.1em;padding:2px 8px;border-radius:10px;${badgeStyle(loss)}">${badgeText(loss)}</span></div>
          <div style="font:500 10.5px 'JetBrains Mono';color:${tierColor(meta.tier)};margin-top:7px">${esc(tierLabel)}</div>
        </div>
        <button class="closebtn" data-act="closePanel" style="background:#181c17;border:1px solid #272c25;border-radius:10px;color:#99a091;cursor:pointer;padding:6px 9px;display:inline-flex"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"></path></svg></button>
      </div>
    </div>
    <div style="flex:1;overflow-y:auto;padding:20px 22px">
      <div style="display:flex;align-items:center;justify-content:space-between;background:#0c0e0c;border:1px solid #272c25;border-radius:14px;padding:13px 15px;margin-bottom:20px">
        <div><div style="font:600 13px 'Instrument Sans'">Enabled</div><div style="font:400 10.5px 'JetBrains Mono';color:#5d6456;margin-top:3px">${pinned ? "Locked on" : enabled ? "Active in the cascade" : "Held in the tray"}</div></div>
        <button data-act="togglePanelEnabled" ${pinned ? "disabled" : ""} style="width:42px;height:24px;border-radius:14px;border:none;cursor:${pinned ? "not-allowed" : "pointer"};background:${enabled ? "#e9ede3" : "#272c25"};position:relative;transition:background .2s;flex-shrink:0"><span style="position:absolute;top:2px;left:${enabled ? "20px" : "2px"};width:20px;height:20px;border-radius:50%;background:${enabled ? "#14170f" : "#99a091"};transition:left .2s;box-shadow:0 1px 3px rgba(0,0,0,.4)"></span></button>
      </div>
      ${def.note ? `<div style="background:#0c0e0c;border:1px solid #272c25;border-radius:14px;padding:13px 15px;font:400 11.5px 'JetBrains Mono';color:#99a091;line-height:1.55;margin-bottom:20px">${esc(def.note)}</div>` : ""}
      ${settings ? `<div style="font:600 9.5px 'JetBrains Mono';letter-spacing:.14em;text-transform:uppercase;color:#5d6456;margin-bottom:11px">Settings</div>${settings}` : ""}
      ${creds ? `<div style="font:600 9.5px 'JetBrains Mono';letter-spacing:.14em;text-transform:uppercase;color:#5d6456;margin:4px 0 11px;display:flex;align-items:center;gap:8px">Credentials <span style="font-weight:400;text-transform:none;letter-spacing:0;color:#464d40">· encrypted at rest, never displayed</span></div>${creds}` : ""}
    </div>
    <div style="padding:16px 22px;border-top:1px solid #1d211c;display:flex;flex-direction:column;gap:11px">
      ${def.validate ? testQueryField() : ""}
      ${validationRow(name)}
      <div style="display:flex;align-items:center;gap:12px">
        <button data-act="savePanel" ${saving ? "disabled" : ""} style="flex:1;background:${saved ? "rgba(233,237,227,.14)" : saving ? "#272c25" : "#e9ede3"};border:none;border-radius:14px;color:${saved ? "#e9ede3" : saving ? C.muted : "#14170f"};font:600 13px 'Instrument Sans';padding:12px;cursor:${saving ? "default" : "pointer"};transition:.15s;display:flex;align-items:center;justify-content:center;gap:8px">${saving ? "Saving…" : saved ? "Saved ✓" : dirty ? "Save changes" : "Save"}</button>
        ${def.validate ? validateButton(name) : ""}
      </div>
    </div>`;
  }
  return sheetChrome(inner, open, "closePanel", 52, "min(440px,94vw)");
}

function searchDropdownHtml(): string {
  const q = state.searchQuery.trim();
  const open = state.searchFocus && (state.searchBusy || state.searchErr != null || q.length >= 2);
  if (!open) return "";
  let body = "";
  if (state.searchErr)
    body = `<div style="padding:16px 18px;font:400 12px 'Instrument Sans';color:#ff6b58">Search error: ${esc(state.searchErr)}</div>`;
  else if (state.searchBusy && !state.searchResults.length)
    body = `<div style="display:flex;align-items:center;gap:10px;padding:16px 18px;font:400 12px 'Instrument Sans';color:#99a091"><span style="width:12px;height:12px;border:1.5px solid #272c25;border-top-color:#e9ede3;border-radius:50%;animation:spin .8s linear infinite"></span>Searching…</div>`;
  else if (!state.searchResults.length)
    body = `<div style="padding:18px;text-align:center;font:400 12px 'Instrument Sans';color:#5d6456">No tracks match.</div>`;
  else
    body = `<div style="max-height:400px;overflow-y:auto">${state.searchResults
      .map((t) => {
        const done = !!state.enqueued[t.id];
        const meta = [t.album, t.year].filter(Boolean).join(" · ");
        const art = t.art
          ? `<img src="${esc(t.art)}" alt="" referrerpolicy="no-referrer" style="width:44px;height:44px;border-radius:10px;object-fit:cover;flex-shrink:0;background:#181c17"/>`
          : `<span style="width:44px;height:44px;border-radius:10px;background:#181c17;border:1px solid #272c25;display:flex;align-items:center;justify-content:center;font:800 16px 'Doto';color:#99a091;flex-shrink:0">${esc((t.title || "?").charAt(0).toUpperCase())}</span>`;
        const ctrl = done
          ? `<span style="font:600 10px 'JetBrains Mono';color:#e9ede3;padding:5px 10px;border-radius:14px;background:rgba(233,237,227,.09);flex-shrink:0">✓ queued</span>`
          : `<span class="qbtn" data-act="enqueue" data-id="${esc(t.id)}" title="Queue download" style="width:28px;height:28px;border-radius:50%;border:1px solid #272c25;display:inline-flex;align-items:center;justify-content:center;color:#99a091;flex-shrink:0;cursor:pointer;transition:border-color .15s,color .15s">${DL_ICON}</span>`;
        return `<div class="srow" data-act="enqueue" data-id="${esc(t.id)}" style="display:flex;align-items:center;gap:12px;padding:10px 14px;cursor:pointer;border-top:1px solid #1a1e19;transition:background .12s">
          ${art}
          <div style="flex:1;min-width:0">
            <div style="font:600 13px 'Instrument Sans';color:#e9ede3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(t.title)}</div>
            <div style="font:400 11px 'Instrument Sans';color:#99a091;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(t.artist)}</div>
            ${meta ? `<div style="font:400 9.5px 'JetBrains Mono';color:#5d6456;margin-top:3px">${esc(meta)}</div>` : ""}
          </div>
          <span style="font:500 10px 'JetBrains Mono';color:#5d6456;flex-shrink:0">${dur(t.duration_ms / 1000)}</span>
          ${ctrl}
        </div>`;
      })
      .join("")}</div>`;
  // Footer: cross-source fallback for tracks Spotify doesn't have.
  const footer = `<button data-act="openSourceDialog" class="ghost" style="width:100%;display:flex;align-items:center;justify-content:center;gap:8px;background:#0c0e0c;border:none;border-top:1px solid #1d211c;color:#99a091;font:600 11px 'JetBrains Mono';letter-spacing:.03em;padding:11px;cursor:pointer;transition:color .15s">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"></circle><path d="M21 21l-4.3-4.3"></path></svg>Not on Spotify? Search the sources →</button>`;
  return `<div style="position:absolute;top:calc(100% + 8px);left:0;right:0;background:#111411;border:1px solid #272c25;border-radius:16px;box-shadow:0 24px 70px rgba(0,0,0,.65);overflow:hidden;animation:fadein .15s ease;z-index:70">${body}${footer}</div>`;
}

// A candidate row in the cross-source dialog: source + quality + match%, an
// audition play button (streamable sources), and a Download button that queues the
// exact candidate. Reuses the review tab's shared clip player (playBtn).
// Album-art tile for a candidate, with the audition play/pause overlaid when the
// source is streamable (reuses the shared clip player via the playClip action).
function srcArt(c: ReviewCandidate): string {
  const tile = c.art
    ? `<img src="${esc(c.art)}" alt="" referrerpolicy="no-referrer" style="width:46px;height:46px;border-radius:10px;object-fit:cover;background:#181c17;display:block"/>`
    : `<span style="width:46px;height:46px;border-radius:10px;background:#181c17;border:1px solid #272c25;display:flex;align-items:center;justify-content:center;font:800 17px 'Doto';color:#99a091">${esc((c.title || "?").charAt(0).toUpperCase())}</span>`;
  if (!c.streamable)
    return `<div style="position:relative;flex-shrink:0;width:46px;height:46px">${tile}</div>`;
  const key = `src:${c.id}`;
  const url = c.preview_url ?? api.reviewCandidateStreamUrl(c.source, c.ref);
  const sounding = playKey === key;
  const isPlaying = sounding && !playPaused;
  const overlay = `<button class="qbtn" data-act="playClip" data-key="${esc(key)}" data-url="${esc(url)}" title="${isPlaying ? "Pause" : "Play"}" style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;border:none;border-radius:10px;background:rgba(8,10,8,${sounding ? "0.34" : "0.5"});color:#e9ede3;cursor:pointer;transition:background .15s">${isPlaying ? PAUSE_ICON : PLAY_ICON}</button>`;
  return `<div style="position:relative;flex-shrink:0;width:46px;height:46px">${tile}${overlay}</div>`;
}

function srcRow(c: ReviewCandidate): string {
  const col = tierColor(c.tier);
  const badge = `<span style="font:700 9.5px 'JetBrains Mono';letter-spacing:.04em;color:${col};border:1px solid ${col};border-radius:8px;padding:2px 7px;flex-shrink:0">${esc(c.source.toUpperCase())}</span>`;
  const quality = `<span style="font:500 10px 'JetBrains Mono';color:${c.lossless ? "#e9ede3" : "#7c8371"}">${esc(c.quality_label || (c.lossless ? "FLAC" : "lossy"))}</span>`;
  const durTxt = c.duration_s ? `<span style="font:500 10px 'JetBrains Mono';color:#5d6456">${dur(c.duration_s)}</span>` : "";
  const play = srcArt(c);
  const queued = !!state.srcQueued[c.id];
  const dl = queued
    ? `<span style="font:600 10px 'JetBrains Mono';color:#e9ede3;padding:6px 11px;border-radius:14px;background:rgba(233,237,227,.09);flex-shrink:0">✓ queued</span>`
    : `<button class="qbtn" data-act="downloadSource" data-id="${esc(c.id)}" title="Download from ${esc(c.source)}" style="width:30px;height:30px;border-radius:50%;border:1px solid #272c25;background:none;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;color:#99a091;flex-shrink:0;transition:border-color .15s,color .15s">${DL_ICON}</button>`;
  return `<div style="display:flex;align-items:center;gap:11px;padding:11px 16px;border-top:1px solid #1a1e19">
      ${play}
      <div style="flex:1;min-width:0">
        <div style="font:600 13px 'Instrument Sans';color:#e9ede3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(c.title || "Untitled")}</div>
        <div style="display:flex;align-items:center;gap:8px;margin-top:3px;overflow:hidden">
          <span style="font:400 11px 'Instrument Sans';color:#99a091;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(c.artist || "")}</span>
        </div>
      </div>
      ${badge}${quality}${durTxt}
      <span style="font:700 11px 'Doto';color:${c.match >= 70 ? "#e9ede3" : c.match >= 40 ? "#99a091" : "#5d6456"};flex-shrink:0;min-width:34px;text-align:right">${c.match}%</span>
      ${dl}
    </div>`;
}

function sourceDialogHtml(): string {
  if (!state.srcOpen) return "";
  let body: string;
  if (state.srcErr)
    body = `<div style="padding:22px 18px;text-align:center;font:400 12px 'Instrument Sans';color:#ff6b58">Search error: ${esc(state.srcErr)}</div>`;
  else if (state.srcBusy && !state.srcResults?.length)
    body = `<div style="display:flex;align-items:center;justify-content:center;gap:10px;padding:34px 18px;font:400 12px 'Instrument Sans';color:#99a091"><span style="width:13px;height:13px;border:1.5px solid #272c25;border-top-color:#e9ede3;border-radius:50%;animation:spin .8s linear infinite"></span>Searching every source…</div>`;
  else if (state.srcResults === null)
    body = `<div style="padding:34px 18px;text-align:center;font:400 12px 'Instrument Sans';color:#5d6456">Type a track name and search across every linked source.</div>`;
  else if (!state.srcResults.length)
    body = `<div style="padding:34px 18px;text-align:center;font:400 12px 'Instrument Sans';color:#5d6456">No source had a match. Try refining the query.</div>`;
  else body = `<div style="max-height:52vh;overflow-y:auto">${state.srcResults.map(srcRow).join("")}</div>`;
  const scrim = `<div data-act="closeSourceDialog" style="position:fixed;inset:0;background:rgba(5,7,5,.62);z-index:80;animation:fadein .18s ease"></div>`;
  return `${scrim}<div role="dialog" style="position:fixed;top:9vh;left:50%;transform:translateX(-50%);width:min(680px,92vw);background:#111411;border:1px solid #272c25;border-radius:20px;box-shadow:0 30px 90px rgba(0,0,0,.7);z-index:81;overflow:hidden;animation:fadein .18s ease">
    <div style="display:flex;align-items:center;gap:12px;padding:16px 18px 12px">
      <div style="flex:1;min-width:0">
        <div style="font:600 15px 'Instrument Sans';color:#e9ede3">Search download sources</div>
        <div style="font:400 11.5px 'Instrument Sans';color:#5d6456;margin-top:2px">Not on Spotify? Search Qobuz, Deezer, arcod &amp; co. directly, then queue the exact match.</div>
      </div>
      <button data-act="closeSourceDialog" class="closebtn" style="background:#181c17;border:1px solid #272c25;border-radius:10px;color:#99a091;cursor:pointer;padding:6px 9px;flex-shrink:0;display:inline-flex"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"></path></svg></button>
    </div>
    <div style="padding:0 18px 14px;display:flex;align-items:center;position:relative">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#5d6456" stroke-width="2" stroke-linecap="round" style="position:absolute;left:31px;pointer-events:none"><circle cx="11" cy="11" r="7"></circle><path d="M21 21l-4.3-4.3"></path></svg>
      <input data-inp="srcsearch" value="${esc(state.srcQuery)}" placeholder="Artist — Title…" autofocus style="width:100%;background:#0c0e0c;border:1px solid #272c25;border-radius:14px;color:#e9ede3;font:400 13px 'Instrument Sans';padding:10px 14px 10px 40px"/>
    </div>
    <div style="border-top:1px solid #1d211c">${body}</div>
  </div>`;
}

function activeDownloads(): number {
  const inBatches = state.batches.reduce((n, b) => n + b.counts.queued + b.counts.downloading, 0);
  const singles = state.inflight.filter((r) => !r.batch_id).length;
  return inBatches + singles;
}
// Number of items needing review — drives the red count badge on the review tab.
function activeReview(): number {
  return state.reviewStats ? state.reviewStats.needs_review : state.reviewItems.length;
}

// Segmented page switcher (console | downloads | review) sitting next to the logo.
function navTabsHtml(): string {
  const n = activeDownloads();
  const dlBadge =
    n > 0
      ? `<span style="font:700 10px 'Doto';min-width:17px;height:17px;padding:0 4px;border-radius:10px;background:#ff4c38;color:#0c0e0c;display:inline-flex;align-items:center;justify-content:center">${n}</span>`
      : "";
  const rv = activeReview();
  const rvBadge =
    rv > 0
      ? `<span style="font:700 10px 'Doto';min-width:17px;height:17px;padding:0 4px;border-radius:10px;background:#ff4c38;color:#0c0e0c;display:inline-flex;align-items:center;justify-content:center">${rv}</span>`
      : "";
  const seg = (active: boolean): string =>
    active ? "background:#e9ede3;color:#0c0e0c" : "background:transparent;color:#99a091";
  return `<nav style="display:flex;align-items:center;gap:3px;background:#131612;border:1px solid #22261f;border-radius:20px;padding:3px;margin-left:4px;flex-shrink:0">
      <button data-act="gotoConsole" style="border:none;border-radius:16px;cursor:pointer;font:600 11px 'JetBrains Mono';letter-spacing:.04em;padding:6px 14px;${seg(state.page === "console")};transition:.15s">console</button>
      <button data-act="gotoFeed" style="border:none;border-radius:16px;cursor:pointer;font:600 11px 'JetBrains Mono';letter-spacing:.04em;padding:6px 14px;${seg(state.page === "feed")};transition:.15s">feed</button>
      <button data-act="gotoBulk" style="display:inline-flex;align-items:center;gap:7px;border:none;border-radius:16px;cursor:pointer;font:600 11px 'JetBrains Mono';letter-spacing:.04em;padding:6px 14px;${seg(state.page === "bulk")};transition:.15s">downloads${dlBadge}</button>
      <button data-act="gotoReview" style="display:inline-flex;align-items:center;gap:7px;border:none;border-radius:16px;cursor:pointer;font:600 11px 'JetBrains Mono';letter-spacing:.04em;padding:6px 14px;${seg(state.page === "review")};transition:.15s">review${rvBadge}</button>
    </nav>`;
}

// Right-side button that opens the bulk paste drawer (distinct from the page tab).
function bulkBtnHtml(): string {
  const n = activeDownloads();
  const count = n > 0 ? `<span style="font:800 12px 'Doto';color:#e9ede3">${n}</span>` : "";
  return `<button data-act="openBulkPanel" title="Bulk downloads panel" style="display:flex;align-items:center;gap:8px;padding:7px 13px;background:#131612;border:1px solid #272c25;border-radius:20px;cursor:pointer;color:#99a091;font:500 11px 'JetBrains Mono';letter-spacing:.06em;transition:border-color .15s" class="ghost">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11M6 10l6 6 6-6M5 20h14"></path></svg>bulk${count}
    </button>`;
}

function headerHtml(): string {
  const saveMap: Record<SaveState, [string, string, string, string, boolean]> = {
    idle: ["All changes saved", C.faint, "#131612", "#272c25", false],
    saving: ["Saving…", "#e9ede3", "#181c17", "#39402f", true],
    saved: ["Saved ✓", "#e9ede3", "rgba(233,237,227,.09)", "#39402f", false],
  };
  const [txt, col, bg, bd, spin] = saveMap[state.saveState];
  const live = !!state.stats;
  const search = state.searchOff
    ? ""
    : `<div style="position:relative;flex:1;max-width:460px;margin-left:16px">
        <div style="display:flex;align-items:center;position:relative">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#5d6456" stroke-width="2" stroke-linecap="round" style="position:absolute;left:13px;pointer-events:none"><circle cx="11" cy="11" r="7"></circle><path d="M21 21l-4.3-4.3"></path></svg>
          <input data-inp="spotify" value="${esc(state.searchQuery)}" placeholder="Search tracks or paste a Spotify / YouTube playlist link…" style="width:100%;background:#0c0e0c;border:1px solid #272c25;border-radius:20px;color:#e9ede3;font:400 12.5px 'Instrument Sans';padding:8px 14px 8px 34px;transition:border-color .15s"/>
        </div>
        ${searchDropdownHtml()}
      </div>`;
  return `<header style="position:sticky;top:0;z-index:40;background:rgba(12,14,12,.85);backdrop-filter:blur(14px);border-bottom:1px solid #1d211c">
    <div style="max-width:1560px;margin:0 auto;padding:0 28px;display:flex;align-items:center;gap:16px">
      <div style="display:flex;align-items:center;gap:12px;padding:15px 0">
        <span style="width:10px;height:10px;border-radius:50%;background:#ff4c38"></span>
        <span style="font:800 17px 'Doto';letter-spacing:.08em">crate</span>
        <span style="font:500 11px 'JetBrains Mono';color:#5d6456;letter-spacing:.14em;text-transform:uppercase;margin-top:1px">console</span>
      </div>
      ${navTabsHtml()}
      ${search}
      <div style="margin-left:auto;display:flex;align-items:center;gap:10px">
        ${bulkBtnHtml()}
        <div style="display:flex;align-items:center;gap:8px;padding:7px 14px;border-radius:20px;background:${bg};border:1px solid ${bd};transition:.2s">
          ${spin ? `<span style="width:11px;height:11px;border:1.5px solid #272c25;border-top-color:#e9ede3;border-radius:50%;animation:spin .8s linear infinite"></span>` : ""}
          <span style="font:600 11px 'JetBrains Mono';color:${col}">${txt}</span>
        </div>
        <div style="display:flex;align-items:center;gap:8px;padding:7px 13px;background:#131612;border:1px solid #272c25;border-radius:20px">
          <span style="width:7px;height:7px;border-radius:50%;background:${live ? "#ff4c38" : C.faint};animation:pulse 2s ease-in-out infinite"></span>
          <span style="font:500 11px 'JetBrains Mono';color:#99a091;letter-spacing:.06em">${live ? "live" : "connecting…"}</span>
        </div>
        ${userChipHtml()}
      </div>
    </div>
  </header>`;
}

// Logged-in user + logout, shown only when Login-with-Spotify is active.
function userChipHtml(): string {
  const me = state.me;
  if (!me || !me.spotify_id) return "";
  const avatar = me.avatar_url
    ? `<img src="${esc(me.avatar_url)}" alt="" style="width:22px;height:22px;border-radius:50%;object-fit:cover"/>`
    : `<span style="width:22px;height:22px;border-radius:50%;background:#272c25;display:inline-flex;align-items:center;justify-content:center;font:700 10px 'JetBrains Mono';color:#99a091">${esc((me.display_name[0] || "?").toUpperCase())}</span>`;
  return `<div style="display:flex;align-items:center;gap:8px;padding:5px 8px 5px 6px;background:#131612;border:1px solid #272c25;border-radius:20px">
    ${avatar}
    <span style="font:600 11.5px 'Instrument Sans';color:#e9ede3;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(me.display_name)}</span>
    <button data-act="logout" title="Log out" style="background:none;border:none;cursor:pointer;color:#5d6456;padding:2px;display:inline-flex;border-radius:6px">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"></path><polyline points="16 17 21 12 16 7"></polyline><line x1="21" y1="12" x2="9" y2="12"></line></svg>
    </button>
  </div>`;
}

// Responsive side-drawer (desktop) / bottom-sheet (mobile) chrome around inner.
function sheetChrome(inner: string, open: boolean, closeAct: string, z: number, deskWidth: string): string {
  const scrim = `<div data-act="${closeAct}" style="position:fixed;inset:0;background:rgba(5,7,5,.6);z-index:${z};transition:opacity .28s;opacity:${open ? "1" : "0"};pointer-events:${open ? "auto" : "none"}"></div>`;
  if (state.isMobile) {
    const handle = `<div style="display:flex;justify-content:center;padding:10px 0 2px;flex-shrink:0"><span style="width:36px;height:4px;border-radius:4px;background:#272c25"></span></div>`;
    return `${scrim}<aside style="position:fixed;left:50%;bottom:0;width:100%;max-width:640px;max-height:88vh;background:#111411;border:1px solid #22261f;border-bottom:none;border-radius:22px 22px 0 0;z-index:${z + 1};transform:translateX(-50%) ${open ? "translateY(0)" : "translateY(101%)"};transition:transform .3s cubic-bezier(.3,.8,.3,1);box-shadow:0 -24px 60px rgba(0,0,0,.55);display:flex;flex-direction:column">${open ? handle : ""}${inner}</aside>`;
  }
  return `${scrim}<aside style="position:fixed;top:0;right:0;bottom:0;width:${deskWidth};background:#111411;border-left:1px solid #22261f;z-index:${z + 1};transform:${open ? "translateX(0)" : "translateX(103%)"};transition:transform .3s cubic-bezier(.3,.8,.3,1);box-shadow:-24px 0 60px rgba(0,0,0,.55);display:flex;flex-direction:column">${inner}</aside>`;
}

const LOCK_SVG = `<svg title="Pinned final fallback — locked" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#5d6456" stroke-width="2" stroke-linecap="round"><rect x="4" y="11" width="16" height="10" rx="2"></rect><path d="M8 11V7a4 4 0 0 1 8 0v4"></path></svg>`;
const gearBtn = (name: string): string =>
  `<button class="gear" data-act="openPanel" data-source="${esc(name)}" title="Configure" style="background:none;border:none;cursor:pointer;color:#5d6456;padding:10px;margin:-8px -6px -8px 0;border-radius:10px;display:inline-flex;transition:color .15s,background .15s"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="3"></circle><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.2 2.2M16.9 16.9l2.2 2.2M19.1 4.9l-2.2 2.2M7.1 16.9l-2.2 2.2"></path></svg></button>`;

// ---------- mobile (tabbed app) builders ----------
function mCardHtml(name: string, tierKey: string, index: number, active: boolean): string {
  const meta = state.sources[name];
  if (!meta) return "";
  const cm = credMeta(meta.credentials);
  const locked = name === "ytdlp";
  const loss = meta.lossless;
  const missing = meta.credentials === "missing";
  const dragging = state.drag.source === name;
  const shake = state.rejectShake === name ? "animation:shake .48s;" : "";
  const accent = tierColor(Number(tierKey) || null);
  const stub =
    tierKey === "disabled"
      ? ""
      : `<span style="width:6px;height:6px;border-radius:50%;background:${active ? accent : "#272c25"};box-shadow:${active ? `0 0 8px ${accent}` : "none"};animation:${active ? "flowpulse 1s ease-in-out infinite" : "none"};flex-shrink:0"></span>`;
  return `<div class="dcard" draggable="${!locked}" data-drag-card data-source="${esc(name)}" data-tier="${esc(tierKey)}" data-index="${index}" style="display:flex;align-items:center;gap:10px;background:#181c17;border:1px solid ${missing ? "rgba(255,76,56,.4)" : "#272c25"};border-radius:14px;padding:12px;opacity:${dragging ? ".4" : "1"};transition:border-color .15s;${shake}">
    ${stub}
    <span style="color:${locked ? "#22261f" : "#5d6456"};font:600 13px 'JetBrains Mono';letter-spacing:-2px;user-select:none">${locked ? "" : "⣿"}</span>
    <div style="flex:1;min-width:0">
      <div style="display:flex;align-items:center;gap:7px"><span style="font:600 13px 'JetBrains Mono';color:#e9ede3">${esc(name)}</span><span style="font:600 8.5px 'JetBrains Mono';letter-spacing:.1em;padding:2px 7px;border-radius:10px;${badgeStyle(loss)}">${badgeText(loss)}</span></div>
      <div style="display:flex;align-items:center;gap:6px;margin-top:5px"><span style="width:5px;height:5px;border-radius:50%;background:${cm.color};flex-shrink:0"></span><span style="font:500 10px 'JetBrains Mono';color:${cm.color}">${cm.text}</span>${missing ? `<span style="font:400 10px 'JetBrains Mono';color:#ff4c38">· unconfigured</span>` : ""}</div>
    </div>
    ${locked ? LOCK_SVG : gearBtn(name)}
  </div>`;
}

function mTierHtml(t: TierCfg, showFall: boolean): string {
  const key = String(t.tier);
  const accent = tierColor(t.tier);
  const over = state.drag.overTier === key && state.drag.source;
  const isAll = t.strategy === "all";
  const n = t.sources.length;
  const activeIdx = isAll ? -1 : n > 0 ? state.tick % n : -1;
  const nextName = !isAll && n > 1 ? t.sources[(activeIdx + 1) % n] : null;
  const caption = isAll
    ? n <= 1
      ? "parallel · first verified file wins"
      : `parallel · all ${n} at once, first wins`
    : `uniform · rotating${nextName ? ` · next: ${nextName}` : ""}`;
  const cells: string[] = [];
  for (let i = 0; i <= n; i++) {
    if (over && state.drag.overIndex === i)
      cells.push(`<div style="height:52px;border:1px dashed #99a091;border-radius:14px;background:rgba(233,237,227,.04)"></div>`);
    if (i < n) cells.push(mCardHtml(t.sources[i], key, i, isAll || i === activeIdx));
  }
  const seg = (strat: string, label: string, icon: string, on: boolean) =>
    `<button data-act="strategy" data-tier="${key}" data-strategy="${strat}" style="flex:1;display:inline-flex;align-items:center;justify-content:center;gap:6px;border:none;border-radius:16px;cursor:pointer;font:600 11px 'JetBrains Mono';letter-spacing:.03em;padding:9px 8px;background:${on ? "#e9ede3" : "transparent"};color:${on ? "#14170f" : C.faint};transition:.15s">${icon}${label}</button>`;
  return `<div style="position:relative">
    <div style="display:flex;align-items:flex-start;margin-top:12px">
      <span style="width:27px;display:flex;justify-content:center;flex-shrink:0;margin-top:12px;position:relative;z-index:1"><span style="width:27px;height:27px;border-radius:50%;background:#0c0e0c;border:1px solid ${accent};display:inline-flex;align-items:center;justify-content:center;font:800 11px 'Doto';color:${accent}">0${key}</span></span>
      <span style="width:9px;height:1px;background:#2c332a;flex-shrink:0;margin-top:25px"></span>
      <div data-drop-tier="${key}" style="flex:1;min-width:0;border:1px solid ${over ? "#99a091" : "#22261f"};border-radius:18px;background:${over ? "rgba(233,237,227,.03)" : "#0e100d"};padding:13px 13px 14px;transition:border-color .15s,background .15s">
        <div style="display:flex;align-items:center;gap:9px">
          <span style="font:600 13px 'Instrument Sans'">Tier ${key}</span>
          <span style="font:500 10px 'JetBrains Mono';color:#5d6456">${n} ${n === 1 ? "source" : "sources"}</span>
          <span title="dispatch" style="margin-left:auto;width:26px;height:26px;border-radius:50%;background:#0c0e0c;border:1px solid ${accent};display:inline-flex;align-items:center;justify-content:center;color:${accent};flex-shrink:0">${isAll ? iconParallel(13, 2) : iconUniform(12, 2.2)}</span>
        </div>
        <div style="display:flex;background:#0c0e0c;border:1px solid #272c25;border-radius:20px;padding:2px;margin-top:11px">
          ${seg("all", "parallel", iconParallel(11, 2.4), isAll)}${seg("uniform", "uniform", iconUniform(11, 2.4), !isAll)}
        </div>
        <div style="font:400 10px 'JetBrains Mono';color:#5d6456;margin-top:8px">${esc(caption)}</div>
        <div style="display:flex;flex-direction:column;gap:9px;margin-top:11px">
          ${cells.join("") || `<div style="padding:10px 2px;font:400 11px 'JetBrains Mono';color:#464d40">Drop sources here</div>`}
        </div>
      </div>
    </div>
    ${
      showFall
        ? `<div style="display:flex;align-items:center;gap:11px;margin-top:12px;position:relative">
        <span style="width:27px;display:flex;justify-content:center;flex-shrink:0"><span style="color:#464d40;font-size:9px;line-height:1;background:#0c0e0c;padding:3px 0">▼</span></span>
        <span style="font:500 9.5px 'JetBrains Mono';letter-spacing:.05em;color:#5d6456">no verified file · falls through</span>
      </div>`
        : ""
    }
  </div>`;
}

function mCascadeTab(): string {
  const tiers = [...(state.layout?.tiers ?? [])].sort((a, b) => a.tier - b.tier);
  const tray = state.layout?.disabled ?? [];
  const trayOver = state.drag.overTier === "disabled" && state.drag.source;
  const trayCells: string[] = [];
  for (let i = 0; i <= tray.length; i++) {
    if (trayOver && state.drag.overIndex === i)
      trayCells.push(`<div style="height:52px;border:1px dashed #99a091;border-radius:14px;background:rgba(233,237,227,.04)"></div>`);
    if (i < tray.length) trayCells.push(mCardHtml(tray[i], "disabled", i, false));
  }
  return `<h1 style="margin:0;font:600 19px 'Instrument Sans';letter-spacing:-.01em">Cascade</h1>
    <p style="margin:5px 0 0;font:400 12.5px 'Instrument Sans';color:#99a091;line-height:1.5">Requests fall through tiers until a source produces a verified file.</p>
    <div style="position:relative;margin-top:16px">
      <span style="position:absolute;left:13px;top:14px;bottom:14px;width:1px;background:#2c332a"></span>
      <div style="display:flex;align-items:center;gap:11px;position:relative">
        <span style="width:27px;display:flex;justify-content:center;flex-shrink:0"><span style="width:11px;height:11px;border-radius:50%;background:#ff4c38;border:4px solid #0c0e0c;box-sizing:content-box;animation:pulse 1.6s ease-in-out infinite"></span></span>
        <span style="display:inline-flex;align-items:center;padding:6px 14px;border-radius:20px;background:#181c17;border:1px solid #272c25;font:600 11px 'JetBrains Mono';letter-spacing:.05em;color:#e9ede3">request in</span>
      </div>
      ${tiers.map((t, i) => mTierHtml(t, i < tiers.length - 1)).join("")}
      <div style="display:flex;align-items:center;gap:11px;margin-top:12px;position:relative">
        <span style="width:27px;display:flex;justify-content:center;flex-shrink:0"><span style="width:19px;height:19px;border-radius:50%;background:#dfe4d6;display:inline-flex;align-items:center;justify-content:center;font:700 10px 'JetBrains Mono';color:#14170f;border:4px solid #0c0e0c;box-sizing:content-box">✓</span></span>
        <span style="display:inline-flex;align-items:center;padding:6px 14px;border-radius:20px;background:#dfe4d6;font:600 11px 'JetBrains Mono';letter-spacing:.05em;color:#14170f">verified file → library</span>
      </div>
    </div>
    <div data-drop-tier="disabled" style="margin-top:18px;border:1px dashed ${trayOver ? "#99a091" : "#22261f"};border-radius:18px;background:${trayOver ? "rgba(233,237,227,.03)" : "#0e100d"};padding:13px;transition:border-color .15s">
      <div style="display:flex;align-items:center;gap:9px;margin-bottom:10px"><span style="font:600 12px 'Instrument Sans';color:#99a091">Available / Disabled</span><span style="font:500 10px 'JetBrains Mono';color:#5d6456">${tray.length === 0 ? "empty" : tray.length + (tray.length === 1 ? " source" : " sources")}</span></div>
      <div style="display:flex;flex-direction:column;gap:9px;min-height:44px">${trayCells.join("") || `<div style="padding:8px 2px;font:400 11px 'JetBrains Mono';color:#464d40">Every source is in a tier</div>`}</div>
    </div>`;
}

function mTracksTab(): string {
  const q = state.searchQuery.trim();
  const searchBox = state.searchOff
    ? ""
    : `<div style="position:relative;display:flex;align-items:center">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#5d6456" stroke-width="2" stroke-linecap="round" style="position:absolute;left:14px;pointer-events:none"><circle cx="11" cy="11" r="7"></circle><path d="M21 21l-4.3-4.3"></path></svg>
        <input data-inp="spotify" value="${esc(state.searchQuery)}" placeholder="Search tracks or paste a Spotify / YouTube playlist link…" style="width:100%;background:#111411;border:1px solid #272c25;border-radius:20px;color:#e9ede3;font:400 13px 'Instrument Sans';padding:11px 15px 11px 38px"/>
      </div>${mSearchResults(q)}`;
  return `<h1 style="margin:0 0 12px;font:600 19px 'Instrument Sans';letter-spacing:-.01em">Tracks</h1>
    ${searchBox}
    <div style="margin-top:14px">${listeningHtml()}</div>`;
}

function mSearchResults(q: string): string {
  const active = state.searchBusy || state.searchErr != null || q.length >= 2;
  if (!active) return "";
  let body = "";
  if (state.searchErr) body = `<div style="padding:15px 16px;font:400 12px 'Instrument Sans';color:#ff6b58">Search error: ${esc(state.searchErr)}</div>`;
  else if (state.searchBusy && !state.searchResults.length)
    body = `<div style="display:flex;align-items:center;gap:10px;padding:15px 16px;font:400 12px 'Instrument Sans';color:#99a091"><span style="width:12px;height:12px;border:1.5px solid #272c25;border-top-color:#e9ede3;border-radius:50%;animation:spin .8s linear infinite"></span>Searching…</div>`;
  else if (!state.searchResults.length) body = `<div style="padding:16px;text-align:center;font:400 12px 'Instrument Sans';color:#5d6456">No tracks match.</div>`;
  else
    body = state.searchResults
      .map((t) => {
        const done = !!state.enqueued[t.id];
        const meta = [t.album, t.year].filter(Boolean).join(" · ");
        const art = t.art
          ? `<img src="${esc(t.art)}" alt="" referrerpolicy="no-referrer" style="width:42px;height:42px;border-radius:10px;object-fit:cover;flex-shrink:0;background:#181c17"/>`
          : `<span style="width:42px;height:42px;border-radius:10px;background:#181c17;border:1px solid #272c25;display:flex;align-items:center;justify-content:center;font:800 15px 'Doto';color:#99a091;flex-shrink:0">${esc((t.title || "?").charAt(0).toUpperCase())}</span>`;
        const ctrl = done
          ? `<span style="font:600 10px 'JetBrains Mono';color:#e9ede3;padding:6px 10px;border-radius:14px;background:rgba(233,237,227,.09);flex-shrink:0">✓ queued</span>`
          : `<button class="qbtn" data-act="enqueue" data-id="${esc(t.id)}" style="width:38px;height:38px;border-radius:50%;border:1px solid #272c25;background:none;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;color:#99a091;flex-shrink:0;transition:border-color .15s,color .15s">${DL_ICON}</button>`;
        return `<div style="display:flex;align-items:center;gap:11px;padding:11px 13px;border-top:1px solid #1a1e19">${art}
          <div style="flex:1;min-width:0"><div style="font:600 13px 'Instrument Sans';overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(t.title)}</div><div style="font:400 11px 'Instrument Sans';color:#99a091;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(t.artist)}</div>${meta ? `<div style="font:400 9.5px 'JetBrains Mono';color:#5d6456;margin-top:3px">${esc(meta)}</div>` : ""}</div>
          ${ctrl}</div>`;
      })
      .join("");
  const footer = `<button data-act="openSourceDialog" style="width:100%;display:flex;align-items:center;justify-content:center;gap:8px;background:#0c0e0c;border:none;border-top:1px solid #1d211c;color:#99a091;font:600 11px 'JetBrains Mono';letter-spacing:.03em;padding:12px;cursor:pointer">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"></circle><path d="M21 21l-4.3-4.3"></path></svg>Not on Spotify? Search the sources →</button>`;
  return `<div style="background:#111411;border:1px solid #272c25;border-radius:16px;overflow:hidden;margin-top:10px;animation:fadein .15s ease">${body}${footer}</div>`;
}

function mHeaderHtml(): string {
  const saveMap: Record<SaveState, [string, string, string, string, boolean]> = {
    idle: ["saved", C.faint, "#131612", "#272c25", false],
    saving: ["saving", "#e9ede3", "#181c17", "#39402f", true],
    saved: ["saved ✓", "#e9ede3", "rgba(233,237,227,.09)", "#39402f", false],
  };
  const [txt, col, bg, bd, spin] = saveMap[state.saveState];
  const live = !!state.stats;
  return `<header style="position:sticky;top:0;z-index:40;background:rgba(12,14,12,.88);backdrop-filter:blur(14px);border-bottom:1px solid #1d211c;padding:14px 18px;display:flex;align-items:center;gap:10px">
    <span style="width:9px;height:9px;border-radius:50%;background:#ff4c38;flex-shrink:0"></span>
    <span style="font:800 16px 'Doto';letter-spacing:.08em">crate</span>
    <span style="font:500 10px 'JetBrains Mono';color:#5d6456;letter-spacing:.14em;text-transform:uppercase;margin-top:1px">console</span>
    <div style="margin-left:auto;display:flex;align-items:center;gap:8px">
      <button data-act="gotoBulk" title="Bulk downloads" style="display:inline-flex;align-items:center;gap:5px;border:1px solid #272c25;border-radius:20px;cursor:pointer;padding:6px 10px;background:#131612;color:#99a091"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11M6 10l6 6 6-6M5 20h14"></path></svg>${activeDownloads() > 0 ? `<span style="font:700 9px 'Doto';min-width:15px;height:15px;padding:0 3px;border-radius:9px;background:#ff4c38;color:#0c0e0c;display:inline-flex;align-items:center;justify-content:center">${activeDownloads()}</span>` : ""}</button>
      <div style="display:flex;align-items:center;gap:7px;padding:6px 11px;border-radius:20px;background:${bg};border:1px solid ${bd};transition:.2s">
        ${spin ? `<span style="width:10px;height:10px;border:1.5px solid #272c25;border-top-color:#e9ede3;border-radius:50%;animation:spin .8s linear infinite"></span>` : ""}
        <span style="font:600 10px 'JetBrains Mono';color:${col}">${txt}</span>
      </div>
      <div style="display:flex;align-items:center;gap:7px;padding:6px 11px;background:#131612;border:1px solid #272c25;border-radius:20px"><span style="width:6px;height:6px;border-radius:50%;background:${live ? "#ff4c38" : C.faint};animation:pulse 2s ease-in-out infinite"></span><span style="font:500 10px 'JetBrains Mono';color:#99a091;letter-spacing:.06em">${live ? "live" : "…"}</span></div>
    </div>
  </header>`;
}

function mBottomNav(): string {
  const tab = state.activeTab;
  const cInk = (t: string) => (tab === t ? "#e9ede3" : "#5d6456");
  const liveBadge = state.inflight.length > 0;
  const btn = (t: string, label: string, icon: string, badge = false) =>
    `<button data-tab="${t}" data-act="setTab" style="flex:1;background:none;border:none;cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:4px;padding:8px 0;color:${cInk(t)};transition:color .15s;position:relative">${icon}<span style="font:600 9.5px 'JetBrains Mono';letter-spacing:.08em">${label}</span>${badge && liveBadge ? `<span style="position:absolute;top:6px;left:calc(50% + 8px);width:7px;height:7px;border-radius:50%;background:#ff4c38;animation:pulse 1.6s ease-in-out infinite"></span>` : ""}</button>`;
  const casc = `<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v5"></path><path d="M12 8l-6 5v8"></path><path d="M12 8l6 5v8"></path><path d="M12 8v13"></path></svg>`;
  const act = `<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"></path></svg>`;
  const trk = `<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="18" r="3"></circle><circle cx="18" cy="16" r="3"></circle><path d="M11 18V5l10-2v13"></path></svg>`;
  return `<nav style="position:fixed;bottom:0;left:50%;transform:translateX(-50%);width:100%;max-width:640px;z-index:45;background:rgba(12,14,12,.92);backdrop-filter:blur(14px);border-top:1px solid #1d211c;display:flex;padding:6px 8px calc(14px + env(safe-area-inset-bottom))">
    ${btn("cascade", "CASCADE", casc)}${btn("activity", "ACTIVITY", act, true)}${btn("tracks", "TRACKS", trk)}
  </nav>`;
}

function toastHtml(): string {
  return state.toast
    ? `<div style="position:fixed;bottom:${state.isMobile ? "84px" : "26px"};left:50%;transform:translateX(-50%);z-index:60;display:flex;align-items:center;gap:10px;background:#dfe4d6;border-radius:14px;padding:12px 19px;box-shadow:0 12px 40px rgba(0,0,0,.5);animation:toastin .3s cubic-bezier(.3,.8,.3,1)"><span style="width:7px;height:7px;border-radius:50%;background:#14170f"></span><span style="font:600 12.5px 'JetBrains Mono';color:#14170f">${esc(state.toast)}</span></div>`
    : "";
}

function viewMobile(): string {
  let tab = "";
  if (state.activeTab === "cascade") tab = mCascadeTab();
  else if (state.activeTab === "activity")
    tab = `<h1 style="margin:0 0 14px;font:600 19px 'Instrument Sans';letter-spacing:-.01em">Activity</h1>
      ${statCardsHtml()}
      <div style="display:flex;flex-direction:column;gap:12px">${liveHtml()}${mFeedHtml()}${winsHtml()}${historyHtml()}</div>`;
  else tab = mTracksTab();
  return `<div style="max-width:640px;margin:0 auto;position:relative">
    ${mHeaderHtml()}
    <main style="padding:16px 16px calc(104px + env(safe-area-inset-bottom));animation:fadein .25s ease">${tab}</main>
    ${mBottomNav()}
    ${drawerHtml()}
    ${panelHtml()}
    ${toastHtml()}
  </div>`;
}

function viewDesktop(): string {
  return `${headerHtml()}
    <main style="max-width:1560px;margin:0 auto;padding:22px 28px 72px;animation:fadein .25s ease">
      ${statCardsHtml()}
      <div class="grid2" style="display:grid;grid-template-columns:65fr 35fr;gap:16px;align-items:start">
        ${cascadeHtml()}
        <div style="display:flex;flex-direction:column;gap:14px">${listeningHtml()}${liveHtml()}${winsHtml()}${historyHtml()}</div>
      </div>
    </main>
    ${drawerHtml()}
    ${panelHtml()}
    ${toastHtml()}`;
}

// ---------- bulk downloads page + panel ----------
function batchActive(b: Batch): number {
  return b.counts.queued + b.counts.downloading;
}
const originLabel = (o: string): string => (o === "youtube" ? "YT" : o === "spotify" ? "SPOTIFY" : "PASTE");

function batchTrackRow(t: BatchDetail["tracks"][number]): string {
  const glyph =
    t.status === "done"
      ? `<span style="font:600 11px 'JetBrains Mono';color:#99a091">✓</span>`
      : t.status === "failed"
        ? `<span style="font:600 11px 'JetBrains Mono';color:#ff4c38">✗</span>`
        : t.status === "downloading"
          ? `<span style="width:11px;height:11px;border:1.5px solid #272c25;border-top-color:#e9ede3;border-radius:50%;animation:spin .8s linear infinite"></span>`
          : t.status === "cancelled"
            ? `<span style="font:600 10px 'JetBrains Mono';color:#5d6456">⊘</span>`
            : `<span style="width:11px;height:11px;border:1.5px dashed #5d6456;border-radius:50%"></span>`;
  const won =
    t.status === "done" && t.source_won
      ? `<span style="font:500 10px 'JetBrains Mono';color:#99a091;padding:2px 8px;background:#181c17;border-radius:10px;flex-shrink:0">${esc(t.source_won)}</span><span style="font:800 11px 'Doto';color:${tierColor(t.tier_won)};flex-shrink:0">T${t.tier_won}</span>`
      : `<span style="font:500 10px 'JetBrains Mono';color:#5d6456;flex-shrink:0">${esc(t.status)}</span>`;
  const retry =
    t.status === "failed" || t.status === "cancelled"
      ? `<button class="qbtn" data-act="retryTrack" data-id="${esc(t.id)}" style="background:none;border:1px solid #272c25;border-radius:10px;color:#99a091;font:600 9.5px 'JetBrains Mono';padding:4px 10px;cursor:pointer;flex-shrink:0;transition:border-color .15s,color .15s">retry</button>`
      : "";
  return `<div style="display:flex;align-items:center;gap:11px;padding:8px 18px;border-top:1px solid #1a1e19">
    <span style="width:14px;display:flex;justify-content:center;flex-shrink:0">${glyph}</span>
    <span style="flex:1;min-width:0;font:500 12.5px 'Instrument Sans';overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:${t.status === "queued" ? "#99a091" : "#e9ede3"}">${esc(t.query)}</span>
    ${won}${retry}
  </div>`;
}

function batchCardHtml(b: Batch): string {
  const c = b.counts;
  const settled = c.done + c.failed;
  const pct = b.total ? Math.round((settled / b.total) * 100) : 0;
  const active = batchActive(b) > 0 && !b.paused && !b.cancelled_flag;
  const expanded = !!state.expandedBatches[b.id];
  const detail = state.batchDetails[b.id];
  const rows = expanded && detail ? detail.tracks.map(batchTrackRow).join("") : "";
  const countLine = `${c.done}/${b.total} done${c.failed ? ` · ${c.failed} failed` : ""}${c.downloading ? ` · ${c.downloading} downloading` : ""}${b.paused ? " · paused" : ""}${b.cancelled_flag ? " · cancelled" : ""}`;
  const pauseBtn = b.cancelled_flag
    ? ""
    : `<button data-act="pauseBatch" data-id="${esc(b.id)}" data-paused="${b.paused ? "0" : "1"}" style="background:#181c17;border:1px solid #272c25;border-radius:12px;color:#99a091;font:600 10.5px 'JetBrains Mono';padding:7px 13px;cursor:pointer;transition:border-color .15s,color .15s">${b.paused ? "resume" : "pause"}</button>`;
  const cancelBtn = b.cancelled_flag
    ? ""
    : `<button data-act="cancelBatch" data-id="${esc(b.id)}" style="background:none;border:1px solid #272c25;border-radius:12px;color:#5d6456;font:600 10.5px 'JetBrains Mono';padding:7px 13px;cursor:pointer;transition:border-color .15s,color .15s">cancel</button>`;
  return `<section style="background:#111411;border:1px solid #22261f;border-radius:20px;margin-bottom:14px;overflow:hidden">
    <div style="display:flex;align-items:center;gap:14px;padding:15px 18px 13px">
      <div style="flex:1;min-width:0">
        <div style="display:flex;align-items:center;gap:9px;flex-wrap:wrap">
          <span style="font:600 14px 'Instrument Sans'">${esc(b.name)}</span>
          <span style="font:600 8.5px 'JetBrains Mono';letter-spacing:.1em;padding:2px 8px;border-radius:10px;border:1px solid #272c25;color:#99a091">${originLabel(b.origin)}</span>
          ${b.owner ? `<span style="font:400 10.5px 'JetBrains Mono';color:#5d6456">${esc(b.owner)}</span>` : ""}
        </div>
        <div style="display:flex;align-items:center;gap:10px;margin-top:9px">
          <div style="flex:1;height:4px;background:#272c25;border-radius:4px;overflow:hidden"><div style="height:100%;width:${pct}%;background:#e9ede3;border-radius:4px;transition:width .6s cubic-bezier(.2,.8,.2,1)"></div></div>
          <span style="font:400 10.5px 'JetBrains Mono';color:#99a091;flex-shrink:0">${countLine}</span>
        </div>
      </div>
      <div style="display:flex;gap:8px;flex-shrink:0">${active || b.paused ? pauseBtn : ""}${cancelBtn}</div>
    </div>
    ${rows}
    <button data-act="toggleBatch" data-id="${esc(b.id)}" style="width:100%;background:#0e100d;border:none;border-top:1px solid #1a1e19;color:#5d6456;font:500 10.5px 'JetBrains Mono';padding:9px;cursor:pointer;transition:color .15s">${expanded ? "hide tracks ▴" : `show ${b.total} tracks ▾`}</button>
  </section>`;
}

function bulkPageHtml(): string {
  const bs = state.batches;
  const sum = (k: keyof Batch["counts"]): number => bs.reduce((n, b) => n + b.counts[k], 0);
  const cards: [string, string, string][] = [
    ["Batches", String(bs.length), "playlists / lists"],
    ["Queued", String(sum("queued")), "waiting"],
    ["Downloading", String(sum("downloading")), "in flight"],
    ["Done", String(sum("done")), "verified"],
    ["Failed", String(sum("failed")), "no source"],
  ];
  const stats = `<div style="display:grid;grid-template-columns:repeat(${state.isMobile ? 2 : 5},1fr);gap:12px;margin-bottom:14px">${cards
    .map(
      ([label, value, sub]) => `<div style="background:#111411;border:1px solid #22261f;border-radius:18px;padding:14px 16px">
        <div style="font:600 9.5px 'JetBrains Mono';letter-spacing:.14em;text-transform:uppercase;color:#5d6456">${label}</div>
        <div style="font:800 27px 'Doto';margin-top:8px;line-height:1;color:#e9ede3">${esc(value)}</div>
        <div style="font:400 10.5px 'JetBrains Mono';color:#5d6456;margin-top:7px">${sub}</div>
      </div>`,
    )
    .join("")}</div>`;
  const batchCards = bs.map(batchCardHtml).join("");
  const singles = state.inflight.filter((r) => !r.batch_id);
  const singlesHtml = singles.length
    ? `<section style="background:#111411;border:1px solid #22261f;border-radius:20px;overflow:hidden">
        <div style="display:flex;align-items:center;gap:10px;padding:15px 18px 12px"><span style="font:600 14px 'Instrument Sans'">Individual requests</span><span style="font:400 10.5px 'JetBrains Mono';color:#5d6456">search · explo · cli · listening</span></div>
        ${singles
          .map(
            (r) => `<div data-act="openDetail" data-id="${esc(r.id)}" class="hrow" style="display:flex;align-items:center;gap:11px;padding:9px 18px;border-top:1px solid #1a1e19;cursor:pointer;transition:background .12s">
          <span style="width:11px;height:11px;border:${r.status === "queued" ? "1.5px dashed #5d6456" : "1.5px solid #272c25;border-top-color:#e9ede3;animation:spin .8s linear infinite"};border-radius:50%;flex-shrink:0"></span>
          <span style="flex:1;min-width:0;font:500 12.5px 'Instrument Sans';overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.query)}</span>
          <span style="font:500 10px 'JetBrains Mono';color:#99a091;padding:2px 8px;background:#181c17;border-radius:10px;flex-shrink:0">via ${esc(r.requested_via)}</span>
        </div>`,
          )
          .join("")}
      </section>`
    : "";
  const empty =
    !bs.length && !singles.length
      ? `<div style="background:#111411;border:1px dashed #272c25;border-radius:20px;padding:48px 20px;text-align:center">
        <div style="font:600 14px 'Instrument Sans';color:#99a091">Nothing downloading</div>
        <div style="font:400 12px 'Instrument Sans';color:#5d6456;margin-top:6px">Paste a Spotify / YouTube playlist link or a track list to queue in bulk.</div>
      </div>`
      : "";
  const pad = state.isMobile ? "16px 16px 96px" : "22px 28px 72px";
  return `<main style="max-width:1560px;margin:0 auto;padding:${pad};animation:fadein .25s ease">
    <div style="display:flex;align-items:baseline;gap:14px;margin-bottom:16px;flex-wrap:wrap">
      <button data-act="gotoConsole" style="background:#181c17;border:1px solid #272c25;border-radius:12px;color:#99a091;font:600 11px 'JetBrains Mono';padding:6px 12px;cursor:pointer">← console</button>
      <h1 style="margin:0;font:600 18px 'Instrument Sans';letter-spacing:-.01em">Downloads</h1>
      <p style="margin:0;font:400 12.5px 'Instrument Sans';color:#99a091">Every batch + request in flight.</p>
      <button data-act="openBulkPanel" style="margin-left:auto;background:#e9ede3;border:none;border-radius:14px;color:#14170f;font:600 12px 'Instrument Sans';padding:8px 16px;cursor:pointer">+ New batch</button>
    </div>
    ${stats}${batchCards}${singlesHtml}${empty}
  </main>`;
}

function bulkPanelHtml(): string {
  const open = state.bulkPanel;
  const summaries = state.batches
    .slice(0, 6)
    .map((b) => {
      const settled = b.counts.done + b.counts.failed;
      const pct = b.total ? Math.round((settled / b.total) * 100) : 0;
      return `<div style="background:#0c0e0c;border:1px solid #272c25;border-radius:14px;padding:12px 14px;margin-bottom:10px">
        <div style="display:flex;align-items:center;gap:8px"><span style="font:600 12.5px 'Instrument Sans';flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(b.name)}</span><span style="font:600 8.5px 'JetBrains Mono';letter-spacing:.1em;padding:2px 7px;border-radius:10px;border:1px solid #272c25;color:#99a091;flex-shrink:0">${originLabel(b.origin)}</span></div>
        <div style="display:flex;align-items:center;gap:10px;margin-top:9px"><div style="flex:1;height:4px;background:#272c25;border-radius:4px;overflow:hidden"><div style="height:100%;width:${pct}%;background:#e9ede3;border-radius:4px"></div></div><span style="font:400 10px 'JetBrains Mono';color:#99a091;flex-shrink:0">${b.counts.done}/${b.total}</span></div>
      </div>`;
    })
    .join("");
  const inner = `<div style="padding:20px 22px;border-bottom:1px solid #1d211c;display:flex;align-items:flex-start;justify-content:space-between;gap:12px">
      <div><div style="font:600 17px 'Instrument Sans';letter-spacing:-.01em">Bulk downloads</div><div style="font:500 10.5px 'JetBrains Mono';color:#99a091;margin-top:7px">Playlist link or one track per line.</div></div>
      <button data-act="closeBulkPanel" style="background:#181c17;border:1px solid #272c25;border-radius:10px;color:#99a091;cursor:pointer;padding:6px 9px;flex-shrink:0;display:inline-flex"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"></path></svg></button>
    </div>
    <div style="flex:1;overflow-y:auto;padding:18px 22px">
      <textarea data-inp="bulk" rows="5" placeholder="https://open.spotify.com/playlist/…&#10;or paste tracks, one per line" style="width:100%;background:#0c0e0c;border:1px solid #272c25;border-radius:14px;color:#e9ede3;font:400 12.5px 'Instrument Sans';padding:12px 14px;resize:vertical;line-height:1.5">${esc(state.bulkText)}</textarea>
      <button data-act="submitBulk" ${state.bulkBusy ? "disabled" : ""} style="width:100%;margin-top:10px;background:${state.bulkBusy ? "#272c25" : "#e9ede3"};border:none;border-radius:14px;color:${state.bulkBusy ? "#99a091" : "#14170f"};font:600 13px 'Instrument Sans';padding:12px;cursor:${state.bulkBusy ? "default" : "pointer"};display:flex;align-items:center;justify-content:center;gap:8px">${state.bulkBusy ? `<span style="width:12px;height:12px;border:1.5px solid rgba(20,23,15,.3);border-top-color:#14170f;border-radius:50%;animation:spin .8s linear infinite"></span>Resolving…` : "Start batch"}</button>
      <div style="font:600 9.5px 'JetBrains Mono';letter-spacing:.14em;text-transform:uppercase;color:#5d6456;margin:18px 0 10px">Batches</div>
      ${summaries || `<div style="padding:16px 0;text-align:center;font:400 12px 'Instrument Sans';color:#5d6456">No batches yet.</div>`}
    </div>
    <div style="padding:16px 22px;border-top:1px solid #1d211c"><button data-act="gotoBulk" style="width:100%;background:#181c17;border:1px solid #272c25;border-radius:14px;color:#e9ede3;font:600 13px 'Instrument Sans';padding:12px;cursor:pointer">Open downloads page</button></div>`;
  return `<div data-act="closeBulkPanel" style="position:fixed;inset:0;background:rgba(5,7,5,.6);z-index:54;transition:opacity .28s;opacity:${open ? "1" : "0"};pointer-events:${open ? "auto" : "none"}"></div>
    <aside style="position:fixed;top:0;right:0;bottom:0;width:min(440px,94vw);background:#111411;border-left:1px solid #22261f;z-index:55;transform:${open ? "translateX(0)" : "translateX(103%)"};transition:transform .3s cubic-bezier(.3,.8,.3,1);box-shadow:-24px 0 60px rgba(0,0,0,.55);display:flex;flex-direction:column">${inner}</aside>`;
}

// ---------- review tab (user-assisted retry / re-download) ----------
function reviewStatCardsHtml(): string {
  const s = state.reviewStats;
  // tone: "" = default dark, "light" = inverted, "amber" = attention/warning
  type Card = [string, string, string, "" | "light" | "amber"];
  const cards: Card[] = s
    ? [
        [
          "Needs review",
          String(s.needs_review),
          `${s.failed} failed · ${s.lossy} lossy · ${s.needs_confirm} confirm`,
          "light",
        ],
        ["Failed", String(s.failed), "no source produced a file", ""],
        ["Lossy grabs", String(s.lossy), "FLAC upgrade available", ""],
        ["Needs confirm", String(s.needs_confirm), "fingerprint didn’t match", "amber"],
        ["Resolved", String(s.resolved), "via review", ""],
      ]
    : Array<Card>(5).fill(["—", "—", "", ""]);
  return `<div style="display:grid;grid-template-columns:repeat(${state.isMobile ? 2 : 5},1fr);gap:12px;margin-bottom:16px">${cards
    .map(([label, value, sub, tone]) => {
      const light = tone === "light";
      const amber = tone === "amber";
      const bg = light ? "#dfe4d6" : "#111411";
      const bd = light ? "#dfe4d6" : amber ? WARN_BD : "#22261f";
      const lc = light ? "#4d5346" : amber ? WARN : "#5d6456";
      const vc = light ? "#14170f" : amber ? WARN : "#e9ede3";
      return `<div style="background:${bg};border:1px solid ${bd};border-radius:18px;padding:14px 16px">
        <div style="font:600 9.5px 'JetBrains Mono';letter-spacing:.14em;text-transform:uppercase;color:${lc}">${esc(label)}</div>
        <div style="font:800 27px 'Doto';margin-top:8px;line-height:1;color:${vc}">${esc(value)}</div>
        <div style="font:400 10.5px 'JetBrains Mono';color:${lc};margin-top:7px">${esc(sub)}</div>
      </div>`;
    })
    .join("")}</div>`;
}

function reviewCandRowHtml(itemId: string, expected: number | null, c: ReviewCandidate): string {
  const sel = state.reviewSel[itemId] === c.id;
  const d = expected != null && c.duration_s != null ? c.duration_s - expected : null;
  const delta = d == null || d === 0 ? "" : `${d > 0 ? "+" : "−"}${Math.abs(d)}s`;
  const deltaColor = d != null && Math.abs(d) > 8 ? "#ff6b58" : "#5d6456";
  const matchColor = c.match >= 90 ? "#e9ede3" : c.match >= 75 ? "#99a091" : "#5d6456";
  const rowBg = sel ? "rgba(233,237,227,.05)" : "#0c0e0c";
  const rowBorder = sel ? "#99a091" : "#272c25";
  const radioBorder = sel ? "#e9ede3" : "#39402f";
  const dotBg = sel ? "#e9ede3" : "transparent";
  const qBg = c.lossless ? "rgba(233,237,227,.09)" : "transparent";
  const qColor = c.lossless ? "#e9ede3" : "#7c8371";
  const qBorder = c.lossless ? "transparent" : "#272c25";
  const flags = c.album ? esc(c.album) : "";
  return `<div data-act="selectCand" data-rid="${esc(itemId)}" data-cid="${esc(c.id)}" style="display:flex;align-items:center;gap:11px;background:${rowBg};border:1px solid ${rowBorder};border-radius:14px;padding:11px 14px;margin-bottom:8px;cursor:pointer;transition:border-color .15s,background .15s">
    <span style="width:16px;height:16px;border-radius:50%;border:1.5px solid ${radioBorder};display:inline-flex;align-items:center;justify-content:center;flex-shrink:0;transition:border-color .15s"><span style="width:8px;height:8px;border-radius:50%;background:${dotBg};transition:background .15s"></span></span>
    <span style="font:500 10px 'JetBrains Mono';color:#99a091;padding:2px 8px;background:#181c17;border-radius:10px;flex-shrink:0">${esc(c.source)}</span>
    <span style="font:800 11px 'Doto';color:${tierColor(c.tier)};flex-shrink:0">T${c.tier}</span>
    <div style="flex:1;min-width:0">
      <div style="font:500 12.5px 'Instrument Sans';color:#e9ede3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(c.title)} <span style="color:#5d6456">· ${esc(c.artist)}</span></div>
      ${flags ? `<div style="font:400 9.5px 'JetBrains Mono';color:#5d6456;margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${flags}</div>` : ""}
    </div>
    <span style="font:600 8.5px 'JetBrains Mono';letter-spacing:.1em;padding:2px 7px;border-radius:10px;background:${qBg};color:${qColor};border:1px solid ${qBorder};flex-shrink:0">${esc(c.quality_label)}</span>
    <span style="font:500 10px 'JetBrains Mono';color:#99a091;flex-shrink:0;width:34px;text-align:right">${dur(c.duration_s)}</span>
    <span style="font:500 10px 'JetBrains Mono';color:${deltaColor};flex-shrink:0;width:32px">${delta}</span>
    <span style="font:400 10px 'JetBrains Mono';color:#5d6456;flex-shrink:0;width:46px;text-align:right">${bytes(c.size_bytes)}</span>
    ${
      c.preview_url || c.streamable
        ? playBtn(
            `cand:${itemId}:${c.id}`,
            c.preview_url ?? api.reviewCandidateStreamUrl(c.source, c.ref),
            26,
          )
        : ""
    }
    <span style="font:800 16px 'Doto';color:${matchColor};width:46px;text-align:right;flex-shrink:0">${c.match}%</span>
  </div>`;
}

// Advisory AcoustID fingerprint hint on an expanded lossy item (needs a current
// file). Prefer a freshly returned hint, fall back to the item's polled acoustid_*.
// Wrong-version / different-title reads render as a warning chip.
function fingerprintBlock(item: ReviewItem): string {
  if (item.kind !== "lossy" || !item.has_audio) return "";
  const id = item.id;
  const identifying = !!state.reviewIdentifying[id];
  const identified = id in state.reviewHint; // identify has completed at least once
  const hint = state.reviewHint[id];
  const artist = hint?.artist ?? item.acoustid_artist;
  const title = hint?.title ?? item.acoustid_title;
  let hintLine = "";
  if (title) {
    const wanted = (item.title ?? item.query ?? "").trim().toLowerCase();
    const got = title.trim().toLowerCase();
    const versionTag = /\b(acoustic|instrumental|live|remix)\b/i.test(title);
    const wrong = versionTag || (!!wanted && !!got && wanted !== got);
    const text = `fingerprint reads this as: ${esc(artist ?? "unknown")} — ${esc(title)}`;
    hintLine = wrong
      ? `<div style="display:inline-flex;align-items:center;gap:7px;background:${WARN_BG};border:1px solid ${WARN_BD};border-radius:12px;padding:6px 11px;margin-top:8px;font:500 10.5px 'JetBrains Mono';color:${WARN}"><span style="flex-shrink:0">⚠</span>${text}</div>`
      : `<div style="margin-top:8px;font:400 10.5px 'JetBrains Mono';color:#5d6456">${text}</div>`;
  } else if (identified) {
    hintLine = `<div style="margin-top:8px;font:400 10.5px 'JetBrains Mono';color:#5d6456">No fingerprint match — using metadata score only.</div>`;
  }
  const btn = `<button data-act="identifyReview" data-id="${esc(id)}" ${identifying ? "disabled" : ""} style="display:inline-flex;align-items:center;gap:7px;background:#181c17;border:1px solid #272c25;border-radius:12px;color:#99a091;font:600 10.5px 'JetBrains Mono';padding:7px 13px;cursor:${identifying ? "default" : "pointer"};transition:border-color .15s,color .15s">${
    identifying
      ? `<span style="width:11px;height:11px;border:1.5px solid #272c25;border-top-color:#e9ede3;border-radius:50%;animation:spin .8s linear infinite"></span>identifying…`
      : "Identify"
  }</button>`;
  // When the fingerprint named the track, offer to re-search using that term —
  // the fastest way to find the right recording when the original query was off.
  const searchHint = title
    ? `<button data-act="searchHint" data-id="${esc(id)}" style="display:inline-flex;align-items:center;gap:6px;background:none;border:1px solid #272c25;border-radius:12px;color:#99a091;font:600 10.5px 'JetBrains Mono';padding:7px 13px;margin-left:8px;cursor:pointer;transition:border-color .15s,color .15s">search this →</button>`
    : "";
  return `<div style="margin-bottom:12px">${btn}${searchHint}${hintLine}</div>`;
}

// The re-search term the fingerprint hint suggests ("Artist Title"), or null.
function hintTerm(item: ReviewItem): string | null {
  const hint = state.reviewHint[item.id];
  const artist = hint?.artist ?? item.acoustid_artist;
  const title = hint?.title ?? item.acoustid_title;
  if (!title) return null;
  return `${artist ? `${artist} ` : ""}${title}`.trim();
}

// Provenance + chronology for a resolve decision: who requested the song, the
// search metadata it carried (artist/title/ISRC/wanted duration), and the ordered
// attempt trail (which sources were tried + how each fared). Fetched on expand.
function provenanceBlock(item: ReviewItem): string {
  const d = state.reviewDetail[item.id];
  const chip = (label: string, val: string, color: string = C.muted) =>
    `<span style="font:400 10px 'JetBrains Mono';color:${C.faint}">${label}</span><span style="font:500 10px 'JetBrains Mono';color:${color};margin-left:5px">${val}</span>`;

  const artist = d?.artist ?? null;
  const title = d?.title ?? item.title ?? null;
  const isrc = d?.isrc ?? null;
  const searched =
    artist && title ? `${esc(artist)} — ${esc(title)}` : title ? esc(title) : esc(item.query);

  const idLine = `<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:8px">
    <span style="font:600 12px 'Instrument Sans';color:${C.text}">${searched}</span>
    ${isrc ? `<span style="font:600 9px 'JetBrains Mono';letter-spacing:.06em;padding:2px 7px;border-radius:8px;background:rgba(153,160,145,.09);border:1px solid #2c322a;color:${C.muted}">ISRC ${esc(isrc)}</span>` : ""}
    ${item.duration_s != null ? chip("want", dur(item.duration_s)) : ""}
  </div>`;

  const originLine = `<div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
    ${chip("requested via", esc(item.requested_via), C.muted)}
    ${d?.batch_id ? chip("batch", esc(d.batch_id.slice(0, 8)), C.muted) : ""}
    ${d?.created_at ? chip("added", relTime(d.created_at)) : ""}
    ${d?.source_won ? chip("won by", `${esc(d.source_won)}${d.tier_won != null ? ` · T${d.tier_won}` : ""}`, C.light) : ""}
  </div>`;

  let trail = "";
  if (d && d.attempts.length) {
    const rows = d.attempts
      .map((a) => {
        const mc = a.ok ? "#9fce8a" : "#ff6b58";
        return `<div style="display:flex;align-items:baseline;gap:9px;font:400 10.5px 'JetBrains Mono';line-height:1.75">
        <span style="color:${mc};width:9px;flex-shrink:0">${a.ok ? "✓" : "✗"}</span>
        <span style="color:${C.faint};width:15px;flex-shrink:0">T${a.tier}</span>
        <span style="color:${a.ok ? C.light : C.muted};width:76px;flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(a.source)}</span>
        <span style="color:${C.faint};overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(a.detail)}</span>
      </div>`;
      })
      .join("");
    trail = `<div style="margin-top:10px;padding-top:10px;border-top:1px solid #1a1e19">
      <div style="font:600 8.5px 'JetBrains Mono';letter-spacing:.12em;color:${C.dim};margin-bottom:6px">CHRONOLOGY · ${d.attempts.length} ATTEMPT${d.attempts.length === 1 ? "" : "S"}${d.finished_at ? ` · ${relTime(d.finished_at)}` : ""}</div>
      ${rows}
    </div>`;
  } else if (!d) {
    trail = `<div style="font:400 10px 'JetBrains Mono';color:${C.dim};margin-top:6px">loading history…</div>`;
  }

  return `<div style="background:#0c0e0c;border:1px solid #22261f;border-radius:14px;padding:12px 14px;margin-bottom:12px">${idLine}${originLine}${trail}</div>`;
}

function reviewItemHtml(item: ReviewItem): string {
  const id = item.id;
  const kind = item.kind;
  const failed = kind === "failed";
  const confirm = kind === "needs_confirm";
  const expanded = !!state.reviewOpen[id];
  const searching = !!state.reviewSearching[id];
  const downloading = !!state.reviewDownloading[id];
  const cands = state.reviewCands[id] ?? [];
  const selCand = cands.find((c) => c.id === state.reviewSel[id]);
  const q = state.reviewQ[id] != null ? state.reviewQ[id] : item.query;
  const matchPct = item.match_similarity != null ? Math.round(item.match_similarity * 100) : null;
  const reasonLabel = failed ? "FAILED" : confirm ? "CONFIRM" : "LOSSY";
  const reasonColor = failed ? "#ff6b58" : confirm ? WARN : "#99a091";
  const reasonBg = failed ? "rgba(255,76,56,.08)" : confirm ? WARN_BG : "rgba(153,160,145,.08)";
  const reasonBorder = failed ? "rgba(255,76,56,.35)" : confirm ? WARN_BD : "#39402f";
  const sub = failed
    ? `${esc(item.reason ?? "all tiers missed")} · via ${esc(item.requested_via)}`
    : confirm
      ? `⚠ doesn’t match current file${matchPct != null ? ` · ${matchPct}%` : ""}`
      : `${esc(item.quality_label ?? "lossy")} · ${bytes(item.size_bytes)}${item.source_won ? ` · won by ${esc(item.source_won)}` : ""}`;
  const cardBorder = expanded ? "#39402f" : "#22261f";
  const current =
    !failed && item.quality_label
      ? `current file · ${esc(item.quality_label)} · ${bytes(item.size_bytes)}`
      : "";

  const head = `<div data-act="toggleReview" data-id="${esc(id)}" style="display:flex;align-items:center;gap:13px;padding:14px 18px;cursor:pointer;transition:background .12s">
      <span style="font:600 8.5px 'JetBrains Mono';letter-spacing:.1em;padding:3px 9px;border-radius:10px;background:${reasonBg};color:${reasonColor};border:1px solid ${reasonBorder};flex-shrink:0">${reasonLabel}</span>
      <div style="flex:1;min-width:0">
        <div style="font:600 13.5px 'Instrument Sans';overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(item.query)}</div>
        <div style="font:400 10.5px 'JetBrains Mono';color:#5d6456;margin-top:3px">${sub}</div>
      </div>
      ${
        downloading
          ? `<span style="font:600 10px 'JetBrains Mono';color:#e9ede3;padding:5px 11px;border-radius:14px;background:rgba(233,237,227,.09);flex-shrink:0">✓ re-queued</span>`
          : `<span style="font:500 10.5px 'JetBrains Mono';color:#99a091;flex-shrink:0">${cands.length} candidates</span><span style="color:#5d6456;font-size:10px;flex-shrink:0">${expanded ? "▴" : "▾"}</span>`
      }
    </div>`;

  if (!expanded) {
    return `<section style="background:#111411;border:1px solid ${cardBorder};border-radius:20px;margin-bottom:12px;overflow:hidden;transition:border-color .2s">${head}</section>`;
  }

  // needs_confirm: parked by the fingerprint gate — warn + play the current file +
  // "Replace anyway" (force the same candidate) / "Keep current" (dismiss).
  if (confirm) {
    const lastCand = state.reviewLastCand[id];
    // The candidate is always stored server-side for a needs_confirm item, so
    // "replace anyway" is available regardless of client state — only gated while
    // a re-download is already in flight.
    const canForce = !downloading;
    const pctStr = matchPct != null ? `${matchPct}%` : "an unknown amount";
    const cBody = `<div style="border-top:1px solid #1a1e19;padding:14px 18px 16px;animation:fadein .2s ease">
      ${provenanceBlock(item)}
      <div style="display:flex;gap:11px;background:${WARN_BG};border:1px solid ${WARN_BD};border-radius:14px;padding:13px 15px;margin-bottom:12px">
        <span style="font:600 14px 'Instrument Sans';color:${WARN};flex-shrink:0;line-height:1.3">⚠</span>
        <div style="font:400 12px 'Instrument Sans';color:#e9ede3;line-height:1.5">Doesn’t match your current file — <span style="color:${WARN};font-weight:600">${pctStr}</span>. This may be a different version or song.${lastCand ? ` <span style="color:#5d6456">(candidate: ${esc(lastCand.source)} · ${esc(lastCand.quality_label)})</span>` : ""}</div>
      </div>
      ${currentFilePlayer(item)}
      <div style="display:flex;align-items:center;gap:10px;margin-top:12px">
        <button data-act="replaceAnyway" data-id="${esc(id)}" ${canForce ? "" : "disabled"} style="background:${canForce ? WARN : "#181c17"};border:none;border-radius:14px;color:${canForce ? "#14170f" : "#5d6456"};font:600 12.5px 'Instrument Sans';padding:11px 18px;cursor:${canForce ? "pointer" : "default"};transition:.15s">Replace anyway</button>
        <button data-act="dismissReview" data-id="${esc(id)}" style="background:none;border:1px solid #272c25;border-radius:14px;color:#5d6456;font:600 11px 'JetBrains Mono';padding:11px 15px;cursor:pointer;transition:border-color .15s,color .15s">keep current</button>
        <span style="margin-left:auto;font:400 10px 'JetBrains Mono';color:#464d40">replace overrides the fingerprint gate</span>
      </div>
    </div>`;
    return `<section style="background:#111411;border:1px solid ${cardBorder};border-radius:20px;margin-bottom:12px;overflow:hidden;transition:border-color .2s">${head}${cBody}</section>`;
  }

  const dlDisabled = !selCand;
  const dlBg = selCand ? "#e9ede3" : "#181c17";
  const dlColor = selCand ? "#14170f" : "#5d6456";
  const dlCursor = selCand ? "pointer" : "default";
  const dlLabel = selCand
    ? `Download from ${esc(selCand.source)} · ${esc(selCand.quality_label)}`
    : "Select a match to download";
  const body = `<div style="border-top:1px solid #1a1e19;padding:14px 18px 16px;animation:fadein .2s ease">
      ${provenanceBlock(item)}
      <div style="display:flex;align-items:center;gap:9px;margin-bottom:12px">
        <div style="position:relative;flex:1;display:flex;align-items:center">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#5d6456" stroke-width="2" stroke-linecap="round" style="position:absolute;left:11px;pointer-events:none"><circle cx="11" cy="11" r="7"></circle><path d="M21 21l-4.3-4.3"></path></svg>
          <input data-inp="reviewq" data-id="${esc(id)}" value="${esc(q)}" style="width:100%;background:#0c0e0c;border:1px solid #272c25;border-radius:12px;color:#e9ede3;font:400 12.5px 'Instrument Sans';padding:8px 12px 8px 32px"/>
        </div>
        <button data-act="rescanReview" data-id="${esc(id)}" style="display:inline-flex;align-items:center;gap:7px;background:#181c17;border:1px solid #272c25;border-radius:12px;color:#99a091;font:600 10.5px 'JetBrains Mono';padding:8px 14px;cursor:pointer;flex-shrink:0;transition:border-color .15s,color .15s">search again</button>
      </div>
      <div style="display:flex;align-items:center;gap:16px;font:400 10.5px 'JetBrains Mono';color:#5d6456;margin-bottom:10px;padding:0 2px">
        <span>expected · ${dur(item.duration_s)}</span>
        ${current ? `<span style="color:#99a091">${current}</span>` : ""}
      </div>
      ${currentFilePlayer(item)}
      ${fingerprintBlock(item)}
      ${
        searching
          ? `<div style="display:flex;align-items:center;gap:10px;background:#0c0e0c;border:1px solid #272c25;border-radius:14px;padding:16px 14px;font:400 11.5px 'JetBrains Mono';color:#99a091"><span style="width:12px;height:12px;border:1.5px solid #272c25;border-top-color:#e9ede3;border-radius:50%;animation:spin .8s linear infinite;flex-shrink:0"></span>Searching all sources…</div>`
          : cands.length
            ? cands.map((c) => reviewCandRowHtml(id, item.duration_s, c)).join("")
            : `<div style="background:#0c0e0c;border:1px solid #272c25;border-radius:14px;padding:16px 14px;font:400 11.5px 'JetBrains Mono';color:#5d6456;text-align:center">No candidates yet — hit “search again”.</div>`
      }
      <div style="display:flex;align-items:center;gap:10px;margin-top:12px">
        <button data-act="downloadCand" data-id="${esc(id)}" ${dlDisabled ? "disabled" : ""} style="background:${dlBg};border:none;border-radius:14px;color:${dlColor};font:600 12.5px 'Instrument Sans';padding:11px 18px;cursor:${dlCursor};transition:.15s">${dlLabel}</button>
        <button data-act="dismissReview" data-id="${esc(id)}" style="background:none;border:1px solid #272c25;border-radius:14px;color:#5d6456;font:600 11px 'JetBrains Mono';padding:11px 15px;cursor:pointer;transition:border-color .15s,color .15s">${failed ? "dismiss" : "keep current"}</button>
        <span style="margin-left:auto;font:400 10px 'JetBrains Mono';color:#464d40">match % = title · artist · duration similarity</span>
      </div>
    </div>`;

  return `<section style="background:#111411;border:1px solid ${cardBorder};border-radius:20px;margin-bottom:12px;overflow:hidden;transition:border-color .2s">${head}${body}</section>`;
}

// ---------- shared feed (Phase C) ----------
// Compact latency: sub-minute as "3.2s", longer as "1m 04s". Serves both chips.
function fmtLatency(s: number): string {
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${String(Math.round(s % 60)).padStart(2, "0")}s`;
}

// One latency chip. `pending` = the mark hasn't landed yet (e.g. FLAC still racing
// after the playable rip) — render a muted, softly-pulsing placeholder instead of a
// value. `absent` = never applicable (a failed request) — render nothing.
function latencyChip(label: string, seconds: number | null, pending: boolean): string {
  if (seconds == null && !pending) return "";
  const dot = (col: string, anim: boolean): string =>
    `<span style="width:6px;height:6px;border-radius:50%;background:${col};flex-shrink:0${anim ? ";animation:pulse 1.6s ease-in-out infinite" : ""}"></span>`;
  if (seconds == null) {
    return `<span style="display:inline-flex;align-items:center;gap:6px;background:#131612;border:1px solid #22261f;border-radius:11px;padding:4px 10px;font:600 10.5px 'JetBrains Mono';color:#5d6456">${dot("#5d6456", true)}${label} …</span>`;
  }
  return `<span style="display:inline-flex;align-items:center;gap:6px;background:rgba(233,237,227,.06);border:1px solid #272c25;border-radius:11px;padding:4px 10px;font:600 10.5px 'JetBrains Mono';color:#aeb5a3">${dot("#7c8371", false)}${label} ${fmtLatency(seconds)}</span>`;
}

// A stable per-user accent so avatars/initials read as the same person across rows.
function userHue(id: string | null): number {
  const key = id ?? "service";
  let h = 0;
  for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) % 360;
  return h;
}

function feedAvatarHtml(row: FeedRow): string {
  const name = row.user_name ?? (row.user_id === "service" ? "service" : "unknown");
  const hue = userHue(row.user_id);
  if (row.user_avatar) {
    return `<img src="${esc(row.user_avatar)}" alt="" style="width:28px;height:28px;border-radius:50%;object-fit:cover;flex-shrink:0" />`;
  }
  const initial = esc((name[0] ?? "?").toUpperCase());
  return `<span style="width:28px;height:28px;border-radius:50%;flex-shrink:0;display:inline-flex;align-items:center;justify-content:center;background:hsl(${hue} 30% 22%);color:hsl(${hue} 55% 78%);font:700 12px 'Instrument Sans'">${initial}</span>`;
}

function feedTrackLabel(row: FeedRow): string {
  const artist = (row.artist ?? "").trim();
  const title = (row.title ?? "").trim();
  if (title) return artist ? `${artist} — ${title}` : title;
  return row.query;
}

function feedRowHtml(row: FeedRow): string {
  const name = esc(row.user_name ?? (row.user_id === "service" ? "service" : "someone"));
  const meta = statusMeta(row.status);
  const failed = row.status === "failed";
  // playable chip: shows once first_playable_at lands (any successful grab).
  const playable = latencyChip("playable in", row.ttf_playable_s, false);
  // FLAC chip: value once final_flac_at lands; while playable exists but FLAC
  // doesn't yet, show the pending placeholder (the endgoal tier is still racing).
  const flacPending =
    !failed && row.ttf_final_s == null && (row.ttf_playable_s != null || row.status === "done");
  const flac = failed ? "" : latencyChip("FLAC in", row.ttf_final_s, flacPending);
  const src =
    row.source_won && row.status === "done"
      ? `<span style="font:600 10px 'JetBrains Mono';color:${tierColor(row.tier_won)}">${esc(row.source_won)}</span>`
      : "";
  const statusPill = failed
    ? `<span style="font:600 10px 'JetBrains Mono';color:${C.bad}">failed</span>`
    : row.status !== "done"
      ? `<span style="font:600 10px 'JetBrains Mono';color:${meta.color}">${esc(row.status)}</span>`
      : "";
  const chips = [playable, flac].filter(Boolean).join("");
  return `<div style="display:flex;align-items:center;gap:12px;padding:12px 16px;border-top:1px solid #1a1e19">
      ${feedAvatarHtml(row)}
      <div style="min-width:0;flex:1">
        <div style="font:600 13px 'Instrument Sans';color:#e9ede3;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(feedTrackLabel(row))}</div>
        <div style="display:flex;align-items:center;gap:8px;margin-top:2px;font:500 11px 'Instrument Sans';color:#99a091">
          <span style="color:#aeb5a3">${name}</span>
          <span style="color:#3a4035">·</span>
          <span style="color:#5d6456">${esc(relTime(row.finished_at ?? row.created_at))}</span>
          ${src ? `<span style="color:#3a4035">·</span>${src}` : ""}
          ${statusPill ? `<span style="color:#3a4035">·</span>${statusPill}` : ""}
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:7px;flex-shrink:0;flex-wrap:wrap;justify-content:flex-end">${chips}</div>
    </div>`;
}

// Mobile: the shared feed as a card inside the Activity tab (capped; the desktop
// Feed page shows the full stream).
function mFeedHtml(): string {
  if (!state.feed.length) return "";
  const rows = state.feed.slice(0, 20).map(feedRowHtml).join("");
  return `<section style="background:#0e100d;border:1px solid #1d211c;border-radius:18px;overflow:hidden">
      <div style="padding:12px 16px;font:600 12px 'JetBrains Mono';letter-spacing:.04em;color:#99a091">SHARED FEED</div>
      ${rows}
    </section>`;
}

function feedPageHtml(): string {
  const rows = state.feed.map(feedRowHtml).join("");
  const body = state.feed.length
    ? `<div style="background:#0e100d;border:1px solid #1d211c;border-radius:20px;overflow:hidden">${rows}</div>`
    : `<div style="background:#111411;border:1px dashed #272c25;border-radius:20px;padding:48px 20px;text-align:center">
        <div style="font:600 14px 'Instrument Sans';color:#99a091">No activity yet</div>
        <div style="font:400 12px 'Instrument Sans';color:#5d6456;margin-top:6px">Every request lands here with who asked for it and how fast it was served.</div>
      </div>`;
  const pad = state.isMobile ? "16px 16px 96px" : "22px 28px 72px";
  return `<main style="max-width:1100px;margin:0 auto;padding:${pad};animation:fadein .25s ease">
    <div style="display:flex;align-items:baseline;gap:14px;margin-bottom:16px;flex-wrap:wrap">
      <h1 style="margin:0;font:600 18px 'Instrument Sans';letter-spacing:-.01em">Feed</h1>
      <p style="margin:0;font:400 12.5px 'Instrument Sans';color:#99a091">Everyone's requests, newest first — time to a playable file and time to the final FLAC.</p>
    </div>
    ${body}
  </main>`;
}

function reviewPageHtml(): string {
  const items = state.reviewItems.map(reviewItemHtml).join("");
  const empty = state.reviewItems.length
    ? ""
    : `<div style="background:#111411;border:1px dashed #272c25;border-radius:20px;padding:48px 20px;text-align:center">
        <div style="font:600 14px 'Instrument Sans';color:#99a091">Nothing to review</div>
        <div style="font:400 12px 'Instrument Sans';color:#5d6456;margin-top:6px">Failed requests and non-FLAC downloads land here for user-assisted retry.</div>
      </div>`;
  const pad = state.isMobile ? "16px 16px 96px" : "22px 28px 72px";
  return `<main style="max-width:1100px;margin:0 auto;padding:${pad};animation:fadein .25s ease">
    <div style="display:flex;align-items:baseline;gap:14px;margin-bottom:16px;flex-wrap:wrap">
      <h1 style="margin:0;font:600 18px 'Instrument Sans';letter-spacing:-.01em">Review</h1>
      <p style="margin:0;font:400 12.5px 'Instrument Sans';color:#99a091">Failed requests and lossy grabs. Compare candidates from every source, pick the closest match, re-download.</p>
    </div>
    ${reviewStatCardsHtml()}
    ${items}${empty}
  </main>`;
}

// Pure view: State -> HTML. The renderer reconciles it into the DOM (morph), so
// this can run every tick without wiping the page.
// Full-screen login gate shown when /api/me 401s (auth configured, no session).
function loginGateHtml(): string {
  const err = state.error
    ? `<div style="margin-top:14px;color:#ff4c38;font:500 12.5px 'Instrument Sans'">${esc(state.error)}</div>`
    : "";
  return `<div style="min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px">
    <div style="text-align:center;max-width:360px">
      <div style="display:flex;align-items:center;justify-content:center;gap:12px;margin-bottom:8px">
        <span style="width:10px;height:10px;border-radius:50%;background:#ff4c38"></span>
        <span style="font:800 26px 'Doto';letter-spacing:.08em">crate</span>
      </div>
      <div style="font:500 13px 'Instrument Sans';color:#99a091;margin-bottom:26px">Log in with Spotify to request and track FLAC downloads.</div>
      <a href="${api.loginUrl}" style="display:inline-flex;align-items:center;gap:10px;background:#1db954;color:#0a140c;font:700 14px 'Instrument Sans';padding:13px 22px;border-radius:24px;text-decoration:none">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2a10 10 0 100 20 10 10 0 000-20zm4.6 14.4a.6.6 0 01-.86.2c-2.35-1.44-5.3-1.76-8.8-.96a.62.62 0 11-.28-1.2c3.83-.88 7.1-.5 9.74 1.1.3.18.4.58.2.86zm1.23-2.74a.78.78 0 01-1.07.26c-2.69-1.65-6.79-2.13-9.97-1.17a.78.78 0 11-.45-1.5c3.63-1.09 8.15-.55 11.24 1.34.36.22.48.7.25 1.07zm.1-2.85C14.8 8.03 9.3 7.84 6.12 8.8a.94.94 0 11-.54-1.8c3.65-1.1 9.72-.89 13.55 1.38a.94.94 0 01-.96 1.62z"></path></svg>
        Log in with Spotify
      </a>
      ${err}
    </div>
  </div>`;
}

function view(): string {
  if (state.authRequired) return loginGateHtml();
  if (state.error) {
    return `<div style="max-width:600px;margin:80px auto;padding:24px;color:#ff4c38;font:500 14px 'Instrument Sans';text-align:center">Failed to load console: ${esc(state.error)}</div>`;
  }
  return _viewBody() + sourceDialogHtml();
}

function _viewBody(): string {
  if (state.page === "bulk") {
    return `${headerHtml()}${bulkPageHtml()}${drawerHtml()}${panelHtml()}${bulkPanelHtml()}${toastHtml()}`;
  }
  if (state.page === "review") {
    return `${headerHtml()}${reviewPageHtml()}${drawerHtml()}${panelHtml()}${bulkPanelHtml()}${toastHtml()}`;
  }
  if (state.page === "feed" && !state.isMobile) {
    return `${headerHtml()}${feedPageHtml()}${drawerHtml()}${panelHtml()}${bulkPanelHtml()}${toastHtml()}`;
  }
  return `${state.isMobile ? viewMobile() : viewDesktop()}${bulkPanelHtml()}`;
}

// render() = schedule a batched, reconciling repaint (safe to call on every tick).
const render = createRenderer(root, view);

// ---------- events ----------
async function openDetail(id: string): Promise<void> {
  state.selectedId = id;
  const row = state.history.find((r) => r.id === id);
  state.showLogs = row?.status === "failed";
  render();
  try {
    state.detail = await api.request(id);
    render();
  } catch {
    /* keep skeleton */
  }
}
function setStrategy(tier: string, strat: "all" | "uniform"): void {
  if (!state.layout) return;
  const t = state.layout.tiers.find((t) => String(t.tier) === tier);
  if (!t || t.strategy === strat) return;
  const next = {
    tiers: state.layout.tiers.map((x) => (String(x.tier) === tier ? { ...x, strategy: strat } : x)),
    disabled: state.layout.disabled,
  };
  void persistLayout(next);
}
async function savePanel(): Promise<void> {
  const name = state.panelSource;
  if (!name) return;
  const def = PANELDEF[name];
  const credentials: Record<string, string> = {};
  for (const [env] of def.creds) if (state.panelValues[env]) credentials[env] = state.panelValues[env];
  const settings: Record<string, unknown> = {};
  for (const [key, , type] of def.settings) {
    const v = state.panelValues[key];
    if (v != null && v !== "") settings[key] = type === "number" ? Number(v) : v;
  }
  state.panelSave = "saving";
  render();
  try {
    const updated = await api.putSource(name, { credentials, settings });
    state.sources[name] = updated;
    state.panelSave = "saved";
    state.panelValues = {};
    toast("Credentials saved — encrypted at rest");
  } catch (err) {
    state.panelSave = "dirty";
    toast(`Save failed: ${(err as Error).message}`);
  }
  render();
}
// Test credentials by downloading a known song through this source (server-side).
async function validatePanel(name: string): Promise<void> {
  const query = state.panelTestQuery.trim();
  state.panelValidate[name] = {
    status: "testing",
    detail: query ? `forcing “${query}” through ${name}…` : "downloading a test track…",
  };
  render();
  try {
    const r = await api.validateSource(name, query || undefined);
    const q = r.quality_label ? ` · ${r.quality_label}` : "";
    const t = r.elapsed_s != null ? ` · ${r.elapsed_s}s` : "";
    state.panelValidate[name] = {
      status: r.ok ? "ok" : "fail",
      detail: `${r.detail}${r.ok ? q + t : ""}`,
    };
  } catch (err) {
    state.panelValidate[name] = { status: "fail", detail: (err as Error).message };
  }
  render();
}
async function togglePanelEnabled(): Promise<void> {
  const name = state.panelSource;
  if (!name || name === "ytdlp") return;
  const enabled = !state.sources[name].enabled;
  try {
    const updated = await api.putSource(name, { enabled });
    state.sources[name] = updated;
    state.layout = await api.config();
    toast("Applied — cascade hot-reloaded");
  } catch (err) {
    toast(`Failed: ${(err as Error).message}`);
  }
  render();
}

// Events are delegated on the root ONCE — they survive every reconcile.
function bindEvents(): void {
  root.addEventListener("click", (e) => {
    const el = (e.target as HTMLElement).closest<HTMLElement>("[data-act]");
    if (!el) return;
    e.stopPropagation();
    const act = el.dataset.act!;
    if (act === "logout") void api.logout();
    else if (act === "openSourceDialog") openSourceDialog();
    else if (act === "closeSourceDialog") closeSourceDialog();
    else if (act === "downloadSource") void downloadSourceCandidate(el.dataset.id!);
    else if (act === "toggleImport") void toggleImport();
    else if (act === "importLiked") void runImport("liked", "liked");
    else if (act === "importPlaylist")
      void runImport("playlist", el.dataset.pid!, el.dataset.pid!, el.dataset.name!);
    else if (act === "toggleSync") void toggleSync(el.dataset.pid!, el.dataset.name!);
    else if (act === "strategy") setStrategy(el.dataset.tier!, el.dataset.strategy as "all" | "uniform");
    else if (act === "openPanel") {
      state.panelSource = el.dataset.source!;
      state.panelValues = {};
      state.panelSave = "idle";
      render();
    } else if (act === "closePanel") {
      state.panelSource = null;
      render();
    } else if (act === "openDetail") void openDetail(el.dataset.id!);
    else if (act === "closeDetail") {
      state.selectedId = null;
      state.detail = null;
      render();
    } else if (act === "toggleLogs") {
      state.showLogs = !state.showLogs;
      render();
    } else if (act === "togglePanelEnabled") void togglePanelEnabled();
    else if (act === "savePanel") void savePanel();
    else if (act === "enqueue") {
      const t = findTrack(el.dataset.id!);
      if (t) void enqueueTrack(t);
    } else if (act === "setTab") {
      state.activeTab = (el.dataset.tab as State["activeTab"]) || "cascade";
      window.scrollTo(0, 0);
      render();
    } else if (act === "gotoBulk") {
      state.page = "bulk";
      state.bulkPanel = false;
      window.scrollTo(0, 0);
      render();
      void loadBatches().then(render);
    } else if (act === "gotoConsole") {
      state.page = "console";
      window.scrollTo(0, 0);
      render();
    } else if (act === "gotoFeed") {
      state.page = "feed";
      state.bulkPanel = false;
      window.scrollTo(0, 0);
      render();
      void api.feed().then((f) => {
        state.feed = f;
        render();
      });
    } else if (act === "gotoReview") {
      state.page = "review";
      state.bulkPanel = false;
      window.scrollTo(0, 0);
      render();
      void loadReview().then(render);
    } else if (act === "toggleReview") {
      const id = el.dataset.id!;
      state.reviewOpen[id] = !state.reviewOpen[id];
      if (state.reviewOpen[id]) void loadReviewDetail(id);
      render();
    } else if (act === "selectCand") {
      state.reviewSel[el.dataset.rid!] = el.dataset.cid!;
      render();
    } else if (act === "rescanReview") void rescanReview(el.dataset.id!);
    else if (act === "searchHint") {
      const id = el.dataset.id!;
      const item = state.reviewItems.find((i) => i.id === id);
      const term = item ? hintTerm(item) : null;
      if (term) {
        state.reviewQ[id] = term; // seed the search box with the fingerprint term
        void rescanReview(id);
      }
    }
    else if (act === "downloadCand") void downloadCand(el.dataset.id!);
    else if (act === "dismissReview") void dismissReview(el.dataset.id!);
    else if (act === "identifyReview") void identifyReview(el.dataset.id!);
    else if (act === "replaceAnyway") void replaceAnyway(el.dataset.id!);
    else if (act === "playClip") toggleClip(el.dataset.key!, el.dataset.url!);
    else if (act === "seekClip") {
      const rect = el.getBoundingClientRect();
      seekClip(el.dataset.key!, rect.width > 0 ? ((e as MouseEvent).clientX - rect.left) / rect.width : 0);
    }
    else if (act === "openBulkPanel") {
      state.bulkPanel = true;
      render();
      void loadBatches().then(render);
    } else if (act === "closeBulkPanel") {
      state.bulkPanel = false;
      render();
    } else if (act === "submitBulk") void submitBulk();
    else if (act === "toggleBatch") void toggleBatch(el.dataset.id!);
    else if (act === "pauseBatch")
      void batchAction(() => api.pauseBatch(el.dataset.id!, el.dataset.paused === "1"));
    else if (act === "cancelBatch") void batchAction(() => api.cancelBatch(el.dataset.id!));
    else if (act === "retryTrack") void batchAction(() => api.retry(el.dataset.id!));
    else if (act === "validatePanel") {
      if (state.panelSource) void validatePanel(state.panelSource);
    }
  });

  // queue from the search dropdown must fire before focusout closes it
  root.addEventListener("mousedown", (e) => {
    const el = (e.target as HTMLElement).closest<HTMLElement>('.srow [data-act="enqueue"], .srow');
    if (!el) return;
    const id = el.getAttribute("data-id");
    if (!id) return;
    e.preventDefault(); // keep focus on the input so the dropdown stays put
    const t = findTrack(id);
    if (t) void enqueueTrack(t);
  });

  root.addEventListener("input", (e) => {
    const el = e.target as HTMLInputElement;
    if (el.dataset.inp === "search") {
      state.histSearch = el.value.trim();
      window.clearTimeout(histTimer);
      histTimer = window.setTimeout(() => void loadHistory().then(render), 250);
    } else if (el.dataset.inp === "spotify") {
      state.searchQuery = el.value;
      state.searchFocus = true;
      window.clearTimeout(searchTimer);
      const q = el.value.trim();
      searchTimer = window.setTimeout(() => void runSearch(q), 220);
    } else if (el.dataset.inp === "panelField") {
      state.panelValues[el.dataset.key!] = el.value;
      state.panelSave = "dirty";
      render(); // reflect Save button state; morph keeps the focused field intact
    } else if (el.dataset.inp === "panelTestQuery") {
      state.panelTestQuery = el.value; // capture without re-render (keep focus)
    } else if (el.dataset.inp === "bulk") {
      state.bulkText = el.value; // capture without re-render so the textarea isn't clobbered
    } else if (el.dataset.inp === "reviewq") {
      state.reviewQ[el.dataset.id!] = el.value; // capture without re-render (keep focus)
    } else if (el.dataset.inp === "srcsearch") {
      state.srcQuery = el.value; // capture without re-render (keep focus), debounce fan-out
      window.clearTimeout(srcTimer);
      srcTimer = window.setTimeout(() => void runSourceSearch(), 320);
    }
  });

  root.addEventListener("keydown", (e) => {
    const el = e.target as HTMLInputElement;
    const key = (e as KeyboardEvent).key;
    if (el.dataset.inp === "reviewq" && key === "Enter") {
      e.preventDefault();
      state.reviewQ[el.dataset.id!] = el.value; // ensure the latest value is used
      void rescanReview(el.dataset.id!);
    } else if (el.dataset.inp === "srcsearch" && key === "Enter") {
      e.preventDefault();
      window.clearTimeout(srcTimer);
      state.srcQuery = el.value;
      void runSourceSearch();
    } else if (key === "Escape" && state.srcOpen) {
      closeSourceDialog();
    }
  });

  root.addEventListener("focusin", (e) => {
    if ((e.target as HTMLElement).dataset?.inp === "spotify") {
      window.clearTimeout(blurTimer);
      state.searchFocus = true;
      render();
    }
  });
  root.addEventListener("focusout", (e) => {
    if ((e.target as HTMLElement).dataset?.inp === "spotify") {
      blurTimer = window.setTimeout(() => {
        state.searchFocus = false;
        render();
      }, 160);
    }
  });

  root.addEventListener("change", (e) => {
    const el = e.target as HTMLSelectElement;
    if (el.dataset.chg === "status") {
      state.histStatus = el.value;
      void loadHistory().then(render);
    }
  });

  root.addEventListener("dragstart", (e) => {
    const el = (e.target as HTMLElement).closest<HTMLElement>("[data-drag-card]");
    if (!el) return;
    const name = el.dataset.source!;
    if (name === "ytdlp") {
      e.preventDefault();
      reject(name);
      return;
    }
    state.drag.source = name;
    const dt = (e as DragEvent).dataTransfer;
    if (dt) {
      dt.effectAllowed = "move";
      try {
        dt.setData("text/plain", name);
      } catch {
        /* ignore */
      }
    }
  });

  root.addEventListener("dragend", () => {
    state.drag = { source: null, overTier: null, overIndex: null };
    render();
  });

  root.addEventListener("dragover", (e) => {
    if (!state.drag.source) return;
    const t = e.target as HTMLElement;
    const card = t.closest<HTMLElement>("[data-drag-card]");
    if (card) {
      e.preventDefault();
      const idx = Number(card.dataset.index);
      const rect = card.getBoundingClientRect();
      const after = (e as DragEvent).clientX - rect.left > rect.width / 2;
      const tier = card.dataset.tier!;
      const oi = after ? idx + 1 : idx;
      if (state.drag.overTier !== tier || state.drag.overIndex !== oi) {
        state.drag.overTier = tier;
        state.drag.overIndex = oi;
        render();
      }
      return;
    }
    const col = t.closest<HTMLElement>("[data-drop-tier]");
    if (col) {
      e.preventDefault();
      const key = col.dataset.dropTier!;
      if (state.drag.overTier !== key) {
        const c =
          key === "disabled"
            ? (state.layout?.disabled ?? [])
            : (state.layout?.tiers.find((t) => String(t.tier) === key)?.sources ?? []);
        state.drag.overTier = key;
        state.drag.overIndex = c.length;
        render();
      }
    }
  });

  root.addEventListener("drop", (e) => {
    const col = (e.target as HTMLElement).closest<HTMLElement>("[data-drop-tier]");
    if (!col) return;
    e.preventDefault();
    const name = state.drag.source;
    const key = col.dataset.dropTier!;
    const index = state.drag.overIndex;
    state.drag = { source: null, overTier: null, overIndex: null };
    if (!name) {
      render();
      return;
    }
    if (name === "ytdlp") {
      reject(name);
      return;
    }
    moveSource(name, key, index);
  });
}

// ---------- boot ----------
function flashOAuthResult(): void {
  const params = new URLSearchParams(location.search);
  const s = params.get("spotify");
  const login = params.get("login");
  if (!s && !login) return;
  if (s) toast(s === "connected" ? "Spotify connected ✓" : "Spotify connection failed — try again");
  if (login === "error") state.error = "Login failed — please try again.";
  params.delete("spotify");
  params.delete("login");
  const qs = params.toString();
  history.replaceState(null, "", location.pathname + (qs ? `?${qs}` : ""));
}

function watchViewport(): void {
  if (typeof matchMedia === "undefined") return;
  const mq = matchMedia("(max-width: 640px)");
  const apply = () => {
    if (state.isMobile !== mq.matches) {
      state.isMobile = mq.matches;
      render();
    }
  };
  mq.addEventListener("change", apply);
}

async function boot(): Promise<void> {
  bindEvents(); // once
  watchViewport(); // desktop <-> mobile layout switch
  flashOAuthResult();
  render(); // header/skeleton
  // Login-with-Spotify gate: /api/me is null when auth is configured and there's
  // no session. In single-user mode it returns the service user and we proceed.
  try {
    state.me = await api.me();
    if (state.me === null) {
      state.authRequired = true;
      render();
      return;
    }
  } catch (err) {
    state.error = (err as Error).message;
  }
  try {
    await Promise.all([loadStatic(), loadHistory()]);
    state.stats = await api.stats();
  } catch (err) {
    state.error = (err as Error).message;
  }
  render();
  void loadSpotifyStatus().then(render);
  void loadBatches().then(render);

  // side effects — run once
  effect(connectLive);
  effect(() => {
    window.setInterval(() => {
      state.tick++;
      if (state.inflight.length || state.layout?.tiers.some((t) => t.strategy === "uniform")) render();
    }, 1000);
    window.setInterval(() => void loadHistory().then(render), 15000); // pick up completions
    window.setInterval(() => void loadPlayer().then(render), 5000); // now-playing refresh
    // batches: poll faster while the bulk page/panel is open, slow otherwise
    window.setInterval(() => {
      if (state.page === "bulk" || state.bulkPanel || state.batches.length) {
        void loadBatches().then(render);
      }
    }, 3000);
    // review: poll while on the page so completed re-downloads drop out live
    window.setInterval(() => {
      if (state.page === "review") void loadReview().then(render);
    }, 4000);
  });
  // keep the review badge fresh even when not on the page
  void loadReview().then(render);
}

void boot();
