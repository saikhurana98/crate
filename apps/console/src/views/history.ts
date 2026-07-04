import { type RequestRow, type RequestDetail, api } from "../api";
import { ago, bytes, dur, esc } from "../format";

const PAGE = 50;

function statusBadge(status: string): string {
  const cls = status === "done" ? "good" : status === "failed" ? "bad" : "warn";
  return `<span class="badge ${cls}">${esc(status)}</span>`;
}

function row(r: RequestRow): string {
  const won = r.source_won ? `T${r.tier_won} · ${esc(r.source_won)}` : "—";
  const quality = r.lossless === 1 ? "FLAC" : r.quality_label ? esc(r.quality_label) : "—";
  return `<tr data-id="${esc(r.id)}">
    <td class="q">${esc(r.query)}</td>
    <td>${statusBadge(r.status)}</td>
    <td>${won}</td>
    <td>${quality}</td>
    <td>${bytes(r.size_bytes)}</td>
    <td>${ago(r.created_at)}</td>
  </tr>`;
}

function drawer(d: RequestDetail): string {
  const trail = d.attempts
    .map(
      (a) => `<li class="${a.ok ? "ok" : "no"}">
        <span class="mono">T${a.tier} ${esc(a.source)}</span> ${a.ok ? "✓" : "✗"}
        <span class="muted">${esc(a.detail)}</span></li>`,
    )
    .join("");
  const dl = d.download
    ? `<dl class="facts">
        <dt>File</dt><dd class="mono">${esc(d.download.path)}</dd>
        <dt>Quality</dt><dd>${esc(d.download.quality_label)}</dd>
        <dt>Codec</dt><dd>${esc(d.download.codec)}${d.download.bit_depth ? ` · ${d.download.bit_depth}-bit` : ""}${d.download.sample_rate ? ` · ${(d.download.sample_rate / 1000).toFixed(1)} kHz` : ""}</dd>
        <dt>Size</dt><dd>${bytes(d.download.size_bytes)}</dd>
        <dt>Duration</dt><dd>${dur(d.download.duration_s)}</dd>
        <dt>Elapsed</dt><dd>${d.download.elapsed_s != null ? `${d.download.elapsed_s.toFixed(1)}s` : "—"}</dd>
      </dl>`
    : d.error
      ? `<p class="error-text">${esc(d.error)}</p>`
      : `<p class="muted">In progress.</p>`;
  return `<div class="drawer-inner">
    <button class="close" id="drawer-close">✕</button>
    <h3>${esc(d.query)}</h3>
    <p class="muted">${esc(d.requested_via)} · ${esc(d.status)} · ${ago(d.created_at)}</p>
    <h4>Cascade trail</h4><ul class="trail">${trail || "<li class='muted'>No attempts.</li>"}</ul>
    <h4>Winner</h4>${dl}
  </div>`;
}

export async function history(root: HTMLElement): Promise<void> {
  const state = { q: "", status: "", offset: 0 };

  root.innerHTML = `
    <div class="toolbar">
      <input id="search" type="search" placeholder="Search query…" />
      <select id="status">
        <option value="">All statuses</option>
        <option value="done">Done</option>
        <option value="failed">Failed</option>
        <option value="downloading">Downloading</option>
      </select>
      <span class="grow"></span>
      <button id="prev" class="btn">‹ Prev</button>
      <button id="next" class="btn">Next ›</button>
    </div>
    <section class="panel"><div id="rows"></div></section>
    <div id="drawer" class="drawer hidden"></div>`;

  const rows = root.querySelector<HTMLElement>("#rows")!;
  const drawerEl = root.querySelector<HTMLElement>("#drawer")!;
  const search = root.querySelector<HTMLInputElement>("#search")!;
  const status = root.querySelector<HTMLSelectElement>("#status")!;
  const prev = root.querySelector<HTMLButtonElement>("#prev")!;
  const next = root.querySelector<HTMLButtonElement>("#next")!;

  async function load(): Promise<void> {
    const params = new URLSearchParams({ limit: String(PAGE), offset: String(state.offset) });
    if (state.q) params.set("q", state.q);
    if (state.status) params.set("status", state.status);
    rows.innerHTML = `<p class="muted">Loading…</p>`;
    const data = await api.requests(params);
    prev.disabled = state.offset === 0;
    next.disabled = data.length < PAGE;
    rows.innerHTML = data.length
      ? `<table class="click">
          <thead><tr><th>Query</th><th>Status</th><th>Won</th><th>Quality</th><th>Size</th><th>When</th></tr></thead>
          <tbody>${data.map(row).join("")}</tbody></table>`
      : `<p class="muted">No requests match.</p>`;
    rows.querySelectorAll<HTMLElement>("tr[data-id]").forEach((tr) => {
      tr.addEventListener("click", () => void openDrawer(tr.dataset.id!));
    });
  }

  async function openDrawer(id: string): Promise<void> {
    drawerEl.classList.remove("hidden");
    drawerEl.innerHTML = `<div class="drawer-inner"><p class="muted">Loading…</p></div>`;
    const detail = await api.request(id);
    drawerEl.innerHTML = drawer(detail);
    drawerEl.querySelector("#drawer-close")!.addEventListener("click", () => {
      drawerEl.classList.add("hidden");
    });
  }

  let debounce: number | undefined;
  search.addEventListener("input", () => {
    window.clearTimeout(debounce);
    debounce = window.setTimeout(() => {
      state.q = search.value.trim();
      state.offset = 0;
      void load();
    }, 250);
  });
  status.addEventListener("change", () => {
    state.status = status.value;
    state.offset = 0;
    void load();
  });
  prev.addEventListener("click", () => {
    state.offset = Math.max(0, state.offset - PAGE);
    void load();
  });
  next.addEventListener("click", () => {
    state.offset += PAGE;
    void load();
  });

  await load();
}
