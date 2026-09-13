import { defineConfig } from "vite";

// base './' so the built assets load under whatever path Caddy serves the UI at.
// Dev proxies /api to the local `crate console` (uvicorn on :8788), including SSE.
export default defineConfig({
  base: "./",
  server: {
    proxy: {
      // ws: true also proxies the /api/ws/jobs WebSocket upgrade (live job log).
      "/api": { target: "http://localhost:8788", changeOrigin: true, ws: true },
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
