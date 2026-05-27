/*
 * Clip type — hand-mirrored from `api/src/ai_video_editor/compile.py`.
 *
 * Optional fields (`speaker`, `style`, `effects`) are forward-compat
 * for later phases (multi-speaker captions in Phase 4, screen-shake
 * in Phase 5). Renderers tolerate them being absent — the backend
 * doesn't emit them yet.
 */

export type Effect =
  | { kind: "zoom"; factor: number; roi: string }
  | { kind: "focus"; x: number; y: number; radius: number; dim: number }
  | { kind: "caption"; text: string };

export type EventType = "clip" | "intro" | "kill" | "manual" | string;

export interface SpeakerStyle {
  // Forward-compat for Phase 4. Backend doesn't emit yet.
  color?: string;
  weight?: "regular" | "bold";
}

/**
 * Clip card data as returned by GET /edit/compile/:id/clips.
 *
 * Reel and source ranges arrive as "M:SS" strings from the backend's
 * `_summarise_clips` helper, NOT raw seconds. Treat them as display
 * strings; the underlying source seconds are NOT exposed by this
 * endpoint — extend mutations work in seconds, so we parse client-side
 * via `lib/time.ts` when needed.
 */
export interface CaptionWord {
  word: string;
  start: number;
  end: number;
}

export interface CaptionStyle {
  /** Visual preset — picks fontsize/position/colors from the renderer's preset table. */
  preset?: "default" | "tiktok";
  /** Per-field overrides (fontsize, color, etc.) — leave undefined to inherit preset. */
  fontsize?: number;
  color?: string;
  y_position?: string;
  border_width?: number;
  border_color?: string;
}

export interface CaptionSegment {
  start_seconds: number;
  end_seconds: number;
  text: string;
  /** Whisper word-level timing; absent when even-split fallback. */
  words?: CaptionWord[];
  /** Per-segment style — preset + optional overrides. */
  style?: CaptionStyle;
  /** Optional sentiment (per the transcripts table). */
  sentiment_score?: number;
}

export type CaptionMode = "segment" | "tiktok";

export interface Clip {
  index: number;
  id: string;
  reel: string;
  source: string;
  duration: number;
  event: EventType;
  effects: Effect[];
  /** Full caption segments with optional word timings — for the editor. */
  caption_segments: CaptionSegment[];
  /** "segment" or "tiktok" — drives the renderer style for this clip. */
  caption_mode: CaptionMode;

  // Forward-compat (Phase 4+):
  speaker?: string;
  style?: SpeakerStyle;
}
