/*
 * Narration types — hand-mirrored from
 * `api/src/ai_video_editor/narration/models.py` (field names identical)
 * plus the router's `SessionView` (docs/specs/spec-narration.md).
 *
 * Nothing here is game- or kind-specific: a new game or a new
 * narratable thing is a backend change only.
 */

/** What is being narrated: a registered target `kind` and its `key`. */
export interface TargetRef {
  kind: string;
  key: string;
}

/** One recorded take, pinned to the video second it started at. */
export interface Take {
  id: string;
  start_s: number;
  duration_s: number;
  /** File name inside the store's takes folder, e.g. "<id>.webm". */
  media: string;
  has_camera: boolean;
  /** ISO-8601 UTC. */
  created_at: string;
}

/**
 * The owner's camera box. Position fields are fractions of the video
 * frame (top-left corner); `null` means "use the game profile default".
 */
export interface CameraBox {
  /** Shown in the export; recording the camera is separate. */
  enabled: boolean;
  width_frac: number | null;
  x: number | null;
  y: number | null;
  rounded: boolean;
}

/** How the original audio ducks under the owner's voice. */
export interface AudioMix {
  narration_gain: number;
  duck_threshold: number;
  duck_ratio: number;
  duck_attack_ms: number;
  duck_release_ms: number;
}

/** Everything stored for one narrated video. */
export interface NarrationSession {
  target: TargetRef;
  game: string;
  video_fingerprint: string;
  takes: Take[];
  camera: CameraBox;
  mix: AudioMix;
}

/** A resolved box in output pixels. */
export interface PixelBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

export type ExportStatus = "ok" | "stale" | "no_takes";

/** The frame `SessionView.camera_px` is laid out against. */
export const CAMERA_LAYOUT_FRAME = { w: 1920, h: 1080 } as const;

/** GET /api/v1/narration/{kind}/{key} and most mutations return this. */
export interface SessionView {
  session: NarrationSession;
  /** Browser-loadable URL of the video being narrated (`/workspace/...`). */
  video_url: string;
  game: string;
  /** The video changed since the takes were recorded. */
  stale: boolean;
  /** Current camera box (owner overrides applied) for a 1920x1080 frame. */
  camera_px: PixelBox;
  /** Game-profile default box for a 1920x1080 frame. */
  default_camera_px?: PixelBox;
}

export interface UploadTakeArgs {
  blob: Blob;
  startS: number;
  durationS: number;
  hasCamera: boolean;
}
