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
  /** Where in the recorded media this take's audio starts. */
  media_in_s: number;
  /** Alignment nudge, -3..3 s. */
  offset_s: number;
  /** Take gain, (0, 4]. */
  gain: number;
  muted: boolean;
}

/** A partial edit of a take (all fields optional). */
export interface TakeEdit {
  start_s?: number;
  duration_s?: number;
  media_in_s?: number;
  offset_s?: number;
  gain?: number;
  muted?: boolean;
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
  game_gain: number;
  narration_gain: number;
  duck_threshold: number;
  duck_ratio: number;
  duck_attack_ms: number;
  duck_release_ms: number;
}

/** A span where the game audio is scaled by `level`. */
export interface GameAudioRegion {
  id: string;
  start_s: number;
  end_s: number;
  level: number;
}

/** A span where the camera box is overridden. */
export interface CameraRegion {
  id: string;
  start_s: number;
  end_s: number;
  box: CameraBox;
}

/** A labelled point on the timeline. */
export interface Marker {
  at_s: number;
  label: string;
  kind: "chapter" | "game" | "cue" | "insert";
}

/** A sprite sheet of video frames. */
export interface FrameStrip {
  sprite_url: string;
  every_s: number;
  tile_w: number;
  tile_h: number;
  cols: number;
  count: number;
}

/** Waveform peaks for a take or the game audio. */
export interface Peaks {
  rate_hz: number;
  peaks: number[];
}

/** Everything stored for one narrated video. */
export interface NarrationSession {
  target: TargetRef;
  game: string;
  video_fingerprint: string;
  takes: Take[];
  camera: CameraBox;
  mix: AudioMix;
  game_regions: GameAudioRegion[];
  camera_regions: CameraRegion[];
  pinned_intro_s: number | null;
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
  /**
   * The narration folder (`takes/`, `narrated.mp4`) under `/workspace`. Not
   * always next to the video: joined videos keep theirs in `story/_joined/<key>/`.
   */
  narration_url?: string | null;
  /** URL of an existing `narrated.mp4`, or null before the first export. */
  export_url: string | null;
  suggested_shift_s: number | null;
  markers: Marker[];
}

export interface UploadTakeArgs {
  blob: Blob;
  startS: number;
  durationS: number;
  hasCamera: boolean;
}
