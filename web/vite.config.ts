import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// Vite proxies `/api` and `/workspace` to the backend (default :8000) so dev
// is same-origin. `VITE_API_URL` is not a proxy override: it is the client's
// API base URL (src/lib/env.ts), bypasses the proxy and needs CORS on the API.
// The constant below is only the proxy target.
const API_TARGET = process.env.VITE_API_URL ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: API_TARGET,
        changeOrigin: true,
      },
      // The `<video>` element streams `compilation.mp4` over this mount.
      // Range requests pass through transparently — the proxy preserves
      // headers so HTTP 206 Partial Content works end-to-end.
      "/workspace": {
        target: API_TARGET,
        changeOrigin: true,
      },
    },
  },
});
