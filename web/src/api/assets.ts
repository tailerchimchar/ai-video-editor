/*
 * Asset endpoints — list + ingest from URL.
 */

import { request } from "./client";

export interface AssetSummary {
  id: string;
  filename: string;
  path: string;
  game: string | null;
  created_at: string;
  indexed_at: string;
  source_origin?: string | null; // 'imported' | 'downloaded'
}

/** GET /api/v1/assets — full list. */
export function listAssets(): Promise<AssetSummary[]> {
  return request<AssetSummary[]>(`/api/v1/assets`);
}

export interface IngestUrlArgs {
  url: string;
  game: string;
}

/**
 * POST /api/v1/assets/ingest_url — start a yt-dlp download.
 * Returns a job_id; poll /api/v1/jobs/{id} for status. On completion
 * the job's `output_path` contains the new asset id.
 */
export function ingestUrl(args: IngestUrlArgs): Promise<{ job_id: string }> {
  return request<{ job_id: string }>(`/api/v1/assets/ingest_url`, {
    method: "POST",
    body: args,
  });
}

export interface JobStatus {
  id: string;
  type: string;
  status: "pending" | "running" | "completed" | "failed";
  output_path: string | null;
  error: string | null;
  created_at: string;
  completed_at: string | null;
  summary: string;
}

/** GET /api/v1/jobs/{id} — used to poll long-running ingest. */
export function getJob(jobId: string): Promise<JobStatus> {
  return request<JobStatus>(`/api/v1/jobs/${jobId}`);
}
