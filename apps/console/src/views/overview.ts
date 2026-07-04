import { api, type Source } from "../api";
import { bytes, pct, esc } from "../format";
import { barList } from "../charts";

function card(label: string, value: string | number, sub = ""): string {
  return `<div class="card">
    <div class="card-val">${esc(value)}</div>
    <div class="card-label">${esc(label)}</div>
    ${sub ? `<div class="card-sub">${esc(sub)}</div>` : ""}
  </div>`;
}

function sourcesTable(sources: Source[]): string {
  const cred: Record<string, string> = {
    ok: "good",
    partial: "warn",
    missing: "bad",
    none: "muted",
  };
  const rows = sources
    .map(
      (s) => `<tr>
        <td><span class="dot ${s.enabled ? "on" : "off"}"></span>${esc(s.name)}</td>
        <td>T${s.tier}</td>
        <td>${s.lossless ? "FLAC" : "any"}</td>
        <td><span class="badge ${cred[s.credentials] ?? "muted"}">${esc(s.credentials)}</span></td>
      </tr>`,
    )
    .join("");
  return `<table class="mini">
    <thead><tr><th>Source</th><th>Tier</th><th>Quality</th><th>Creds</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

export async function overview(root: HTMLElement): Promise<void> {
  const [s, sources] = await Promise.all([api.stats(), api.sources()]);

  const wins = Object.entries(s.wins_by_source).sort((a, b) => b[1] - a[1]);
  const tiers = Object.entries(s.wins_by_tier)
    .sort()
    .map(([t, n]) => [`Tier ${t}`, n] as [string, number]);
  const reliability = Object.entries(s.attempts_by_source)
    .map(([name, a]) => [name, a.total ? Math.round((a.ok / a.total) * 100) : 0] as [string, number])
    .sort((a, b) => b[1] - a[1]);

  root.innerHTML = `
    <div class="cards">
      ${card("Requests", s.total)}
      ${card("Success rate", pct(s.success_rate), `${s.done} done · ${s.failed} failed`)}
      ${card("In flight", s.in_flight)}
      ${card("Lossless", pct(s.lossless_rate), `${s.lossless} FLAC`)}
      ${card("Downloaded", bytes(s.total_bytes))}
      ${card("Avg time", s.avg_elapsed_s ? `${s.avg_elapsed_s.toFixed(1)}s` : "—")}
    </div>
    <div class="panels">
      <section class="panel"><h3>Wins by source</h3>${barList(wins)}</section>
      <section class="panel"><h3>Wins by tier</h3>${barList(tiers, "var(--good)")}</section>
      <section class="panel"><h3>Source reliability (% ok)</h3>${barList(reliability, "var(--warn)")}</section>
      <section class="panel"><h3>Sources &amp; tiers</h3>${sourcesTable(sources)}</section>
    </div>`;
}
