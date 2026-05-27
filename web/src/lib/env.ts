/*
 * Single source of truth for runtime config. In dev, Vite proxies
 * `/api` and `/workspace` to the backend, so the empty default
 * works without configuration. Override with VITE_API_URL only when
 * pointing at a non-proxied backend (e.g. production deployment).
 */

export const env = {
  // Empty in dev (use the Vite proxy). Absolute URL in prod.
  apiBaseUrl: import.meta.env.VITE_API_URL ?? "",
} as const;
