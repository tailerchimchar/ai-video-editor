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
  source_deleted_at?: string | null;
}

/** GET /api/v1/assets — full list. */
export function listAssets(): Promise<AssetSummary[]> {
  return request<AssetSummary[]>(`/api/v1/assets`);
}

/**
 * URL the browser fetches for an asset's poster thumbnail. Goes
 * through the Vite proxy → StaticFiles mount on the API. Returns null
 * if there's no asset id to point at.
 *
 * `cacheBust` should change when the user requests a regeneration so
 * the new JPG loads instead of the browser's cached copy.
 */
export function assetThumbnailUrl(assetId: string, cacheBust?: string | number): string {
  const v = cacheBust !== undefined ? `?v=${encodeURIComponent(String(cacheBust))}` : "";
  return `/workspace/asset_thumbnails/${encodeURIComponent(assetId)}.jpg${v}`;
}

export interface RegenerateThumbnailResponse {
  ok: boolean;
  path?: string;
  reason?: string;
  error?: string;
}

/** POST /api/v1/assets/:id/thumbnail — backfill / refresh the poster. */
export function regenerateAssetThumbnail(assetId: string): Promise<RegenerateThumbnailResponse> {
  return request<RegenerateThumbnailResponse>(`/api/v1/assets/${assetId}/thumbnail`, {
    method: "POST",
  });
}

/** GET /api/v1/assets/:id — one asset. */
export function getAsset(assetId: string): Promise<AssetSummary> {
  return request<AssetSummary>(`/api/v1/assets/${assetId}`);
}

export interface DeleteSourceResponse {
  asset_id: string;
  freed_bytes: number;
  already_deleted: boolean;
}

/**
 * POST /api/v1/assets/:id/delete_source — delete the .mp4 on disk.
 * Only works for source_origin='downloaded'. The asset row stays;
 * compilations made from this source keep working.
 */
export function deleteAssetSource(assetId: string): Promise<DeleteSourceResponse> {
  return request<DeleteSourceResponse>(`/api/v1/assets/${assetId}/delete_source`, {
    method: "POST",
  });
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
