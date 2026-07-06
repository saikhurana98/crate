// crate console service worker — offline app shell.
//
// Strategy:
//   - navigations (the HTML shell): NETWORK-FIRST, fall back to cache when
//     offline. This is what makes a new deploy show up immediately — a
//     cache-first shell would pin users to the old app until a second reload.
//   - hashed static assets (JS/CSS/icons): cache-first (they're immutable —
//     Vite fingerprints the filename, so a new build = a new URL).
//   - /api (live data + SSE): never touched, always the network.
const CACHE = "crate-shell-v2";

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

async function cachePut(req, res) {
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

  const isNavigation = req.mode === "navigate" || url.pathname === "/";

  if (isNavigation) {
    // network-first: always try to fetch the freshest shell, cache it, and only
    // fall back to the cached shell (or "/") when the network is unavailable.
    e.respondWith(
      (async () => {
        try {
          return await cachePut(req, await fetch(req));
        } catch {
          return (await caches.match(req)) || (await caches.match("/")) || Response.error();
        }
      })(),
    );
    return;
  }

  // cache-first for immutable, fingerprinted assets
  e.respondWith(
    (async () => {
      const cached = await caches.match(req);
      if (cached) return cached;
      try {
        return await cachePut(req, await fetch(req));
      } catch {
        return Response.error();
      }
    })(),
  );
});
