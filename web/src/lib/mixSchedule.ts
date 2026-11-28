/*
 * Playback mix scheduling — pure functions that turn takes into a plan
 * the Web Audio layer executes. No DOM, no AudioContext.
 */

import type { GameAudioRegion, Take } from "@/types/narration";
import { effectiveEnd, effectiveStart } from "./takeEdits";

export interface Scheduled {
  takeId: string;
  /** Seconds from now until playback starts (0 if already running). */
  whenS: number;
  /** Where in the take's media to start. */
  offsetS: number;
  durationS: number;
  gain: number;
}

/** Plan every unmuted take that has not finished by `nowS`, soonest first. */
export function scheduleFrom(takes: Take[], nowS: number): Scheduled[] {
  const out: Scheduled[] = [];
  for (const t of takes) {
    if (t.muted) continue;
    const start = effectiveStart(t);
    const end = effectiveEnd(t);
    if (end <= nowS) continue;
    if (start <= nowS) {
      out.push({
        takeId: t.id,
        whenS: 0,
        offsetS: t.media_in_s + (nowS - start),
        durationS: end - nowS,
        gain: t.gain,
      });
    } else {
      out.push({
        takeId: t.id,
        whenS: start - nowS,
        offsetS: t.media_in_s,
        durationS: t.duration_s,
        gain: t.gain,
      });
    }
  }
  return out.sort((a, b) => a.whenS - b.whenS);
}

/** Game-audio gain at `tS`: the region's level scales `gameGain`. */
export function regionLevelAt(regions: GameAudioRegion[], gameGain: number, tS: number): number {
  const r = regions.find((g) => g.start_s <= tS && tS < g.end_s);
  return r ? gameGain * r.level : gameGain;
}

/** Is an unmuted take speaking at `tS`? */
export function isVoiceAt(takes: Take[], tS: number): boolean {
  return takes.some((t) => !t.muted && effectiveStart(t) <= tS && tS < effectiveEnd(t));
}
