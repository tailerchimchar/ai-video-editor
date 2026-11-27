/** Mirrors `GET /api/v1/videos` (api/src/ai_video_editor/narration/catalog.py). */

export type SuggestionStatus = "none" | "pending" | "ready" | "applied";

export interface VideoSummary {
  kind: string;
  key: string;
  title: string;
  game: string;
  /** ISO date taken from the key prefix, or null. */
  date: string | null;
  duration_s: number | null;
  video_url: string;
  take_count: number;
  stale: boolean;
  exported: boolean;
  suggestion_status: SuggestionStatus;
}
