/*
 * Camera-box geometry — pure functions, no React, no DOM.
 *
 * Boxes are fractions of the video frame (0..1 on both axes, top-left
 * origin), matching `CameraBox` in narration/models.py. Width bounds
 * mirror the model's `width_frac` validation (0.08..0.5).
 */

import type { CameraBox, PixelBox } from "@/types/narration";

export const MIN_WIDTH_FRAC = 0.08;
export const MAX_WIDTH_FRAC = 0.5;

/** A box in frame fractions: `w` is a fraction of frame width, `h` of frame height. */
export interface FracBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** Which corner stays put while the opposite one is dragged. */
export type ResizeAnchor = "top-left" | "bottom-right";

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

/** Pixel box laid out on `frame` → fractions of that frame. */
export function pixelToFrac(px: PixelBox, frame: { w: number; h: number }): FracBox {
  return { x: px.x / frame.w, y: px.y / frame.h, w: px.w / frame.w, h: px.h / frame.h };
}

/** Translate by a fractional delta, keeping the whole box inside the frame. */
export function moveBox(box: FracBox, dx: number, dy: number): FracBox {
  return {
    ...box,
    x: clamp(box.x + dx, 0, 1 - box.w),
    y: clamp(box.y + dy, 0, 1 - box.h),
  };
}

/**
 * Grow/shrink by `dw` (fraction of frame width), keeping the aspect
 * ratio and the `anchor` corner fixed. `dw > 0` grows the box.
 */
export function resizeBox(box: FracBox, dw: number, anchor: ResizeAnchor): FracBox {
  const ratio = box.h / box.w; // height-fraction per width-fraction
  const right = box.x + box.w;
  const bottom = box.y + box.h;
  // Room available in the direction we grow, in width-fraction units.
  const roomW =
    anchor === "bottom-right"
      ? Math.min(right, bottom / ratio)
      : Math.min(1 - box.x, (1 - box.y) / ratio);
  const w = clamp(box.w + dw, MIN_WIDTH_FRAC, Math.min(MAX_WIDTH_FRAC, roomW));
  const h = w * ratio;
  return anchor === "bottom-right"
    ? { x: right - w, y: bottom - h, w, h }
    : { x: box.x, y: box.y, w, h };
}

/** Write a fractional box back into the owner's `CameraBox` (other fields kept). */
export function applyToCamera(camera: CameraBox, box: FracBox): CameraBox {
  return { ...camera, x: round4(box.x), y: round4(box.y), width_frac: round4(box.w) };
}

/** The `CameraBox` that means "use the game profile default position and size". */
export function resetToDefault(camera: CameraBox): CameraBox {
  return { ...camera, x: null, y: null, width_frac: null };
}

function round4(v: number): number {
  return Math.round(v * 10_000) / 10_000;
}
