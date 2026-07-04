import { defineConfig } from "vite";

// base './' so the built assets load under whatever path Caddy serves the UI at.
// Dev proxies /api to the local `lbmd console` (uvicorn on :8788), including SSE.
export default defineConfig({
  base: "./",
  server: {
    proxy: {
      "/api": { target: "http://localhost:8788", changeOrigin: true },
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
