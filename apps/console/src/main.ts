import "./styles.css";
import { overview } from "./views/overview";
import { live } from "./views/live";
import { history } from "./views/history";
import { esc } from "./format";

export type View = (root: HTMLElement) => void | (() => void) | Promise<void | (() => void)>;

const routes: Record<string, View> = {
  "#/overview": overview,
  "#/live": live,
  "#/history": history,
};

const app = document.getElementById("app")!;
let cleanup: void | (() => void);

async function render(): Promise<void> {
  if (typeof cleanup === "function") cleanup();
  cleanup = undefined;

  const hash = location.hash || "#/overview";
  const view = routes[hash] ?? overview;
  document.querySelectorAll("nav a").forEach((a) => {
    a.classList.toggle("active", a.getAttribute("href") === (routes[hash] ? hash : "#/overview"));
  });

  app.innerHTML = `<div class="loading">Loading…</div>`;
  try {
    cleanup = await view(app);
  } catch (err) {
    app.innerHTML = `<div class="error">Failed to load: ${esc((err as Error).message)}</div>`;
  }
}

window.addEventListener("hashchange", render);
void render();
