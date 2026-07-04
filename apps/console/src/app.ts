// lbmd console — single-screen app (cascade map + live/wins/history rail +
// detail drawer + source config panel). Ported from the v2 design, wired to the
// real /api. No framework: one state object, full re-render on change, event
// delegation. See docs/console-ui-spec.md.
import "./styles.css";
import {
  api,
  type Layout,
  type LiveEvent,
  type RequestDetail,
  type RequestRow,
  type Source,
  type TierCfg,
} from "./api";
import { bytes, dur, ago as relTime, pct, esc } from "./format";
import { createRenderer, effect } from "./reactive";

const C = {
  good: "#4ade80",
  warn: "#fbbf24",
  bad: "#f87171",
  accent: "#6ea8fe",
  teal: "#34d3b5",
  muted: "#8a94a6",
  faint: "#5b6575",
  text: "#e8ebf1",
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
      ["LBMD_QOBUZ_APP_ID", "App ID"],
      ["LBMD_QOBUZ_APP_SECRET", "App Secret"],
      ["LBMD_QOBUZ_TOKEN", "Token"],
    ],
    settings: [],
    validate: true,
  },
  monochrome: {
    creds: [],
    settings: [],
    note: "Browser-based source — no credentials required. Enable it and lbmd drives a headless session.",
  },
  deezer: { creds: [["LBMD_DEEZER_ARL", "ARL cookie"]], settings: [], validate: true },
  soulseek: {
    creds: [
      ["LBMD_SLSKD_URL", "slskd URL"],
      ["LBMD_SLSKD_API_KEY", "API key"],
      ["LBMD_SLSKD_DOWNLOADS", "Downloads dir"],
    ],
    settings: [["min_bitrate_kbps", "Minimum bitrate (kbps)", "number", "256"]],
    validate: true,
  },
  telegram: {
    creds: [
      ["LBMD_TG_API_ID", "API ID"],
      ["LBMD_TG_API_HASH", "API hash"],
      ["LBMD_TG_SESSION", "Session string"],
      ["LBMD_TG_BOT", "Bot handle"],
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
};

const root = document.getElementById("app")!;
let saveTimer: number | undefined;
let toastTimer: number | undefined;
let shakeTimer: number | undefined;
let histTimer: number | undefined;

// ---------- helpers ----------
const tierColor = (t: number | null): string =>
  t === 1 ? C.accent : t === 2 ? C.teal : t === 3 ? C.warn : C.faint;

function credMeta(c: string): { color: string; text: string } {
  if (c === "ok") return { color: C.good, text: "ready" };
  if (c === "partial") return { color: C.warn, text: "partial" };
  if (c === "missing") return { color: C.bad, text: "missing" };
  return { color: C.faint, text: "no auth" };
}
function statusMeta(s: string): { label: string; color: string; bg: string } {
  if (s === "done") return { label: "Done", color: C.good, bg: "rgba(74,222,128,.12)" };
  if (s === "failed") return { label: "Failed", color: C.bad, bg: "rgba(248,113,113,.12)" };
  return { label: "Downloading", color: C.accent, bg: "rgba(110,168,254,.12)" };
}
const qualityBadge = (loss: boolean): string => (loss ? "FLAC" : "any");

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
  const changed = JSON.stringify(cfg) !== JSON.stringify({ tiers: state.layout.tiers, disabled: state.layout.disabled });
  if (changed) void persistLayout(cfg);
}

// ---------- view builders (return HTML strings) ----------
function statCardsHtml(): string {
  const s = state.stats;
  const cards = s
    ? [
        ["Requests", String(s.total), C.text, `${s.done} done · ${s.failed} failed · ${s.in_flight} live`],
        ["Success rate", pct(s.success_rate), C.good, `${s.done} of ${s.done + s.failed} completed`],
        ["In flight", String(s.in_flight), C.accent, "live"],
        ["Lossless", pct(s.lossless_rate), C.text, `${s.lossless} of ${s.done} downloads`],
        ["Downloaded", bytes(s.total_bytes), C.text, "across sources"],
        ["Avg time", s.avg_elapsed_s ? `${s.avg_elapsed_s.toFixed(1)}s` : "—", C.text, "per completed"],
      ]
    : Array(6).fill(["—", "—", C.text, ""]);
  return `<div style="display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:16px">${cards
    .map(
      ([label, value, color, sub]) => `<div style="background:#14181f;border:1px solid #2a313d;border-radius:12px;padding:12px 14px">
        <div style="font:600 9px 'JetBrains Mono';letter-spacing:.11em;text-transform:uppercase;color:#8a94a6">${esc(label)}</div>
        <div style="font:700 23px 'Space Grotesk';letter-spacing:-.02em;margin-top:6px;line-height:1;color:${color}">${esc(value)}</div>
        <div style="font:400 10px 'JetBrains Mono';color:#5b6575;margin-top:6px">${esc(sub)}</div>
      </div>`,
    )
    .join("")}</div>`;
}

function cardHtml(name: string, tierKey: string, index: number): string {
  const meta = state.sources[name];
  if (!meta) return "";
  const cm = credMeta(meta.credentials);
  const locked = name === "ytdlp";
  const dragging = state.drag.source === name;
  const loss = meta.lossless;
  const shake = state.rejectShake === name ? "animation:shake .48s;" : "";
  return `<div class="dcard" draggable="${!locked}" data-drag-card data-source="${esc(name)}" data-tier="${esc(tierKey)}" data-index="${index}"
      style="flex:1;display:flex;align-items:center;gap:9px;background:#1b212a;border:1px solid ${meta.credentials === "missing" ? "rgba(251,191,36,.4)" : "#2a313d"};border-radius:11px;padding:10px 11px;cursor:${locked ? "default" : "grab"};opacity:${dragging ? ".4" : "1"};transition:border-color .15s,box-shadow .15s;${shake}">
      <span style="color:${locked ? "#232b36" : "#5b6575"};font:600 13px 'JetBrains Mono';letter-spacing:-2px;user-select:none">${locked ? "" : "⣿"}</span>
      <div style="flex:1;min-width:0">
        <div style="display:flex;align-items:center;gap:7px">
          <span style="font:600 13px 'JetBrains Mono';color:#e8ebf1">${esc(name)}</span>
          <span style="font:600 9px 'JetBrains Mono';letter-spacing:.04em;padding:2px 6px;border-radius:5px;background:${loss ? "rgba(110,168,254,.14)" : "rgba(139,148,166,.14)"};color:${loss ? C.accent : C.muted}">${qualityBadge(loss)}</span>
        </div>
        <div style="display:flex;align-items:center;gap:6px;margin-top:5px">
          <span style="width:6px;height:6px;border-radius:50%;background:${cm.color};flex-shrink:0"></span>
          <span style="font:500 10px 'JetBrains Mono';color:${cm.color}">${cm.text}</span>
          ${meta.credentials === "missing" ? `<span style="font:400 10px 'JetBrains Mono';color:#fbbf24">· unconfigured</span>` : ""}
        </div>
      </div>
      ${locked ? `<span title="Pinned final fallback — locked" style="color:#5b6575;font-size:13px">🔒</span>` : `<button class="gear" data-act="openPanel" data-source="${esc(name)}" title="Configure" style="background:none;border:none;cursor:pointer;color:#5b6575;font-size:15px;padding:2px 4px;border-radius:6px;transition:.12s">⚙</button>`}
    </div>`;
}

function tierHtml(t: TierCfg, showFall: boolean): string {
  const key = String(t.tier);
  const accent = tierColor(t.tier);
  const over = state.drag.overTier === key && state.drag.source;
  const isAll = t.strategy === "all";
  const n = t.sources.length;
  const caption = isAll
    ? n <= 1
      ? "parallel · first verified file wins"
      : `parallel · all ${n} fired at once, first wins`
    : "uniform · one per request, rotating";
  // build cards with a drop placeholder at overIndex
  const cells: string[] = [];
  for (let i = 0; i <= n; i++) {
    if (over && state.drag.overIndex === i)
      cells.push(
        `<div style="flex:0 0 130px;align-self:stretch;border:1.5px dashed #6ea8fe;border-radius:11px;background:rgba(110,168,254,.07)"></div>`,
      );
    if (i < n)
      cells.push(
        `<div style="flex:0 1 240px;min-width:168px;display:flex;flex-direction:column"><span style="width:1.5px;height:14px;margin:0 auto;background:${isAll ? accent : "#2a313d"};box-shadow:${isAll ? "0 0 8px " + accent : "none"}"></span>${cardHtml(t.sources[i], key, i)}</div>`,
      );
  }
  const stratBtn = (strat: string, label: string, active: boolean) =>
    `<button data-act="strategy" data-tier="${key}" data-strategy="${strat}" style="border:none;border-radius:7px;cursor:pointer;font:600 10px 'JetBrains Mono';padding:5px 9px;background:${active ? accent : "transparent"};color:${active ? "#0d1014" : C.muted};transition:.15s">${label}</button>`;
  return `<div>
    <div data-drop-tier="${key}" style="border:1.5px solid ${over ? accent : "#2a313d"};border-radius:14px;background:${over ? "rgba(110,168,254,.04)" : "#111419"};padding:12px 14px 14px;transition:border-color .15s,background .15s">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span style="width:9px;height:9px;border-radius:3px;background:${accent}"></span>
        <span style="font:700 13px 'Space Grotesk'">Tier ${key}</span>
        <span style="font:500 11px 'JetBrains Mono';color:#5b6575">${n} ${n === 1 ? "source" : "sources"}</span>
        <div style="margin-left:auto;display:flex;align-items:center;gap:10px">
          <span style="font:400 10px 'JetBrains Mono';color:#5b6575">${caption}</span>
          <div style="display:flex;background:#0d1014;border:1px solid #2a313d;border-radius:9px;padding:2px">
            ${stratBtn("all", "⇉ Parallel", isAll)}${stratBtn("uniform", "⟳ Uniform", !isAll)}
          </div>
        </div>
      </div>
      <div style="display:flex;flex-direction:column;align-items:center;margin-top:8px">
        <span style="width:26px;height:26px;border-radius:50%;background:#0d1014;border:1.5px solid ${accent};display:inline-flex;align-items:center;justify-content:center;font:600 13px 'JetBrains Mono';color:${accent}">${isAll ? "⇉" : "⟳"}</span>
        <span style="width:1.5px;height:10px;background:${isAll ? accent + "55" : "#2a313d"}"></span>
      </div>
      <div style="height:1.5px;background:${isAll ? accent + "55" : "#2a313d"};margin:0 ${n <= 1 ? "34%" : n === 2 ? "22%" : "14%"};border-radius:2px"></div>
      <div style="display:flex;gap:10px;justify-content:center;min-height:86px">
        ${cells.join("") || `<div style="align-self:center;padding:20px 8px;font:400 11px 'JetBrains Mono';color:#3a4453">Drop sources here</div>`}
      </div>
    </div>
    ${
      showFall
        ? `<div style="display:flex;flex-direction:column;align-items:center;padding:2px 0">
        <span style="width:1.5px;height:9px;background:#2a313d"></span>
        <span style="font:500 9.5px 'JetBrains Mono';color:#5b6575;padding:2px 9px;border:1px solid #1f2732;border-radius:12px;background:#111419">no verified file · fall through</span>
        <span style="width:1.5px;height:9px;background:#2a313d"></span>
        <span style="color:#3a4453;font-size:9px;line-height:4px;margin-bottom:2px">▼</span>
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
      cells.push(`<div style="flex:0 0 130px;border:1.5px dashed #6ea8fe;border-radius:11px;background:rgba(110,168,254,.07)"></div>`);
    if (i < list.length) cells.push(cardHtml(list[i], "disabled", i));
  }
  return `<div data-drop-tier="disabled" style="margin-top:16px;border:1.5px dashed ${over ? C.muted : "#1f2732"};border-radius:14px;background:${over ? "rgba(139,148,166,.04)" : "#111419"};padding:11px 14px;transition:border-color .15s">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
      <span style="font:700 12px 'Space Grotesk';color:#8a94a6">Available / Disabled</span>
      <span style="font:500 11px 'JetBrains Mono';color:#5b6575">${list.length === 0 ? "empty" : list.length + (list.length === 1 ? " source" : " sources")}</span>
      <span style="margin-left:auto;font:400 10px 'JetBrains Mono';color:#3a4453">not in the cascade</span>
    </div>
    <div style="display:flex;gap:10px;flex-wrap:wrap;min-height:56px">
      ${cells.join("") || `<div style="align-self:center;font:400 11px 'JetBrains Mono';color:#3a4453;padding:12px 4px">Every source is in a tier</div>`}
    </div>
  </div>`;
}

function cascadeHtml(): string {
  const tiers = [...(state.layout?.tiers ?? [])].sort((a, b) => a.tier - b.tier);
  return `<section style="background:#14181f;border:1px solid #2a313d;border-radius:16px;padding:18px 20px 20px">
    <div style="margin-bottom:6px">
      <h1 style="margin:0;font:700 17px 'Space Grotesk';letter-spacing:-.01em">Cascade</h1>
      <p style="margin:3px 0 0;font:400 12px 'Space Grotesk';color:#8a94a6">Requests fall through tiers until a source produces a verified file. Drag sources between tiers; toggles set each tier's fan-out strategy.</p>
    </div>
    <div style="display:flex;flex-direction:column;align-items:center;margin-top:10px">
      <span style="display:inline-flex;align-items:center;gap:8px;padding:6px 14px;border-radius:20px;background:#1b212a;border:1px solid #2a313d;font:600 11px 'JetBrains Mono';color:#e8ebf1"><span style="width:7px;height:7px;border-radius:50%;background:#6ea8fe;animation:pulse 1.6s ease-in-out infinite"></span>request in</span>
      <span style="width:1.5px;height:14px;background:#2a313d"></span>
      <span style="color:#3a4453;font-size:9px;line-height:4px;margin-bottom:2px">▼</span>
    </div>
    ${tiers.map((t, i) => tierHtml(t, i < tiers.length - 1)).join("")}
    <div style="display:flex;flex-direction:column;align-items:center">
      <span style="width:1.5px;height:14px;background:#2a313d"></span>
      <span style="display:inline-flex;align-items:center;gap:8px;padding:6px 14px;border-radius:20px;background:rgba(74,222,128,.08);border:1px solid rgba(74,222,128,.3);font:600 11px 'JetBrains Mono';color:#4ade80">✓ verified file → library</span>
    </div>
    ${trayHtml()}
  </section>`;
}

function liveHtml(): string {
  const rows = state.inflight;
  const body = rows.length
    ? rows
        .map((d) => {
          const el = dur((Date.now() - new Date(d.created_at).getTime()) / 1000);
          return `<div style="display:flex;align-items:center;gap:10px;padding:9px 0;border-top:1px solid #171c24">
        <span style="width:13px;height:13px;border:2px solid #2a313d;border-top-color:#6ea8fe;border-radius:50%;animation:spin .8s linear infinite;flex-shrink:0"></span>
        <span style="flex:1;min-width:0;font:500 12.5px 'Space Grotesk';overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(d.query)}</span>
        <span style="font:500 10px 'JetBrains Mono';color:#8a94a6;padding:2px 7px;background:#1b212a;border-radius:5px">${esc(d.requested_via)}</span>
        <span style="font:600 12px 'JetBrains Mono';color:#6ea8fe">${el}</span>
      </div>`;
        })
        .join("")
    : `<div style="padding:14px 0 6px;text-align:center;color:#5b6575;font:400 12px 'Space Grotesk'">Nothing downloading right now.</div>`;
  return `<div style="background:#14181f;border:1px solid #2a313d;border-radius:14px;padding:14px 16px">
    <div style="display:flex;align-items:center;gap:9px;margin-bottom:10px">
      <span style="font:600 13px 'Space Grotesk'">Live</span>
      <span style="font:400 11px 'JetBrains Mono';color:#5b6575">${rows.length} in-flight · SSE</span>
    </div>${body}</div>`;
}

function winsHtml(): string {
  const wins = state.stats?.wins_by_tier ?? {};
  const max = Math.max(1, ...Object.values(wins));
  const bars = [1, 2, 3]
    .map((t) => {
      const v = wins[String(t)] ?? 0;
      return `<div style="display:flex;align-items:center;gap:12px;padding:4px 0">
        <span style="width:56px;font:500 11px 'JetBrains Mono';color:#8a94a6;text-align:right;flex-shrink:0">Tier ${t}</span>
        <div style="flex:1;height:7px;background:#1b212a;border-radius:6px;overflow:hidden"><div style="height:100%;width:${Math.round((v / max) * 100)}%;background:${tierColor(t)};border-radius:6px;transition:width .5s cubic-bezier(.2,.8,.2,1)"></div></div>
        <span style="width:20px;font:600 11px 'JetBrains Mono';text-align:right;flex-shrink:0">${v}</span>
      </div>`;
    })
    .join("");
  return `<div style="background:#14181f;border:1px solid #2a313d;border-radius:14px;padding:14px 16px">
    <div style="font:600 13px 'Space Grotesk';margin-bottom:10px">Wins by tier</div>${bars}</div>`;
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
          const wonColor = r.status === "done" ? tierColor(r.tier_won) : r.status === "failed" ? C.bad : C.accent;
          const quality =
            r.status === "downloading" ? "In progress" : r.lossless === 1 ? "FLAC" : r.quality_label || "no file";
          return `<div class="hrow" data-act="openDetail" data-id="${esc(r.id)}" style="display:flex;align-items:center;gap:10px;padding:10px 16px;border-top:1px solid #171c24;cursor:pointer;transition:background .12s">
        <span title="${esc(sm.label)}" style="width:7px;height:7px;border-radius:50%;background:${sm.color};flex-shrink:0"></span>
        <div style="flex:1;min-width:0">
          <div style="font:500 12.5px 'Space Grotesk';overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.query)}</div>
          <div style="font:400 10px 'JetBrains Mono';color:#5b6575;margin-top:2px">${esc(quality)} · ${bytes(r.size_bytes)}</div>
        </div>
        <span style="font:500 10.5px 'JetBrains Mono';color:${wonColor};flex-shrink:0">${esc(won)}</span>
        <span style="font:400 10.5px 'JetBrains Mono';color:#5b6575;flex-shrink:0;width:52px;text-align:right">${r.status === "downloading" ? "live" : relTime(r.created_at)}</span>
      </div>`;
        })
        .join("")
    : `<div style="padding:32px 16px;text-align:center;color:#5b6575;font:400 13px 'Space Grotesk'">No requests match.</div>`;
  const opt = (v: string, label: string) =>
    `<option value="${v}"${state.histStatus === v ? " selected" : ""}>${label}</option>`;
  return `<div style="background:#14181f;border:1px solid #2a313d;border-radius:14px;overflow:hidden;display:flex;flex-direction:column">
    <div style="display:flex;align-items:center;gap:10px;padding:13px 16px 11px">
      <span style="font:600 13px 'Space Grotesk';flex-shrink:0">History</span>
      <div style="position:relative;flex:1;min-width:100px">
        <span style="position:absolute;left:9px;top:50%;transform:translateY(-50%);color:#5b6575;font-size:12px">⌕</span>
        <input data-inp="search" value="${esc(state.histSearch)}" placeholder="Search…" style="width:100%;background:#0d1014;border:1px solid #2a313d;border-radius:8px;color:#e8ebf1;font:400 12px 'Space Grotesk';padding:6px 10px 6px 26px"/>
      </div>
      <select data-chg="status" style="background:#0d1014;border:1px solid #2a313d;border-radius:8px;color:#e8ebf1;font:500 11px 'Space Grotesk';padding:6px 8px;cursor:pointer">
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
    const lc = level === "ERROR" ? C.bad : level === "WARN" ? C.warn : level === "OK" ? C.good : C.faint;
    const mc = level === "ERROR" ? C.bad : level === "OK" ? C.text : C.muted;
    rows.push(
      `<div style="display:flex;gap:10px;font:400 11px 'JetBrains Mono';line-height:1.9;white-space:nowrap"><span style="color:#3a4453;flex-shrink:0">${fmt(t0)}</span><span style="color:${lc};flex-shrink:0;width:38px;font-weight:600">${level}</span><span style="color:${mc}">${esc(msg)}</span></div>`,
    );
    t0 += 900 + (rows.length * 347) % 1300;
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
          <span style="width:24px;height:24px;border-radius:50%;background:${a.ok ? "rgba(74,222,128,.1)" : "rgba(248,113,113,.08)"};border:1.5px solid ${okColor};display:flex;align-items:center;justify-content:center;font:700 12px 'JetBrains Mono';color:${okColor};z-index:1">${a.ok ? "✓" : "✗"}</span>
          ${i < n - 1 ? `<span style="width:1.5px;flex:1;background:#2a313d;margin-top:2px;min-height:14px"></span>` : ""}
        </div>
        <div style="flex:1;min-width:0;padding-bottom:2px">
          <div style="display:flex;align-items:center;gap:8px"><span style="font:600 13px 'JetBrains Mono';color:#e8ebf1">${esc(a.source)}</span><span style="font:500 10px 'JetBrains Mono';color:${tierColor(a.tier)}">T${a.tier}</span></div>
          <div style="font:400 12px 'JetBrains Mono';color:${a.ok ? "#e8ebf1" : C.muted};margin-top:3px;line-height:1.4">${esc(a.detail)}</div>
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
      const rows: [string, string][] = [
        ["Path", dl.path],
        ["Quality", dl.quality_label],
        ["Codec", codec],
        ["Size", bytes(dl.size_bytes)],
        ["Duration", dur(dl.duration_s)],
        ["Download time", dl.elapsed_s != null ? `${dl.elapsed_s.toFixed(1)}s` : "—"],
      ];
      facts = `<div style="background:#0d1014;border:1px solid #2a313d;border-radius:12px;overflow:hidden">${rows
        .map(
          ([k, v]) => `<div style="display:flex;justify-content:space-between;gap:14px;padding:10px 14px;border-bottom:1px solid #171c24"><span style="font:500 11px 'JetBrains Mono';color:#8a94a6;flex-shrink:0">${esc(k)}</span><span style="font:500 12px 'JetBrains Mono';color:#e8ebf1;text-align:right;word-break:break-all">${esc(v)}</span></div>`,
        )
        .join("")}</div>`;
    } else if (d.status === "failed") {
      facts = `<div style="background:rgba(248,113,113,.08);border:1px solid rgba(248,113,113,.3);border-radius:12px;padding:14px;font:400 12px 'JetBrains Mono';color:#f87171;line-height:1.5">${esc(d.error || "No source produced a verified file.")}</div>`;
    } else {
      facts = `<div style="display:flex;align-items:center;gap:10px;background:rgba(110,168,254,.07);border:1px solid rgba(110,168,254,.3);border-radius:12px;padding:14px;font:500 12px 'JetBrains Mono';color:#6ea8fe"><span style="width:13px;height:13px;border:2px solid #2a313d;border-top-color:#6ea8fe;border-radius:50%;animation:spin .8s linear infinite"></span>In progress — cascade still running.</div>`;
    }
    const factsTitle = d.status === "done" ? "Winner" : d.status === "failed" ? "Result" : "Status";
    inner = `<div style="padding:20px 22px;border-bottom:1px solid #1f2732">
      <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px">
        <div style="min-width:0">
          <div style="font:700 17px 'Space Grotesk';letter-spacing:-.01em;line-height:1.25">${esc(d.query)}</div>
          <div style="display:flex;align-items:center;gap:10px;margin-top:9px">
            <span style="display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:20px;background:${sm.bg};font:600 11px 'JetBrains Mono';color:${sm.color}"><span style="width:6px;height:6px;border-radius:50%;background:${sm.color}"></span>${sm.label}</span>
            <span style="font:500 11px 'JetBrains Mono';color:#8a94a6;padding:2px 8px;background:#1b212a;border-radius:5px">${esc(d.requested_via)}</span>
            <span style="font:400 11px 'JetBrains Mono';color:#5b6575">${d.status === "downloading" ? "live" : relTime(d.created_at)}</span>
          </div>
        </div>
        <button class="closebtn" data-act="closeDetail" style="background:#1b212a;border:1px solid #2a313d;border-radius:8px;color:#8a94a6;font-size:15px;cursor:pointer;padding:5px 10px;flex-shrink:0">✕</button>
      </div>
    </div>
    <div style="flex:1;overflow-y:auto;padding:20px 22px">
      <div style="font:600 10px 'JetBrains Mono';letter-spacing:.11em;text-transform:uppercase;color:#8a94a6;margin-bottom:12px">Cascade trail</div>
      ${trail}
      <div style="font:600 10px 'JetBrains Mono';letter-spacing:.11em;text-transform:uppercase;color:#8a94a6;margin:8px 0 12px">${factsTitle}</div>
      ${facts}
      <button class="ghost" data-act="toggleLogs" style="margin-top:16px;width:100%;display:flex;align-items:center;justify-content:space-between;background:#0d1014;border:1px solid #2a313d;border-radius:11px;padding:11px 14px;cursor:pointer;color:#8a94a6;transition:border-color .15s">
        <span style="display:flex;align-items:center;gap:8px;font:600 11px 'JetBrains Mono';letter-spacing:.08em;text-transform:uppercase"><span style="font-size:12px">≡</span>Download log</span>
        <span style="font:500 11px 'JetBrains Mono';color:#5b6575">${state.showLogs ? "hide ▴" : "show ▾"}</span>
      </button>
      ${state.showLogs ? `<div style="margin-top:8px;background:#090b0e;border:1px solid #1f2732;border-radius:11px;padding:12px 14px;overflow-x:auto;animation:fadein .2s ease">${buildLogs(d)}</div>` : ""}
    </div>`;
  }
  return `<div data-act="closeDetail" style="position:fixed;inset:0;background:rgba(4,6,9,.55);z-index:50;transition:opacity .28s;opacity:${open ? "1" : "0"};pointer-events:${open ? "auto" : "none"}"></div>
    <aside style="position:fixed;top:0;right:0;bottom:0;width:min(470px,94vw);background:#14181f;border-left:1px solid #2a313d;z-index:51;transform:${open ? "translateX(0)" : "translateX(103%)"};transition:transform .3s cubic-bezier(.3,.8,.3,1);box-shadow:-24px 0 60px rgba(0,0,0,.5);display:flex;flex-direction:column">${inner}</aside>`;
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
        <label style="display:block;font:500 12px 'JetBrains Mono';color:#e8ebf1;margin-bottom:6px">${esc(label)}</label>
        <input data-inp="panelField" data-key="${key}" value="${esc(state.panelValues[key] ?? dflt)}" type="${type}" style="width:100%;background:#0d1014;border:1px solid #2a313d;border-radius:9px;color:#e8ebf1;font:500 13px 'JetBrains Mono';padding:10px 12px"/>
      </div>`,
      )
      .join("");
    const configured = (env: string) => !meta.missing_env.includes(env);
    const creds = def.creds
      .map(([env, label]) => {
        const ok = configured(env);
        return `<div style="margin-bottom:15px">
        <label style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
          <span style="font:500 12px 'JetBrains Mono';color:#e8ebf1">${esc(label)}</span>
          <span style="display:inline-flex;align-items:center;gap:5px;font:500 10px 'JetBrains Mono';color:${ok ? C.good : C.bad}"><span style="width:6px;height:6px;border-radius:50%;background:${ok ? C.good : C.bad}"></span>${ok ? "configured" : "not set"}</span>
        </label>
        <input data-inp="panelField" data-key="${env}" value="${esc(state.panelValues[env] ?? "")}" type="password" placeholder="${ok ? "•••••••• configured — type to replace" : "not set"}" style="width:100%;background:#0d1014;border:1px solid #2a313d;border-radius:9px;color:#e8ebf1;font:500 13px 'JetBrains Mono';padding:10px 12px"/>
        <div style="font:400 10px 'JetBrains Mono';color:#3a4453;margin-top:5px">${env}</div>
      </div>`;
      })
      .join("");
    const tierLabel = pinned
      ? "Tier 3 · pinned fallback"
      : `${enabled ? "Tier " + meta.tier : "Disabled"} · ${cm.text} credentials`;
    inner = `<div style="padding:20px 22px;border-bottom:1px solid #1f2732">
      <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px">
        <div>
          <div style="display:flex;align-items:center;gap:9px"><span style="font:700 18px 'JetBrains Mono';letter-spacing:-.01em">${esc(name)}</span><span style="font:600 9px 'JetBrains Mono';padding:2px 7px;border-radius:5px;background:${loss ? "rgba(110,168,254,.14)" : "rgba(139,148,166,.14)"};color:${loss ? C.accent : C.muted}">${qualityBadge(loss)}</span></div>
          <div style="font:500 11px 'JetBrains Mono';color:${tierColor(meta.tier)};margin-top:7px">${esc(tierLabel)}</div>
        </div>
        <button class="closebtn" data-act="closePanel" style="background:#1b212a;border:1px solid #2a313d;border-radius:8px;color:#8a94a6;font-size:15px;cursor:pointer;padding:5px 10px">✕</button>
      </div>
    </div>
    <div style="flex:1;overflow-y:auto;padding:20px 22px">
      <div style="display:flex;align-items:center;justify-content:space-between;background:#0d1014;border:1px solid #2a313d;border-radius:11px;padding:13px 15px;margin-bottom:20px">
        <div><div style="font:600 13px 'Space Grotesk'">Enabled</div><div style="font:400 11px 'JetBrains Mono';color:#5b6575;margin-top:2px">${pinned ? "Locked on" : enabled ? "Active in the cascade" : "Held in the tray"}</div></div>
        <button data-act="togglePanelEnabled" ${pinned ? "disabled" : ""} style="width:42px;height:24px;border-radius:14px;border:none;cursor:${pinned ? "not-allowed" : "pointer"};background:${enabled ? C.accent : "#2a313d"};position:relative;transition:background .2s;flex-shrink:0"><span style="position:absolute;top:2px;left:${enabled ? "20px" : "2px"};width:20px;height:20px;border-radius:50%;background:#fff;transition:left .2s;box-shadow:0 1px 3px rgba(0,0,0,.4)"></span></button>
      </div>
      ${def.note ? `<div style="background:#0d1014;border:1px solid #2a313d;border-radius:11px;padding:13px 15px;font:400 12px 'JetBrains Mono';color:#8a94a6;line-height:1.5;margin-bottom:20px">${esc(def.note)}</div>` : ""}
      ${settings ? `<div style="font:600 10px 'JetBrains Mono';letter-spacing:.11em;text-transform:uppercase;color:#8a94a6;margin-bottom:11px">Settings</div>${settings}` : ""}
      ${creds ? `<div style="font:600 10px 'JetBrains Mono';letter-spacing:.11em;text-transform:uppercase;color:#8a94a6;margin:4px 0 11px;display:flex;align-items:center;gap:8px">Credentials <span style="font-weight:400;text-transform:none;letter-spacing:0;color:#5b6575">· encrypted at rest, never displayed</span></div>${creds}` : ""}
    </div>
    <div style="padding:16px 22px;border-top:1px solid #1f2732;display:flex;align-items:center;gap:12px">
      <button data-act="savePanel" ${saving ? "disabled" : ""} style="flex:1;background:${saved ? "rgba(74,222,128,.16)" : saving ? "#2a313d" : C.accent};border:none;border-radius:10px;color:${saved ? C.good : saving ? C.muted : "#0d1014"};font:600 13px 'Space Grotesk';padding:11px;cursor:${saving ? "default" : "pointer"};transition:.15s;display:flex;align-items:center;justify-content:center;gap:8px">${saving ? "Saving…" : saved ? "Saved ✓" : dirty ? "Save changes" : "Save"}</button>
      ${def.validate ? `<button class="ghost" data-act="validatePanel" style="background:#1b212a;border:1px solid #2a313d;border-radius:10px;color:#e8ebf1;font:600 13px 'Space Grotesk';padding:11px 15px;cursor:pointer">Test</button>` : ""}
    </div>`;
  }
  return `<div data-act="closePanel" style="position:fixed;inset:0;background:rgba(4,6,9,.55);z-index:52;transition:opacity .28s;opacity:${open ? "1" : "0"};pointer-events:${open ? "auto" : "none"}"></div>
    <aside style="position:fixed;top:0;right:0;bottom:0;width:min(440px,94vw);background:#14181f;border-left:1px solid #2a313d;z-index:53;transform:${open ? "translateX(0)" : "translateX(103%)"};transition:transform .3s cubic-bezier(.3,.8,.3,1);box-shadow:-24px 0 60px rgba(0,0,0,.5);display:flex;flex-direction:column">${inner}</aside>`;
}

function headerHtml(): string {
  const saveMap: Record<SaveState, [string, string, string, string, boolean]> = {
    idle: ["All changes saved", C.muted, "#14181f", "#2a313d", false],
    saving: ["Saving…", C.accent, "rgba(110,168,254,.08)", "rgba(110,168,254,.3)", true],
    saved: ["Saved ✓", C.good, "rgba(74,222,128,.1)", "rgba(74,222,128,.35)", false],
  };
  const [txt, col, bg, bd, spin] = saveMap[state.saveState];
  const live = !!state.stats;
  return `<header style="position:sticky;top:0;z-index:40;background:rgba(13,16,20,.82);backdrop-filter:blur(14px);border-bottom:1px solid #1f2732">
    <div style="max-width:1560px;margin:0 auto;padding:0 28px;display:flex;align-items:center;gap:16px">
      <div style="display:flex;align-items:center;gap:10px;padding:14px 0">
        <span style="width:22px;height:22px;border-radius:6px;background:linear-gradient(135deg,#6ea8fe,#34d3b5);display:inline-flex;align-items:center;justify-content:center;font:700 12px 'JetBrains Mono';color:#0d1014">♪</span>
        <span style="font:700 15px 'Space Grotesk';letter-spacing:.02em">lbmd <span style="color:#8a94a6;font-weight:500">console</span></span>
      </div>
      <div style="margin-left:auto;display:flex;align-items:center;gap:10px">
        <div style="display:flex;align-items:center;gap:8px;padding:7px 13px;border-radius:20px;background:${bg};border:1px solid ${bd};transition:.2s">
          ${spin ? `<span style="width:11px;height:11px;border:2px solid #2a313d;border-top-color:#6ea8fe;border-radius:50%;animation:spin .8s linear infinite"></span>` : ""}
          <span style="font:600 11px 'JetBrains Mono';color:${col}">${txt}</span>
        </div>
        <div style="display:flex;align-items:center;gap:8px;padding:7px 12px;background:#14181f;border:1px solid #2a313d;border-radius:20px">
          <span style="width:8px;height:8px;border-radius:50%;background:${live ? C.good : C.faint};box-shadow:0 0 0 3px ${live ? "rgba(74,222,128,.15)" : "transparent"};animation:pulse 2s ease-in-out infinite"></span>
          <span style="font:500 12px 'JetBrains Mono';color:#8a94a6">${live ? "live" : "connecting…"}</span>
        </div>
      </div>
    </div>
  </header>`;
}

// Pure view: State -> HTML. The renderer reconciles it into the DOM (morph), so
// this can run every tick without wiping the page.
function view(): string {
  if (state.error) {
    return `<div style="max-width:600px;margin:80px auto;padding:24px;color:#f87171;font:500 14px 'Space Grotesk';text-align:center">Failed to load console: ${esc(state.error)}</div>`;
  }
  return `${headerHtml()}
    <main style="max-width:1560px;margin:0 auto;padding:20px 28px 72px;animation:fadein .25s ease">
      ${statCardsHtml()}
      <div class="grid2" style="display:grid;grid-template-columns:1.55fr 1fr;gap:16px;align-items:start">
        ${cascadeHtml()}
        <div style="display:flex;flex-direction:column;gap:14px">${liveHtml()}${winsHtml()}${historyHtml()}</div>
      </div>
    </main>
    ${drawerHtml()}
    ${panelHtml()}
    ${state.toast ? `<div style="position:fixed;bottom:26px;left:50%;transform:translateX(-50%);z-index:60;display:flex;align-items:center;gap:10px;background:#1b212a;border:1px solid rgba(74,222,128,.35);border-radius:12px;padding:12px 18px;box-shadow:0 12px 40px rgba(0,0,0,.5);animation:toastin .3s cubic-bezier(.3,.8,.3,1)"><span style="width:8px;height:8px;border-radius:50%;background:#4ade80"></span><span style="font:600 13px 'JetBrains Mono';color:#e8ebf1">${esc(state.toast)}</span></div>` : ""}`;
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

// Events are delegated on the root ONCE — they survive every reconcile, so no
// rebinding per render (that was part of what made the old full-refresh churn).
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
    else if (act === "validatePanel") toast(`${state.panelSource} — validation not wired yet`);
  });

  root.addEventListener("input", (e) => {
    const el = e.target as HTMLInputElement;
    if (el.dataset.inp === "search") {
      state.histSearch = el.value.trim();
      window.clearTimeout(histTimer);
      histTimer = window.setTimeout(() => void loadHistory().then(render), 250);
    } else if (el.dataset.inp === "panelField") {
      state.panelValues[el.dataset.key!] = el.value;
      state.panelSave = "dirty";
      render(); // reflect Save button state; morph keeps the focused field intact
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
async function boot(): Promise<void> {
  bindEvents(); // once
  render(); // header/skeleton
  try {
    await Promise.all([loadStatic(), loadHistory()]);
    state.stats = await api.stats();
  } catch (err) {
    state.error = (err as Error).message;
  }
  render();

  // side effects — run once
  effect(connectLive);
  effect(() => {
    window.setInterval(() => {
      state.tick++;
      if (state.inflight.length) render(); // tick live elapsed (patches only those nodes)
    }, 1000);
    window.setInterval(() => void loadHistory().then(render), 15000); // pick up completions
  });
}

void boot();
