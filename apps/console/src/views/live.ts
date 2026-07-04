import { type LiveEvent, type RequestRow, api } from "../api";
import { ago, pct, esc } from "../format";

function inFlightTable(rows: RequestRow[]): string {
  if (rows.length === 0) return `<p class="muted">Nothing downloading right now.</p>`;
  return `<table>
    <thead><tr><th>Query</th><th>Via</th><th>Started</th></tr></thead>
    <tbody>${rows
      .map(
        (r) => `<tr>
          <td><span class="spinner"></span>${esc(r.query)}</td>
          <td>${esc(r.requested_via)}</td>
          <td>${ago(r.created_at)}</td>
        </tr>`,
      )
      .join("")}</tbody></table>`;
}

export function live(root: HTMLElement): () => void {
  root.innerHTML = `
    <div class="live-head">
      <span id="live-dot" class="dot off"></span>
      <span id="live-status">connecting…</span>
      <span class="grow"></span>
      <span id="live-mini" class="muted"></span>
    </div>
    <section class="panel"><h3>In flight</h3><div id="live-body"></div></section>`;

  const dot = root.querySelector<HTMLElement>("#live-dot")!;
  const status = root.querySelector<HTMLElement>("#live-status")!;
  const mini = root.querySelector<HTMLElement>("#live-mini")!;
  const body = root.querySelector<HTMLElement>("#live-body")!;

  const paint = (e: LiveEvent): void => {
    body.innerHTML = inFlightTable(e.in_flight);
    mini.textContent = `${e.stats.total} total · ${pct(e.stats.success_rate)} success · ${e.stats.in_flight} in flight`;
  };

  const source = new EventSource(api.eventsUrl);
  source.onopen = () => {
    dot.className = "dot on";
    status.textContent = "live";
  };
  source.onmessage = (ev) => {
    try {
      paint(JSON.parse(ev.data) as LiveEvent);
    } catch {
      /* ignore malformed frame */
    }
  };
  source.onerror = () => {
    dot.className = "dot off";
    status.textContent = "reconnecting…";
  };

  return () => source.close();
}
