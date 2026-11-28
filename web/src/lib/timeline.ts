/*
 * Timeline viewport maths — pure functions, no React, no DOM.
 *
 * A `Viewport` maps seconds to pixels: `scrollS` is the second at the left
 * edge, `pxPerS` the zoom, `widthPx` the visible width.
 */

export interface Viewport {
  pxPerS: number;
  scrollS: number;
  widthPx: number;
}

export const MAX_PX_PER_S = 400;
const TICK_STEPS_S = [1, 2, 5, 10, 15, 30, 60, 120, 300];
const MIN_TICK_PX = 80;

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

export const sToPx = (v: Viewport, s: number): number => (s - v.scrollS) * v.pxPerS;

export const pxToS = (v: Viewport, px: number): number => v.scrollS + px / v.pxPerS;

/** Keep `scrollS` within `[0, durationS - visible seconds]`. */
export function clampScroll(v: Viewport, durationS: number): Viewport {
  const max = Math.max(0, durationS - v.widthPx / v.pxPerS);
  return { ...v, scrollS: clamp(v.scrollS, 0, max) };
}

/** Whole video visible. */
export function fit(widthPx: number, durationS: number): Viewport {
  return { pxPerS: widthPx / durationS, scrollS: 0, widthPx };
}

/** Zoom by `factor`, keeping the second under `anchorPx` where it is. */
export function zoomAround(
  v: Viewport,
  factor: number,
  anchorPx: number,
  durationS: number,
): Viewport {
  const pxPerS = clamp(v.pxPerS * factor, v.widthPx / durationS, MAX_PX_PER_S);
  const anchorS = v.scrollS + anchorPx / v.pxPerS;
  return clampScroll({ ...v, pxPerS, scrollS: anchorS - anchorPx / pxPerS }, durationS);
}

/** Seconds between ruler ticks: the first step that is at least 80 px wide. */
export function tickEvery(pxPerS: number): number {
  return TICK_STEPS_S.find((s) => s * pxPerS >= MIN_TICK_PX) ?? 300;
}
