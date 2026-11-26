/**
 * Editor suggestions: hand-mirrored from
 * `api/src/ai_video_editor/story/suggest/models.py` (names identical).
 * `start_s`/`end_s` are game-clock seconds (what the HUD shows); `video_s`
 * is the matching second in the cut video.
 */

export type SuggestionKind = "letterbox_focus" | "slowmo" | "zoom";
export type SuggestionSetStatus = "pending" | "ready" | "applied" | "failed";

export interface SuggestionItem {
  id: string;
  kind: SuggestionKind;
  chapter_index: number;
  start_s: number;
  end_s: number;
  params: Record<string, number | string>;
  reason: string;
  play_score: number | null;
  pop_p: number | null;
  backend: "jev" | "rules";
  /** null = not decided yet. */
  accepted: boolean | null;
  /** Second in the rendered story.mp4 where it starts ("jump there"), for
   * the render the suggestions were made from. */
  video_s: number | null;
}

/** `WORKSPACE/story/<round>/suggestions.json`. */
export interface SuggestionSet {
  round: string;
  created_at: string;
  status: SuggestionSetStatus;
  error: string | null;
  items: SuggestionItem[];
}

/** POST .../suggestions and .../suggestions/apply. */
export interface JobStarted {
  job_id: string;
}
