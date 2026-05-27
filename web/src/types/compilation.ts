/*
 * Compilation + history types — mirror `api/src/ai_video_editor/`
 * router responses for compile.py / compile_journal.py.
 */

import type { Clip } from "./clip";

/** Row from GET /edit/compile (list endpoint). */
export interface CompilationSummary {
  id: string;
  asset_id: string;
  output_path: string | null;
  created_at: string;
}

/** Body returned by GET /edit/compile/:id/clips. */
export interface CompilationClipsPayload {
  compilation_id: string;
  clips: Clip[];
}

/** Body returned by GET /edit/compile/:id (params + index). */
export interface CompilationDetail {
  id: string;
  asset_id: string;
  output_path: string | null;
  params: Record<string, unknown>;
  created_at: string;
  index?: {
    output?: string;
    aspect?: string;
    kept_total?: number;
    parts_rendered?: number;
    [k: string]: unknown;
  } | null;
}

/** Journal entry from GET /edit/compile/:id/history. */
export interface HistoryEntry {
  version: number;
  ts: string;
  action: string;
  details: Record<string, unknown> | null;
  /** Human-readable phrasing — backend's preferred display string. */
  display?: string;
  clip_count: number;
}

export interface CompilationHistoryPayload {
  compilation_id: string;
  history: HistoryEntry[];
}

/**
 * Edit summary returned by mutation endpoints (extend, intro, etc).
 * The full shape from `render_spec` — we expose only what the UI uses.
 */
export interface EditSummary {
  output: string | null;
  compiled: boolean;
  kept_total: number;
  parts_rendered: number;
  clips: Clip[];
  error: string | null;
}
