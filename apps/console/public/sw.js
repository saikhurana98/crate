// crate console service worker — offline app shell.
// Caches same-origin static assets (index, hashed JS/CSS, icons) so the console
// opens instantly and works offline as an installed app. NEVER touches /api
// (live data + SSE must always hit the network).
const CACHE = "crate-shell-v1";

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (e) => {
  e.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)));
      await self.clients.claim();
    })(),
  );
});

async function fetchAndCache(req) {
  const res = await fetch(req);
  if (res && res.ok && res.type === "basic") {
    const cache = await caches.open(CACHE);
    cache.put(req, res.clone());
  }
  return res;
}

self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);
  // only handle same-origin GETs; skip API/SSE and cross-origin (fonts) entirely
  if (req.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return;
  e.respondWith(
    (async () => {
      const cached = await caches.match(req);
      if (cached) {
        fetchAndCache(req).catch(() => {}); // refresh in the background
        return cached;
      }
      try {
        return await fetchAndCache(req);
      } catch {
        // offline navigation → fall back to the cached app shell
        if (req.mode === "navigate") {
          const shell = await caches.match("/");
          if (shell) return shell;
        }
        throw new Error("offline and not cached");
      }
    })(),
  );
});
