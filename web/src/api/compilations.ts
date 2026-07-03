/*
 * Compilation endpoints — thin typed wrappers over `api/v1/edit/compile`.
 *
 * Each function maps 1:1 to a backend route. Adding a new endpoint =
 * add a function; no other client plumbing changes.
 */

import { request } from "./client";
import type {
  CompilationClipsPayload,
  CompilationDetail,
  CompilationHistoryPayload,
  CompilationSummary,
} from "@/types/compilation";

/** GET /api/v1/edit/compile?limit=N — newest-first list. */
export function listCompilations(limit = 20): Promise<CompilationSummary[]> {
  return request<CompilationSummary[]>(`/api/v1/edit/compile?limit=${limit}`);
}

/** GET /api/v1/edit/compile/:id — full metadata + index.json. */
export function getCompilation(id: string): Promise<CompilationDetail> {
  return request<CompilationDetail>(`/api/v1/edit/compile/${id}`);
}

/** GET /api/v1/edit/compile/:id/clips — clip table with reel + source times. */
export function getCompilationClips(id: string): Promise<CompilationClipsPayload> {
  return request<CompilationClipsPayload>(`/api/v1/edit/compile/${id}/clips`);
}

/** GET /api/v1/edit/compile/:id/history — edit journal. */
export function getCompilationHistory(id: string): Promise<CompilationHistoryPayload> {
  return request<CompilationHistoryPayload>(`/api/v1/edit/compile/${id}/history`);
}

// ---------------------------------------------------------------------
// VLM taste-layer — health + review-only whole-comp review.
// ---------------------------------------------------------------------

export interface VLMHealth {
  ok: boolean;
  backend: string;
  enabled: boolean;
  model: string | null;
  latency_ms: number | null;
  reason: string | null;
}

/** POST /api/v1/vlm/health — probe the VLM backend. Never throws for
 * "just unreachable" — returns `ok: false` with a `reason`. */
export function getVLMHealth(): Promise<VLMHealth> {
  return request<VLMHealth>(`/api/v1/vlm/health`, { method: "POST" });
}

export interface VLMFix {
  clip_ref: string;
  issue: string;
  fix:
    | "extend_before"
    | "extend_after"
    | "trim_start"
    | "trim_end"
    | "remove_clip"
    | "apply_zoom"
    | "apply_focus";
  fix_seconds: number | null;
  roi: string | null;
  focus_x: number | null;
  focus_y: number | null;
}

export interface VLMReviewResponse {
  ok: boolean;
  passes: number;
  is_cohesive: boolean;
  fixes: VLMFix[];
  backend: string;
  model: string | null;
}

/** POST /api/v1/edit/compile/:id/vlm_review — run the whole-comp review
 * loop in review-only mode. Returns suggested fixes; the caller decides
 * which (if any) to apply via the existing editing mutation surface. */
export function vlmReviewCompilation(id: string): Promise<VLMReviewResponse> {
  return request<VLMReviewResponse>(`/api/v1/edit/compile/${id}/vlm_review`, {
    method: "POST",
    body: {},
  });
}
