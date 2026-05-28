/*
 * Edit mutations — every operation that changes spec.json.
 *
 * Phase 1 only ships `extendClip` and `revertCompilation` here. As
 * Phase 2+ adds UI for more edits, each new mutation gets one
 * function below and one mutation hook in `hooks/`.
 */

import { request } from "./client";
import type { EditSummary } from "@/types/compilation";

export interface ExtendClipArgs {
  /** Same `clip_ref` semantics as MCP: index ("3"), UUID prefix, or "M:SS". */
  clipRef: string;
  /** Seconds to add BEFORE the clip's current start. 0 leaves start alone. */
  before: number;
  /** Seconds to add AFTER the clip's current end. 0 leaves end alone. */
  after: number;
}

/** POST /api/v1/edit/compile/:id/extend — drag-handle commit lands here. */
export function extendClip(compilationId: string, args: ExtendClipArgs): Promise<EditSummary> {
  return request<EditSummary>(`/api/v1/edit/compile/${compilationId}/extend`, {
    method: "POST",
    body: {
      clip_ref: args.clipRef,
      before: args.before,
      after: args.after,
    },
  });
}

/**
 * POST /api/v1/edit/compile/:id/reorder_explicit — drag-and-drop commit.
 * Pass the full new order as a list of clip ids. Validation rejects
 * missing / unknown / duplicate ids server-side, so a stale UI can't
 * silently lose clips.
 */
export function reorderClipsExplicit(
  compilationId: string,
  clipIds: string[],
): Promise<EditSummary> {
  return request<EditSummary>(`/api/v1/edit/compile/${compilationId}/reorder_explicit`, {
    method: "POST",
    body: { clip_ids: clipIds },
  });
}

export interface RevertArgs {
  /** Pick exactly one: jump to a version, or walk back N steps. */
  to_version?: number;
  steps?: number;
}

/** POST /api/v1/edit/compile/:id/revert — restore a previous spec snapshot. */
export function revertCompilation(
  compilationId: string,
  args: RevertArgs,
): Promise<EditSummary & { reverted_to_version?: number }> {
  return request<EditSummary & { reverted_to_version?: number }>(
    `/api/v1/edit/compile/${compilationId}/revert`,
    { method: "POST", body: args },
  );
}

export interface EditedCaptionWord {
  word: string;
  start: number;
  end: number;
}

export interface EditedCaptionSegment {
  start_seconds: number;
  end_seconds: number;
  text: string;
  /** Only send when the text is unchanged from the original. */
  words?: EditedCaptionWord[];
}

export interface SetClipCaptionsArgs {
  clipRef: string;
  segments: EditedCaptionSegment[];
}

/**
 * POST /api/v1/edit/compile/:id/clip_captions — replace a clip's
 * captions with edited segments. The body is the full updated list
 * (not a patch); for unchanged segments preserve the `words` array,
 * for edited segments drop `words` so the renderer even-splits.
 */
export function setClipCaptions(
  compilationId: string,
  args: SetClipCaptionsArgs,
): Promise<EditSummary> {
  return request<EditSummary>(`/api/v1/edit/compile/${compilationId}/clip_captions`, {
    method: "POST",
    body: { clip_ref: args.clipRef, segments: args.segments },
  });
}

export type CaptionStylePreset = "default" | "tiktok";

export interface AddCaptionArgs {
  clipRef: string;
  startSeconds: number;
  endSeconds: number;
  text: string;
  preset?: CaptionStylePreset;
}

/**
 * POST /api/v1/edit/compile/:id/clip_captions/add — insert one caption
 * segment at a specific timestamp. Sorted into the existing list by
 * start time.
 */
export function addClipCaption(compilationId: string, args: AddCaptionArgs): Promise<EditSummary> {
  const body: Record<string, unknown> = {
    clip_ref: args.clipRef,
    start_seconds: args.startSeconds,
    end_seconds: args.endSeconds,
    text: args.text,
  };
  if (args.preset) body.style = { preset: args.preset };
  return request<EditSummary>(`/api/v1/edit/compile/${compilationId}/clip_captions/add`, {
    method: "POST",
    body,
  });
}

export interface RemoveCaptionArgs {
  clipRef: string;
  segmentIndex: number;
}

/**
 * POST /api/v1/edit/compile/:id/clip_captions/remove — delete one caption
 * segment by its 0-based index. Idempotent for out-of-range indexes.
 */
export function removeClipCaption(
  compilationId: string,
  args: RemoveCaptionArgs,
): Promise<EditSummary> {
  return request<EditSummary>(`/api/v1/edit/compile/${compilationId}/clip_captions/remove`, {
    method: "POST",
    body: { clip_ref: args.clipRef, segment_index: args.segmentIndex },
  });
}

/**
 * POST /api/v1/edit/compile/:id/clip_captions/tiktokify — switch a
 * clip's captions to TikTok style (explode + restyle).
 *
 * The transformation is destructive. To go back, use the journal's
 * revert button. There is no clean "un-tiktokify" because the original
 * segment groupings aren't preserved.
 */
export function tiktokifyClipCaptions(
  compilationId: string,
  clipRef: string,
): Promise<EditSummary> {
  return request<EditSummary>(`/api/v1/edit/compile/${compilationId}/clip_captions/tiktokify`, {
    method: "POST",
    body: { clip_ref: clipRef },
  });
}

export interface AddZoomEffectArgs {
  clipRef: string;
  /** Zoom magnification. 1.5 is a comfortable default; 2.0 is aggressive. */
  factor: number;
  /**
   * ROI preset name from the backend (`center`, `minimap_lol`, etc.).
   * League regions are mirrored from `profiles/league.toml`.
   */
  roi: string;
}

/**
 * POST /api/v1/edit/compile/:id/effect — add a zoom effect to a clip.
 * Applies to the whole clip duration. Stored as
 * `effects: [{kind: "zoom", factor, roi}]` on the clip's spec entry.
 */
export function addZoomEffect(
  compilationId: string,
  args: AddZoomEffectArgs,
): Promise<EditSummary> {
  return request<EditSummary>(`/api/v1/edit/compile/${compilationId}/effect`, {
    method: "POST",
    body: { clip_ref: args.clipRef, kind: "zoom", factor: args.factor, roi: args.roi },
  });
}

export interface AddFocusEffectArgs {
  clipRef: string;
  /** Spotlight center, fractional 0..1 of the frame. */
  x: number;
  y: number;
  /** Spotlight radius, fractional 0..1 of frame height. */
  radius: number;
  /** Darkness applied OUTSIDE the spotlight: 0 = no dim, 1 = black. */
  dim: number;
}

/**
 * POST /api/v1/edit/compile/:id/effect — add a focus spotlight to a clip.
 * Applies to the whole clip duration. Stored as
 * `effects: [{kind: "focus", x, y, radius, dim}]`.
 */
export function addFocusEffect(
  compilationId: string,
  args: AddFocusEffectArgs,
): Promise<EditSummary> {
  return request<EditSummary>(`/api/v1/edit/compile/${compilationId}/effect`, {
    method: "POST",
    body: {
      clip_ref: args.clipRef,
      kind: "focus",
      x: args.x,
      y: args.y,
      radius: args.radius,
      dim: args.dim,
    },
  });
}
