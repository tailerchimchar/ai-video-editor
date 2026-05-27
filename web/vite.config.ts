import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// Vite proxies `/api` and `/workspace` to the backend (default :8000) so dev
// is same-origin — no CORS middleware needed on the API. Override the target
// with `VITE_API_URL` for non-default API ports during local development.
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
