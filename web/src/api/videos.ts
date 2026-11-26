/*
 * Videos hub endpoint: every narratable video across all registered kinds
 * (`GET /api/v1/videos`). Query key for consumers: `VIDEOS_QUERY_KEY`.
 */

import type { VideoSummary } from "@/types/videos";
import { request } from "./client";

export const VIDEOS_QUERY_KEY = ["videos"] as const;

export function listVideos(): Promise<VideoSummary[]> {
  return request<VideoSummary[]>("/api/v1/videos");
}
