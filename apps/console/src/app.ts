// crate console — single-screen app (cascade map + listening/live/wins/history
// rail + detail drawer + source config panel). Ported from the v4 design, wired
// to the real /api. No framework: one state object, reconciling re-render, event
// delegation. See docs/console-ui-spec.md.
import "./styles.css";
import {
  api,
  type Batch,
  type BatchDetail,
  type Layout,
  type LiveEvent,
  type PlayerState,
  type PlayerTrack,
  type RequestDetail,
  type RequestRow,
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
  searchFocus: boolean; // dropdown open only while the header field has focus
  enqueued: Record<string, boolean>; // track id -> already sent to the cascade
  spotifyConfigured: boolean; // /api/spotify/status.configured (user-OAuth set up)
  spotifyConnected: boolean; // refresh token stored -> Listening panel is live
  player: PlayerState | null; // now-playing + recently-played
  isMobile: boolean; // viewport <= 640px -> tabbed app layout
  activeTab: "cascade" | "activity" | "tracks"; // mobile bottom-nav selection
  page: "console" | "bulk"; // top-level page (bulk downloads is a separate screen)
  bulkPanel: boolean; // right-side bulk drawer open
  bulkText: string; // paste box (playlist link / track list)
  bulkBusy: boolean; // submit in flight
  batches: Batch[]; // bulk batches (polled)
  expandedBatches: Record<string, boolean>; // batch id -> show track rows
  batchDetails: Record<string, BatchDetail>; // fetched track rows for expanded batches
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
  searchFocus: false,
  enqueued: {},
  spotifyConfigured: false,
  spotifyConnected: false,
  player: null,
  isMobile: typeof matchMedia !== "undefined" ? matchMedia("(max-width: 640px)").matches : false,
  activeTab: "cascade",
  page: "console",
  bulkPanel: false,
  bulkText: "",
  bulkBusy: false,
  batches: [],
  expandedBatches: {},
  batchDetails: {},
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
      render();
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
    await api.enqueue(track.query);
    toast(`Queued: ${track.title}`);
    void loadHistory().then(render); // surface the new queued row immediately
  } catch (err) {
    state.enqueued[track.id] = false;
    toast(`Couldn't queue: ${(err as Error).message}`);
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
      `<a href="${api.spotifyLoginUrl}" style="display:flex;align-items:center;justify-content:center;gap:9px;background:#181c17;border:1px solid #272c25;border-radius:14px;padding:14px;text-decoration:none;color:#e9ede3;font:600 12.5px 'Instrument Sans';transition:border-color .15s" class="ghost">
        <span style="width:8px;height:8px;border-radius:50%;background:#ff4c38"></span>Connect Spotify to pull your now-playing &amp; recent tracks</a>`,
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
  return wrap(nowBlock + recentBlock);
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
        <span style="flex:1;min-width:0;font:500 12.5px 'Instrument Sans';overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(d.query)}</span>
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
          <div style="font:500 12.5px 'Instrument Sans';overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.query)}</div>
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
    <div style="padding:16px 22px;border-top:1px solid #1d211c;display:flex;align-items:center;gap:12px">
      <button data-act="savePanel" ${saving ? "disabled" : ""} style="flex:1;background:${saved ? "rgba(233,237,227,.14)" : saving ? "#272c25" : "#e9ede3"};border:none;border-radius:14px;color:${saved ? "#e9ede3" : saving ? C.muted : "#14170f"};font:600 13px 'Instrument Sans';padding:12px;cursor:${saving ? "default" : "pointer"};transition:.15s;display:flex;align-items:center;justify-content:center;gap:8px">${saving ? "Saving…" : saved ? "Saved ✓" : dirty ? "Save changes" : "Save"}</button>
      ${def.validate ? `<button class="ghost" data-act="validatePanel" style="background:#181c17;border:1px solid #272c25;border-radius:14px;color:#e9ede3;font:600 13px 'Instrument Sans';padding:12px 16px;cursor:pointer">Test</button>` : ""}
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
  return `<div style="position:absolute;top:calc(100% + 8px);left:0;right:0;background:#111411;border:1px solid #272c25;border-radius:16px;box-shadow:0 24px 70px rgba(0,0,0,.65);overflow:hidden;animation:fadein .15s ease;z-index:70">${body}</div>`;
}

function activeDownloads(): number {
  const inBatches = state.batches.reduce((n, b) => n + b.counts.queued + b.counts.downloading, 0);
  const singles = state.inflight.filter((r) => !r.batch_id).length;
  return inBatches + singles;
}
function downloadsNavHtml(): string {
  const n = activeDownloads();
  const badge =
    n > 0
      ? `<span style="font:700 10px 'Doto';min-width:17px;height:17px;padding:0 4px;border-radius:10px;background:#ff4c38;color:#0c0e0c;display:inline-flex;align-items:center;justify-content:center">${n}</span>`
      : "";
  return `<button data-act="gotoBulk" title="Bulk downloads" style="display:inline-flex;align-items:center;gap:7px;border:1px solid #272c25;border-radius:20px;cursor:pointer;font:600 11px 'JetBrains Mono';letter-spacing:.04em;padding:6px 13px;background:#131612;color:#99a091;transition:border-color .15s" class="ghost">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11M6 10l6 6 6-6M5 20h14"></path></svg>downloads${badge}
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
          <input data-inp="spotify" value="${esc(state.searchQuery)}" placeholder="Search tracks — queue a download…" style="width:100%;background:#0c0e0c;border:1px solid #272c25;border-radius:20px;color:#e9ede3;font:400 12.5px 'Instrument Sans';padding:8px 14px 8px 34px;transition:border-color .15s"/>
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
      ${search}
      <div style="margin-left:auto;display:flex;align-items:center;gap:10px">
        ${downloadsNavHtml()}
        <div style="display:flex;align-items:center;gap:8px;padding:7px 14px;border-radius:20px;background:${bg};border:1px solid ${bd};transition:.2s">
          ${spin ? `<span style="width:11px;height:11px;border:1.5px solid #272c25;border-top-color:#e9ede3;border-radius:50%;animation:spin .8s linear infinite"></span>` : ""}
          <span style="font:600 11px 'JetBrains Mono';color:${col}">${txt}</span>
        </div>
        <div style="display:flex;align-items:center;gap:8px;padding:7px 13px;background:#131612;border:1px solid #272c25;border-radius:20px">
          <span style="width:7px;height:7px;border-radius:50%;background:${live ? "#ff4c38" : C.faint};animation:pulse 2s ease-in-out infinite"></span>
          <span style="font:500 11px 'JetBrains Mono';color:#99a091;letter-spacing:.06em">${live ? "live" : "connecting…"}</span>
        </div>
      </div>
    </div>
  </header>`;
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
        <input data-inp="spotify" value="${esc(state.searchQuery)}" placeholder="Search tracks — queue a download…" style="width:100%;background:#111411;border:1px solid #272c25;border-radius:20px;color:#e9ede3;font:400 13px 'Instrument Sans';padding:11px 15px 11px 38px"/>
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
  return `<div style="background:#111411;border:1px solid #272c25;border-radius:16px;overflow:hidden;margin-top:10px;animation:fadein .15s ease">${body}</div>`;
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
      <div style="display:flex;flex-direction:column;gap:12px">${liveHtml()}${winsHtml()}${historyHtml()}</div>`;
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

// Pure view: State -> HTML. The renderer reconciles it into the DOM (morph), so
// this can run every tick without wiping the page.
function view(): string {
  if (state.error) {
    return `<div style="max-width:600px;margin:80px auto;padding:24px;color:#ff4c38;font:500 14px 'Instrument Sans';text-align:center">Failed to load console: ${esc(state.error)}</div>`;
  }
  if (state.page === "bulk") {
    return `${headerHtml()}${bulkPageHtml()}${drawerHtml()}${panelHtml()}${bulkPanelHtml()}${toastHtml()}`;
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
    if (act === "strategy") setStrategy(el.dataset.tier!, el.dataset.strategy as "all" | "uniform");
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
    } else if (act === "openBulkPanel") {
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
    else if (act === "validatePanel") toast(`${state.panelSource} — validation not wired yet`);
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
    } else if (el.dataset.inp === "bulk") {
      state.bulkText = el.value; // capture without re-render so the textarea isn't clobbered
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
  if (!s) return;
  toast(s === "connected" ? "Spotify connected ✓" : "Spotify connection failed — try again");
  params.delete("spotify");
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
  });
}

void boot();
