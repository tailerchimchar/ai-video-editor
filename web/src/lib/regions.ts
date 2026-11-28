/*
 * Game-audio regions — pure helpers, no DOM.
 */

import type { GameAudioRegion } from "@/types/narration";

/** Shortest region the timeline creates or keeps, seconds. */
export const MIN_REGION_S = 0.2;

/**
 * Cut regions at the video's end (`durationS`) and drop any left shorter
 * than MIN_REGION_S. A shift-relock or a shorter re-render can leave regions
 * past the end; the API only checks `end > start`. With the duration unknown
 * (0) or nothing to change, the input list is returned as is.
 */
export function clampRegions(regions: GameAudioRegion[], durationS: number): GameAudioRegion[] {
  if (durationS <= 0 || regions.every((g) => g.end_s <= durationS)) return regions;
  return regions
    .map((g) => ({ ...g, end_s: Math.min(g.end_s, durationS) }))
    .filter((g) => g.end_s - g.start_s >= MIN_REGION_S);
}
