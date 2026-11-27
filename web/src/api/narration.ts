/*
 * Narration endpoints — the HTTP contract in docs/specs/spec-narration.md
 * (prefix `/api/v1/narration/{kind}/{key}`), plus the URL builders for
 * the files the backend writes next to the video.
 *
 * Every function takes a `TargetRef`, so a new narratable kind (montage,
 * short, IRL video) needs no change here.
 */

import type { CameraBox, SessionView, TargetRef, Take, UploadTakeArgs } from "@/types/narration";
import { request } from "./client";

function basePath({ kind, key }: TargetRef): string {
  return `/api/v1/narration/${encodeURIComponent(kind)}/${encodeURIComponent(key)}`;
}

/** GET — the session (created on first visit) plus the video URL, stale flag and camera layout. */
export function getSession(target: TargetRef): Promise<SessionView> {
  return request<SessionView>(basePath(target));
}

/**
 * POST /takes — raw-body upload of one recorded take. The backend
 * accepts `video/webm` or `audio/webm` exactly, so codec parameters
 * (`;codecs=opus`) are stripped from the blob's MIME type.
 */
export function uploadTake(target: TargetRef, args: UploadTakeArgs): Promise<Take> {
  const query = new URLSearchParams({
    start_s: args.startS.toFixed(3),
    duration_s: args.durationS.toFixed(3),
    has_camera: String(args.hasCamera),
  });
  const fallback = args.hasCamera ? "video/webm" : "audio/webm";
  const contentType = args.blob.type.split(";")[0]?.trim() || fallback;
  return request<Take>(`${basePath(target)}/takes?${query.toString()}`, {
    method: "POST",
    body: args.blob,
    contentType,
  });
}

/** DELETE /takes/{take_id}. */
export function deleteTake(target: TargetRef, takeId: string): Promise<SessionView> {
  return request<SessionView>(`${basePath(target)}/takes/${encodeURIComponent(takeId)}`, {
    method: "DELETE",
  });
}

/** PUT /camera — save the owner's camera box (fractions; null = profile default). */
export function putCamera(target: TargetRef, camera: CameraBox): Promise<SessionView> {
  return request<SessionView>(`${basePath(target)}/camera`, { method: "PUT", body: camera });
}

/** POST /relock — keep the takes as they are against the current video. */
export function relock(target: TargetRef): Promise<SessionView> {
  return request<SessionView>(`${basePath(target)}/relock`, { method: "POST" });
}

/** POST /export — starts a `narration_export` job; poll it with `getJob`. 409 when stale and not forced. */
export function startExport(target: TargetRef, force = false): Promise<{ job_id: string }> {
  return request<{ job_id: string }>(`${basePath(target)}/export`, {
    method: "POST",
    body: { force },
  });
}

/*
 * File URLs. The API sends the narration folder (`narration_url`); older
 * sessions fall back to `<video folder>/narration/` (spec "Files"). These
 * builders are the only place that layout is known on the client.
 */

function narrationFolderUrl(view: SessionView): string {
  if (view.narration_url) return view.narration_url;
  const videoPath = view.video_url.split("?")[0] ?? view.video_url;
  const folder = videoPath.slice(0, videoPath.lastIndexOf("/"));
  return `${folder}/narration`;
}

/** URL of one take's recorded media (for playback). */
export function takeMediaUrl(view: SessionView, take: Take): string {
  return `${narrationFolderUrl(view)}/takes/${encodeURIComponent(take.media)}`;
}

/** URL of the exported `narrated.mp4`; `cacheBust` forces a re-fetch after a new export. */
export function narratedVideoUrl(view: SessionView, cacheBust?: string): string {
  const v = cacheBust ? `?v=${encodeURIComponent(cacheBust)}` : "";
  return `${narrationFolderUrl(view)}/narrated.mp4${v}`;
}
