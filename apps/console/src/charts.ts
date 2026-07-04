// Tiny hand-rolled bar list — no charting dependency.
import { esc } from "./format";

export function barList(data: [string, number][], color = "var(--accent)"): string {
  if (data.length === 0) return `<p class="muted">No data yet.</p>`;
  const max = Math.max(1, ...data.map((d) => d[1]));
  return (
    `<div class="bars">` +
    data
      .map(
        ([label, val]) => `
      <div class="bar-row">
        <span class="bar-label">${esc(label)}</span>
        <span class="bar-track"><span class="bar-fill" style="width:${((val / max) * 100).toFixed(1)}%;background:${color}"></span></span>
        <span class="bar-val">${val}</span>
      </div>`,
      )
      .join("") +
    `</div>`
  );
}
