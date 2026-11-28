/*
 * Shared bits for the timeline lanes: their heights (the label gutter in
 * `Timeline` lines up with them), pointer → lane x, and canvas drawing.
 */

export const LANE_HEIGHT = {
  ruler: 24,
  frames: 48,
  hook: 56,
  markers: 32,
  takes: 56,
  game: 56,
} as const;

/** Horizontal position of a pointer event inside `el`, in CSS px. */
export function laneX(e: { clientX: number }, el: Element): number {
  return e.clientX - el.getBoundingClientRect().left;
}

/** Resolve a design token (e.g. `--accent`) for canvas drawing. */
export function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/**
 * Size `canvas` to `widthPx` x `heightPx` CSS px at the device pixel ratio
 * and return a cleared context that draws in CSS px.
 */
export function sizeCanvas(
  canvas: HTMLCanvasElement,
  widthPx: number,
  heightPx: number,
): CanvasRenderingContext2D | null {
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.round(widthPx * dpr));
  canvas.height = Math.max(1, Math.round(heightPx * dpr));
  canvas.style.width = `${widthPx}px`;
  canvas.style.height = `${heightPx}px`;
  const g = canvas.getContext("2d");
  if (!g) return null;
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, widthPx, heightPx);
  return g;
}

/** One centred bar per column; `peaks` are 0..1 of half the height (clipped). */
export function drawMirrorBars(
  g: CanvasRenderingContext2D,
  peaks: ArrayLike<number>,
  heightPx: number,
  color: string,
): void {
  const mid = heightPx / 2;
  const half = mid - 1;
  g.fillStyle = color;
  for (let c = 0; c < peaks.length; c++) {
    const h = Math.max(0.5, Math.min(1, peaks[c] ?? 0) * half);
    g.fillRect(c, mid - h, 1, h * 2);
  }
}
