/*
 * Narration endpoints — the HTTP contract in docs/specs/spec-narration.md
 * (prefix `/api/v1/narration/{kind}/{key}`), plus the URL builders for
 * the files the backend writes next to the video.
 *
 * Every function takes a `TargetRef`, so a new narratable kind (montage,
 * short, IRL video) needs no change here.
 */

import type {
  AudioMix,
  CameraBox,
  CameraRegion,
  FrameStrip,
  GameAudioRegion,
  HookPicksBody,
  HookView,
  Marker,
  Peaks,
  SessionView,
  Storyboard,
  TakeEdit,
  TargetRef,
  Take,
  UploadTakeArgs,
} from "@/types/narration";
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
export function uploadTake(target: TargetRef, args: UploadTakeArgs, replace = true): Promise<Take> {
  const query = new URLSearchParams({
    start_s: args.startS.toFixed(3),
    duration_s: args.durationS.toFixed(3),
    has_camera: String(args.hasCamera),
    replace: String(replace),
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

/** POST /relock — re-pin the takes to the current video, shifted by `shiftS` (0 = as they are). */
export function relock(target: TargetRef, shiftS = 0): Promise<SessionView> {
  return request<SessionView>(`${basePath(target)}/relock`, {
    method: "POST",
    body: { shift_s: shiftS },
  });
}

/** PATCH /takes/{take_id} — partial edit (trim, offset, gain, mute). */
export function patchTake(target: TargetRef, takeId: string, edit: TakeEdit): Promise<SessionView> {
  return request<SessionView>(`${basePath(target)}/takes/${encodeURIComponent(takeId)}`, {
    method: "PATCH",
    body: edit,
  });
}

/** POST /takes/{take_id}/split?at_s= — split a take at a video second. */
export function splitTake(target: TargetRef, takeId: string, atS: number): Promise<SessionView> {
  const query = new URLSearchParams({ at_s: atS.toFixed(3) });
  return request<SessionView>(
    `${basePath(target)}/takes/${encodeURIComponent(takeId)}/split?${query.toString()}`,
    { method: "POST" },
  );
}

/** PUT /mix — the ducking / gain settings. */
export function putMix(target: TargetRef, mix: AudioMix): Promise<SessionView> {
  return request<SessionView>(`${basePath(target)}/mix`, { method: "PUT", body: mix });
}

/** PUT /game-regions — replace the game-audio regions. */
export function putGameRegions(
  target: TargetRef,
  regions: GameAudioRegion[],
): Promise<SessionView> {
  return request<SessionView>(`${basePath(target)}/game-regions`, {
    method: "PUT",
    body: regions,
  });
}

/** PUT /camera-regions — replace the camera-override regions. */
export function putCameraRegions(target: TargetRef, regions: CameraRegion[]): Promise<SessionView> {
  return request<SessionView>(`${basePath(target)}/camera-regions`, {
    method: "PUT",
    body: regions,
  });
}

/** GET /frames — the video's sprite-sheet frame strip. */
export function getFrames(target: TargetRef): Promise<FrameStrip> {
  return request<FrameStrip>(`${basePath(target)}/frames`);
}

/** GET /peaks — waveform peaks of the game audio. */
export function getPeaks(target: TargetRef): Promise<Peaks> {
  return request<Peaks>(`${basePath(target)}/peaks`);
}

/** GET /markers — chapter / game / cue markers (also in `SessionView.markers`). */
export function getMarkers(target: TargetRef): Promise<Marker[]> {
  return request<Marker[]>(`${basePath(target)}/markers`);
}

/** POST /export — starts a `narration_export` job; poll it with `getJob`. 409 when stale and not forced. */
export function startExport(target: TargetRef, force = false): Promise<{ job_id: string }> {
  return request<{ job_id: string }>(`${basePath(target)}/export`, {
    method: "POST",
    body: { force },
  });
}

/** GET /hook — the hook track of a joined video; 404 when the map has no hook block. */
export function getHook(target: TargetRef): Promise<HookView> {
  return request<HookView>(`${basePath(target)}/hook`);
}

/** PUT /hook — save the hook picks (the laptop applies them). 422 = invalid list, 409 = re-joined since. */
export function putHook(target: TargetRef, body: HookPicksBody): Promise<HookView> {
  return request<HookView>(`${basePath(target)}/hook`, { method: "PUT", body });
}

/** DELETE /hook — discard the saved hook changes. */
export function resetHook(target: TargetRef): Promise<HookView> {
  return request<HookView>(`${basePath(target)}/hook`, { method: "DELETE" });
}

/** GET /storyboard — games and chapters of a joined video, with what the hook uses. */
export function getStoryboard(target: TargetRef): Promise<Storyboard> {
  return request<Storyboard>(`${basePath(target)}/storyboard`);
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
