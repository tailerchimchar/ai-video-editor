/*
 * Take edits — pure functions mirroring the backend's trim/split/punch-in
 * semantics (narration/models.py). All times are video seconds; a take's
 * effective span is shifted by its `offset_s`.
 */

import type { Take, TakeEdit } from "@/types/narration";

export const MIN_TAKE_S = 0.3;
const MAX_OFFSET_S = 3;

export const effectiveStart = (t: Take): number => t.start_s + t.offset_s;
export const effectiveEnd = (t: Take): number => t.start_s + t.offset_s + t.duration_s;

/** Apply a partial edit: every defined field overwrites the take's. */
export function applyTakeEdit(t: Take, edit: TakeEdit): Take {
  const defined = Object.fromEntries(Object.entries(edit).filter(([, v]) => v !== undefined));
  return { ...t, ...defined };
}

/**
 * Move the head so the take starts (effectively) at `newStartS`: later cuts
 * audio, earlier brings back trimmed audio (never before the media start or 0:00).
 */
export function trimHead(t: Take, newStartS: number): Take {
  const earliest = Math.max(-t.media_in_s, -t.start_s, -effectiveStart(t));
  const cut = Math.max(earliest, newStartS - effectiveStart(t));
  return {
    ...t,
    start_s: t.start_s + cut,
    media_in_s: t.media_in_s + cut,
    duration_s: t.duration_s - cut,
  };
}

/**
 * Move the tail so the take ends (effectively) at `newEndS`. With the media
 * length known it may grow back up to the media's end; without it, only cut.
 */
export function trimTail(t: Take, newEndS: number, mediaDurationS?: number): Take {
  const limit = mediaDurationS !== undefined ? mediaDurationS - t.media_in_s : t.duration_s;
  const duration = Math.min(newEndS - effectiveStart(t), limit);
  return { ...t, duration_s: Math.max(0, duration) };
}

/** Split at effective time `atS` into head and tail (tail gets `newId`). */
export function splitAt(t: Take, atS: number, newId: () => string): [Take, Take] {
  const cut = atS - effectiveStart(t);
  const head: Take = { ...t, duration_s: cut };
  const tail: Take = {
    ...t,
    id: newId(),
    start_s: t.start_s + cut,
    media_in_s: t.media_in_s + cut,
    duration_s: t.duration_s - cut,
  };
  return [head, tail];
}

/** Set the alignment offset, clamped to ±3 s. */
export function nudge(t: Take, offsetS: number): Take {
  return { ...t, offset_s: Math.min(MAX_OFFSET_S, Math.max(-MAX_OFFSET_S, offsetS)) };
}

/**
 * Add `newTake`, making room: older takes overlapping its span are trimmed,
 * split around it, or removed. Fragments under MIN_TAKE_S are dropped.
 */
export function punchIn(takes: Take[], newTake: Take, newId: () => string): Take[] {
  const ns = effectiveStart(newTake);
  const ne = effectiveEnd(newTake);
  const out: Take[] = [];
  for (const t of takes) {
    const es = effectiveStart(t);
    const ee = effectiveEnd(t);
    if (ee <= ns || es >= ne) {
      out.push(t);
      continue;
    }
    if (es < ns) out.push(trimTail(t, ns));
    if (ee > ne) {
      const tail = trimHead(t, ne);
      out.push(es < ns ? { ...tail, id: newId() } : tail);
    }
  }
  return [...out.filter((t) => t.duration_s >= MIN_TAKE_S), newTake];
}
