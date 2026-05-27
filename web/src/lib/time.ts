/*
 * Time formatting helpers. Match the backend semantics in
 * `api/src/ai_video_editor/compile.py::_parse_time` so any string
 * the user can paste into MCP also parses on the client.
 *
 * Backend accepts: "M:SS", "H:MM:SS", or plain seconds. We expose
 * the same surface here for when Phase 2's timeline-click needs to
 * produce a `clip_ref`.
 */

/** Format seconds as `M:SS` (or `H:MM:SS` past an hour). Always returns the canonical form. */
export function mmss(totalSeconds: number): string {
  const sec = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const pad = (n: number) => n.toString().padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

/** Parse `"M:SS"` / `"H:MM:SS"` / plain seconds into seconds. Returns `null` if unparseable. */
export function parseClipRef(input: string): number | null {
  const trimmed = input.trim();
  if (!trimmed) return null;
  if (trimmed.includes(":")) {
    const parts = trimmed.split(":");
    if (!parts.every((p) => /^\d+(\.\d+)?$/.test(p))) return null;
    if (parts.length === 2) {
      const [m, s] = parts as [string, string];
      return Number(m) * 60 + Number(s);
    }
    if (parts.length === 3) {
      const [h, m, s] = parts as [string, string, string];
      return Number(h) * 3600 + Number(m) * 60 + Number(s);
    }
    return null;
  }
  const n = Number(trimmed);
  return Number.isFinite(n) ? n : null;
}

/** Render a duration as "7.32s" or "1m 12s". Used for clip durations in the UI. */
export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(2)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}m ${s.toString().padStart(2, "0")}s`;
}

/** Human-readable relative time ("3 days ago"). Browser-locale-aware. */
export function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return iso;
  const diffSec = Math.round((then - Date.now()) / 1000);
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  const abs = Math.abs(diffSec);
  if (abs < 60) return rtf.format(diffSec, "second");
  if (abs < 3600) return rtf.format(Math.round(diffSec / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(diffSec / 3600), "hour");
  if (abs < 86400 * 30) return rtf.format(Math.round(diffSec / 86400), "day");
  if (abs < 86400 * 365) return rtf.format(Math.round(diffSec / (86400 * 30)), "month");
  return rtf.format(Math.round(diffSec / (86400 * 365)), "year");
}
