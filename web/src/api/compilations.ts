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
