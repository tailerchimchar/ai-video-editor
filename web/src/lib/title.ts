/*
 * Parsers for the Outplayed-style filenames we get from `output_path`.
 *
 * A typical path:
 *   .../compilations/League of Legends_05-18-2026_20-29-54-0_20260525-200359/compilation.mp4
 *
 * Two dates live in there:
 *   - The GAME date: `05-18-2026` (when the user played)
 *   - The COMPILE date: `20260525-200359` (when we rendered it; trailing block after _ )
 *
 * Plus the game name prefix and a per-session ordinal. Use these helpers
 * to render a human title instead of dumping the whole path on screen.
 */

export interface ParsedTitle {
  /** "League of Legends", "Valorant", or whatever leads the filename. */
  game: string;
  /** Display string for the GAME date, e.g. "May 18, 2026". */
  gameDate: string;
  /** Display string for the compile date, e.g. "May 25 · 20:03". */
  compileTime: string;
  /** Raw stem in case the caller still wants it as fallback. */
  raw: string;
}

/**
 * Pull a structured title out of a compilation folder name.
 *
 * The Outplayed filename format is brittle (no real schema). We do
 * defensive parsing: any field that fails to parse falls back to the
 * raw stem so we never silently mis-label a compilation.
 */
export function parseCompilationTitle(folderStem: string | null | undefined): ParsedTitle {
  const raw = folderStem ?? "";
  if (!raw) {
    return { game: "Compilation", gameDate: "", compileTime: "", raw: "" };
  }

  // The compile timestamp suffix is always `_YYYYMMDD-HHMMSS` at the end.
  const compileMatch = raw.match(/_(\d{8})-(\d{6})$/);
  let body = raw;
  let compileTime = "";
  if (compileMatch) {
    body = raw.slice(0, compileMatch.index);
    const [, ymd, hms] = compileMatch;
    if (ymd && hms) compileTime = formatCompileTime(ymd, hms);
  }

  // After stripping the compile suffix, the next-from-end token is the
  // Outplayed counter (e.g. `-0`). Strip it.
  body = body.replace(/-\d+$/, "");

  // The remaining shape is: `<game>_MM-DD-YYYY_HH-MM-SS`. Pull the
  // game-date and clip the time block (we only want the date).
  const gameMatch = body.match(/^(.+?)_(\d{2})-(\d{2})-(\d{4})/);
  if (gameMatch) {
    const [, game, mm, dd, yyyy] = gameMatch;
    return {
      game: (game ?? "Compilation").trim(),
      gameDate: formatGameDate(yyyy ?? "", mm ?? "", dd ?? ""),
      compileTime,
      raw,
    };
  }

  // Fallback: dump everything as the game name.
  return { game: body, gameDate: "", compileTime, raw };
}

function formatGameDate(yyyy: string, mm: string, dd: string): string {
  const d = new Date(`${yyyy}-${mm}-${dd}T00:00:00`);
  if (Number.isNaN(d.getTime())) return `${mm}/${dd}/${yyyy}`;
  return d.toLocaleDateString(undefined, {
    month: "long",
    day: "numeric",
    year: "numeric",
  });
}

function formatCompileTime(ymd: string, hms: string): string {
  const year = ymd.slice(0, 4);
  const month = ymd.slice(4, 6);
  const day = ymd.slice(6, 8);
  const hour = hms.slice(0, 2);
  const min = hms.slice(2, 4);
  const d = new Date(`${year}-${month}-${day}T${hour}:${min}:00`);
  if (Number.isNaN(d.getTime())) return `${month}/${day} · ${hour}:${min}`;
  return `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })} · ${hour}:${min}`;
}
