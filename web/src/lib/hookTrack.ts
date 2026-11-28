/*
 * The hook track — pure maths, no React, no DOM. Mirrors
 * `api/src/ai_video_editor/story/hook_edits.py` (`settle`, `clamp_offsets`,
 * `split_pick`) and the owner-approved playground (`applyTrim`, `dropAt`,
 * `addToHook`, `closeGaps`).
 *
 * The hook is a track: each clip is one chapter placed at `at_s` (track
 * seconds, swipes not counted) with offsets `start_s` / `end_s` (seconds
 * earlier / later than the chapter's base window, recording seconds). Clips
 * never overlap after `settle`; deleting and trimming leave empty space,
 * which the laptop closes when it re-joins.
 *
 * A chapter's base window comes from any `HookPickView` of that chapter in
 * the view (a split chapter has several), or from `extra` picks the caller
 * remembers (a deleted pick, so its undo can put it back).
 */

import type { HookPickView, HookView, Storyboard } from "@/types/narration";

export const SNAP_S = 0.4;
export const HOOK_MIN_S = 2.0;
export const FREEZE_S = 1.4;
export const SWIPE_S = 0.5;
const EPS = 1e-6;
/** Shorter empty spaces are rounding, not space. */
const GAP_MIN_S = 0.05;
/** The map keeps `at_s` to 0.01, the edits to 0.1. */
const AT_TOL_S = 0.06;

/** One clip on the hook track (the shape of the API's `EditPick`). */
export interface TrackClip {
  id: string;
  chapter_id: string;
  at_s: number;
  start_s: number;
  end_s: number;
}

/** Rendered length of a clip, seconds. */
export type Lens = (c: TrackClip) => number;

/** Round to 0.1 (never −0). */
export const r1 = (x: number) => Math.round(x * 10) / 10 + 0;
/** Up to the 0.1 grid, so a pushed clip never overlaps. */
const ceil1 = (x: number) => Math.ceil(x * 10 - 1e-9) / 10 + 0;

/** A new pick id: 8 hex chars, like the API's `uuid4().hex[:8]`. */
export function newPickId(): string {
  return crypto.randomUUID().replace(/-/g, "").slice(0, 8);
}

export function toClips(view: HookView): TrackClip[] {
  return view.picks.map(({ id, chapter_id, at_s, start_s, end_s }) => ({
    id,
    chapter_id,
    at_s,
    start_s,
    end_s,
  }));
}

/** The base window of `chapterId`, or undefined when neither list knows it. */
export function findBase(
  view: HookView,
  chapterId: string,
  extra: HookPickView[] = [],
): HookPickView | undefined {
  return (
    view.picks.find((p) => p.chapter_id === chapterId) ??
    extra.find((p) => p.chapter_id === chapterId)
  );
}

export function baseOf(view: HookView, chapterId: string, extra: HookPickView[] = []) {
  const b = findBase(view, chapterId, extra);
  if (!b) throw new Error(`unknown chapter_id: ${chapterId}`);
  return b;
}

/** Rendered length of the base window; `tease` clips end on a freeze. */
export function baseLen(p: HookPickView, mode: string): number {
  return p.base_rec_end_s - p.base_rec_start_s + (mode === "tease" ? FREEZE_S : 0);
}

export function lensOf(view: HookView, extra: HookPickView[] = []): Lens {
  return (c) => baseLen(baseOf(view, c.chapter_id, extra), view.mode) + c.start_s + c.end_s;
}

/**
 * Sorted by `at_s`; a clip that overlaps the previous one is pushed to its
 * end; `at_s >= 0`, on the 0.1 grid. Stable: on a tie the earlier clip in
 * `clips` stays first.
 */
export function settle(clips: TrackClip[], lens: Lens): TrackClip[] {
  const out: TrackClip[] = [];
  let end = 0;
  for (const c of [...clips].sort((a, b) => a.at_s - b.at_s)) {
    let at = r1(Math.max(c.at_s, 0));
    if (out.length && at < end - EPS) at = ceil1(end);
    out.push({ ...c, at_s: at });
    end = at + lens(c);
  }
  return out;
}

/**
 * Offsets whose window stays between the neighbouring chapters and is at
 * least HOOK_MIN_S long; `keep` is the edge that stays put when too short.
 */
export function clampOffsets(
  p: HookPickView,
  start_s: number,
  end_s: number,
  keep: "start" | "end",
): { start_s: number; end_s: number } {
  const hi = p.limit_hi_rec_s ?? Infinity;
  let s = Math.min(start_s, p.base_rec_start_s - p.limit_lo_rec_s);
  let e = Math.min(end_s, hi - p.base_rec_end_s);
  const window = p.base_rec_end_s - p.base_rec_start_s;
  if (window + s + e < HOOK_MIN_S) {
    if (keep === "start") e = HOOK_MIN_S - window - s;
    else s = HOOK_MIN_S - window - e;
  }
  // round the edge that moved away from the other one, so rounding never shortens
  return keep === "start"
    ? { start_s: r1(s), end_s: ceil1(e) }
    : { start_s: ceil1(s), end_s: r1(e) };
}

/** The clip that ends at or before `clip` starts (the last such), if any. */
function previousOf(clips: TrackClip[], clip: TrackClip, lens: Lens): TrackClip | null {
  let prev: TrackClip | null = null;
  for (const x of settle(
    clips.filter((c) => c.id !== clip.id),
    lens,
  )) {
    if (x.at_s + lens(x) <= clip.at_s + EPS) prev = x;
  }
  return prev;
}

/**
 * Trim or extend one edge. End: grows / shrinks in place; the end eats the
 * empty space, then pushes the next clips. Start: moves left into empty
 * space, stops at the previous clip, and the rest of the length goes right.
 */
export function applyTrim(
  clips: TrackClip[],
  clip: TrackClip,
  edge: "start" | "end",
  start_s: number,
  end_s: number,
  view: HookView,
  extra: HookPickView[] = [],
): TrackClip[] {
  const lens = lensOf(view, extra);
  const cur = clips.find((c) => c.id === clip.id) ?? clip;
  const cl = clampOffsets(
    baseOf(view, cur.chapter_id, extra),
    start_s,
    end_s,
    edge === "end" ? "start" : "end",
  );
  let at = cur.at_s;
  if (edge === "start") {
    const d = cl.start_s - cur.start_s; // + = starts earlier
    const prev = previousOf(clips, cur, lens);
    const floor = prev ? prev.at_s + lens(prev) : 0;
    at = Math.max(floor, r1(cur.at_s - d));
  }
  return settle(
    clips.map((c) => (c.id === cur.id ? { ...c, ...cl, at_s: at } : c)),
    lens,
  );
}

/**
 * Where a moved clip lands: into empty space as is (snapping to neighbour
 * edges within SNAP_S); onto another clip = before or after it by half.
 */
export function dropAt(clips: TrackClip[], clip: TrackClip, wantAt: number, lens: Lens): number {
  const len = lens(clip);
  let at = Math.max(0, wantAt);
  const others = settle(
    clips.filter((x) => x.id !== clip.id),
    lens,
  );
  for (const o of others) {
    const oe = o.at_s + lens(o);
    if (Math.abs(at - oe) < SNAP_S) at = oe;
    if (Math.abs(at + len - o.at_s) < SNAP_S) at = o.at_s - len;
  }
  const under = others.find((o) => at > o.at_s - EPS && at < o.at_s + lens(o) - EPS);
  if (under) at = at < under.at_s + lens(under) / 2 ? under.at_s : under.at_s + lens(under);
  return r1(Math.max(0, at));
}

/** Put `clip` at `at` (first on a tie) and push what it overlaps. */
export function moveTo(clips: TrackClip[], clip: TrackClip, at: number, lens: Lens): TrackClip[] {
  const cur = clips.find((c) => c.id === clip.id) ?? clip;
  return settle([{ ...cur, at_s: at }, ...clips.filter((c) => c.id !== clip.id)], lens);
}

/** Take a clip out; its place stays as empty space. */
export function removeClip(clips: TrackClip[], id: string): TrackClip[] {
  return clips.filter((c) => c.id !== id);
}

/** Add `pick` in the first empty space that fits it, else at the end of the track. */
export function addToHook(clips: TrackClip[], pick: TrackClip, lens: Lens): TrackClip[] {
  const sorted = settle(clips, lens);
  const len = lens(pick);
  let end = 0;
  let at: number | null = null;
  for (const c of sorted) {
    if (at === null && c.at_s - end >= len - EPS) at = end;
    end = Math.max(end, c.at_s + lens(c));
  }
  return settle([...sorted, { ...pick, at_s: r1(at ?? end) }], lens);
}

/**
 * "add to the hook" for a chapter the view may not know yet: `len` is its
 * base window's rendered length (the storyboard's `hook_len_s`); the new
 * clip gets a fresh id and the base window (offsets 0).
 */
export function addChapter(
  clips: TrackClip[],
  chapterId: string,
  len: number,
  view: HookView,
  extra: HookPickView[] = [],
): TrackClip[] {
  const pick: TrackClip = { id: newPickId(), chapter_id: chapterId, at_s: 0, start_s: 0, end_s: 0 };
  const known = lensOf(view, extra);
  const lens: Lens = (c) =>
    c.chapter_id === chapterId && !findBase(view, chapterId, extra)
      ? len + c.start_s + c.end_s
      : known(c);
  return addToHook(clips, pick, lens);
}

/** Lay the clips back to back in `order` (default: track order). */
function backToBack(order: TrackClip[], lens: Lens): TrackClip[] {
  let t = 0;
  const out = order.map((c) => {
    const placed = { ...c, at_s: r1(t) };
    t = placed.at_s + lens(c);
    return placed;
  });
  return settle(out, lens);
}

/** Close every empty space (what the laptop does on the re-join). */
export function closeGaps(clips: TrackClip[], lens: Lens): TrackClip[] {
  return backToBack(settle(clips, lens), lens);
}

/**
 * Cut `clip` at track second `tTrack` into two clips of the same chapter:
 * the left keeps the id, the right gets a new one. Throws when a piece
 * would be shorter than HOOK_MIN_S or the point is outside the clip.
 */
export function splitAt(
  clips: TrackClip[],
  clip: TrackClip,
  tTrack: number,
  view: HookView,
  extra: HookPickView[] = [],
): TrackClip[] {
  const i = clips.findIndex((c) => c.id === clip.id);
  const c = clips[i];
  if (!c) throw new Error(`unknown pick id: ${clip.id}`);
  const b = baseOf(view, c.chapter_id, extra);
  const lens = lensOf(view, extra);
  if (!(c.at_s <= tTrack && tTrack < c.at_s + lens(c))) {
    throw new Error("split point is outside the pick");
  }
  const recT = r1(b.base_rec_start_s - c.start_s + (tTrack - c.at_s));
  const leftWin = recT - (b.base_rec_start_s - c.start_s);
  const rightWin = b.base_rec_end_s + c.end_s - recT;
  if (leftWin < HOOK_MIN_S - EPS || rightWin < HOOK_MIN_S - EPS) {
    throw new Error("too close to the edge to split");
  }
  const left = { ...c, end_s: r1(recT - b.base_rec_end_s) };
  const right: TrackClip = {
    id: newPickId(),
    chapter_id: c.chapter_id,
    at_s: r1(c.at_s + lens(left)),
    start_s: r1(b.base_rec_start_s - recT),
    end_s: c.end_s,
  };
  return [...clips.slice(0, i), left, right, ...clips.slice(i + 1)];
}

/** One clip as drawn: track span, its number, and its part of a split chapter. */
export interface HookSpan {
  clip: TrackClip;
  vs: number;
  ve: number;
  len: number;
  /** 0-based place on the track. */
  n: number;
  /** 1, 2, … in track order when the chapter is split; null otherwise. */
  part: number | null;
}

export interface TrackLayout {
  hooks: HookSpan[];
  gaps: { vs: number; ve: number }[];
  trackEnd: number;
  /** The intro length after the re-join (the swipes overlap, the empty spaces close). */
  introAfter: number;
  /**
   * How far the picture after the hook slides: `(trackEnd − n·SWIPE_S) −
   * rendered_intro_s` (n = clips on the track; the swipes overlap). Exactly 0
   * for an unchanged hook; the owner's empty space stays in it (the laptop
   * closes it, `introAfter` is the length it will have).
   */
  shift: number;
  /** Seconds of empty space on the track. */
  emptyS: number;
}

export function layout(
  clips: TrackClip[],
  view: HookView,
  extra: HookPickView[] = [],
): TrackLayout {
  const lens = lensOf(view, extra);
  const sorted = settle(clips, lens);
  const count = new Map<string, number>();
  for (const c of sorted) count.set(c.chapter_id, (count.get(c.chapter_id) ?? 0) + 1);
  const seen = new Map<string, number>();
  const hooks: HookSpan[] = [];
  const gaps: { vs: number; ve: number }[] = [];
  let end = 0;
  let total = 0;
  sorted.forEach((c, n) => {
    const len = lens(c);
    if (c.at_s > end + GAP_MIN_S) gaps.push({ vs: end, ve: c.at_s });
    let part: number | null = null;
    if ((count.get(c.chapter_id) ?? 0) > 1) {
      part = (seen.get(c.chapter_id) ?? 0) + 1;
      seen.set(c.chapter_id, part);
    }
    hooks.push({ clip: c, vs: c.at_s, ve: c.at_s + len, len, n, part });
    end = c.at_s + len;
    total += len;
  });
  return {
    hooks,
    gaps,
    trackEnd: end,
    introAfter: hooks.length ? total - hooks.length * SWIPE_S : 0,
    shift: end - hooks.length * SWIPE_S - view.rendered_intro_s,
    emptyS: gaps.reduce((s, g) => s + g.ve - g.vs, 0),
  };
}

/**
 * Where the pick's rendered piece is: its recording start (the map's base
 * window with the offsets the last apply folded in) and its span in the mp4;
 * null for a piece the mp4 does not have.
 */
export function renderedWindow(p: HookPickView | undefined) {
  if (!p || p.video_start_s === null || p.video_end_s === null) return null;
  if (p.rendered_rec_start_s === null) return null;
  return {
    recStart: p.rendered_rec_start_s,
    videoStart: p.video_start_s,
    videoEnd: p.video_end_s,
  };
}

/** Recording second at track second `t` of span `h`. */
function recAt(h: HookSpan, t: number, view: HookView, extra: HookPickView[]): number {
  const b = baseOf(view, h.clip.chapter_id, extra);
  return b.base_rec_start_s - h.clip.start_s + (t - h.vs);
}

/**
 * The mp4 second to seek to for track second `t`: inside a clip's rendered
 * window → that piece; seconds the mp4 does not have in the hook → the
 * chapter's own footage (needs the storyboard), else null; empty space →
 * null; after the track → the mp4 slid by the shift.
 */
export function seekTarget(
  t: number,
  lay: TrackLayout,
  view: HookView,
  storyboard?: Storyboard,
  extra: HookPickView[] = [],
): number | null {
  if (t >= lay.trackEnd) return Math.max(0, t - lay.shift);
  const h = lay.hooks.find((x) => t >= x.vs && t < x.ve);
  if (!h) return null;
  const rec = recAt(h, t, view, extra);
  const r = renderedWindow(view.picks.find((p) => p.id === h.clip.id));
  if (r && rec >= r.recStart - EPS && rec < r.recStart + (r.videoEnd - r.videoStart)) {
    return r.videoStart + (rec - r.recStart);
  }
  const ch = storyboard?.games.flatMap((g) => g.chapters).find((c) => c.id === h.clip.chapter_id);
  if (!ch) return null;
  return ch.video_start_s + (rec - baseOf(view, h.clip.chapter_id, extra).chapter_rec_start_s);
}

/** The track second showing mp4 second `v` (the inverse of `seekTarget`), or null. */
export function trackTimeOf(v: number, lay: TrackLayout, view: HookView): number | null {
  if (v >= view.rendered_intro_s) return v + lay.shift;
  for (const h of lay.hooks) {
    const r = renderedWindow(view.picks.find((p) => p.id === h.clip.id));
    if (!r || v < r.videoStart || v >= r.videoEnd) continue;
    const rec = r.recStart + (v - r.videoStart);
    const t = h.vs + (rec - recAt(h, h.vs, view, []));
    if (t >= h.vs && t < h.ve) return t;
  }
  return null;
}

const signed = (x: number) => `${x > 0 ? "+" : x < 0 ? "−" : ""}${Math.abs(x).toFixed(1)}`;

/** "start −1.0 s, end +2.1 s" (start: − = earlier, like the playground). */
export function offsetsText(start_s: number, end_s: number, sep = ", "): string {
  return [start_s ? `start ${signed(-start_s)} s` : "", end_s ? `end ${signed(end_s)} s` : ""]
    .filter(Boolean)
    .join(sep);
}

/** One line of the changes badge and how to undo just that change. */
export interface HookChange {
  text: string;
  undo: (clips: TrackClip[]) => TrackClip[];
}

/**
 * What differs from the rendered hook, one line per change: trims, splits,
 * deleted (from `rendered`, the picks the caller has seen rendered), added,
 * re-ordered, empty spaces.
 */
export function changes(
  view: HookView,
  rendered: HookPickView[] = [],
  name: (p: HookPickView) => string = (p) => p.label,
): HookChange[] {
  const lens = lensOf(view);
  const lay = layout(toClips(view), view);
  const byId = new Map(view.picks.map((p) => [p.id, p]));
  const out: HookChange[] = [];

  for (const h of lay.hooks) {
    const p = byId.get(h.clip.id);
    if (!p || !p.pending || h.part !== null || (!p.start_s && !p.end_s)) continue;
    out.push({
      text: `${h.n + 1} ${name(p)}: ${offsetsText(p.start_s, p.end_s)}`,
      undo: (clips) => applyTrim(clips, h.clip, "end", 0, 0, view),
    });
  }

  for (const r of rendered) {
    if (r.video_start_s === null || byId.has(r.id)) continue;
    const pick: TrackClip = {
      id: r.id,
      chapter_id: r.chapter_id,
      at_s: 0,
      start_s: r.start_s,
      end_s: r.end_s,
    };
    out.push({
      text: `deleted ${name(r)}`,
      undo: (clips) => addToHook(clips, pick, lensOf(view, [r])),
    });
  }

  for (const p of view.picks) {
    if (p.video_start_s !== null) continue;
    const siblings = view.picks.filter((x) => x.chapter_id === p.chapter_id && x.id !== p.id);
    if (siblings.length) {
      out.push({
        text: `split ${name(p)}`,
        undo: (clips) => {
          const me = clips.find((c) => c.id === p.id);
          if (!me) return clips;
          const b = baseOf(view, p.chapter_id);
          const myStart = b.base_rec_start_s - me.start_s;
          // the piece that ends where this one starts takes its end back
          const left = clips.find(
            (c) =>
              c.chapter_id === p.chapter_id &&
              c.id !== p.id &&
              Math.abs(b.base_rec_end_s + c.end_s - myStart) < 0.05,
          );
          const rest = removeClip(clips, p.id).map((c) =>
            left && c.id === left.id ? { ...c, end_s: me.end_s } : c,
          );
          return settle(rest, lens);
        },
      });
    } else {
      out.push({
        text: `added ${name(p)}`,
        undo: (clips) => settle(removeClip(clips, p.id), lens),
      });
    }
  }

  const kept = lay.hooks
    .map((h) => byId.get(h.clip.id))
    .filter((p): p is HookPickView => !!p && p.video_start_s !== null);
  const renderedOrder = [...kept].sort((a, b) => a.video_start_s! - b.video_start_s!);
  if (kept.some((p, i) => p.id !== renderedOrder[i]?.id)) {
    out.push({
      text: `re-ordered: ${lay.hooks.map((h) => name(byId.get(h.clip.id)!)).join(" → ")}`,
      undo: (clips) => {
        const rank = new Map(renderedOrder.map((p, i) => [p.id, i]));
        const order = settle(clips, lens).sort(
          (a, b) => (rank.get(a.id) ?? Infinity) - (rank.get(b.id) ?? Infinity),
        );
        return backToBack(order, lens);
      },
    });
  }

  const k = lay.gaps.length;
  if (k) {
    out.push({
      text: `${k} empty space${k > 1 ? "s" : ""} (closed on the laptop)`,
      undo: (clips) => closeGaps(clips, lens),
    });
  }
  return out;
}

/**
 * The view the server will answer for `clips`, computed locally so an edit
 * shows at once. Null when a clip names a chapter no pick here has a base
 * window for (then the page waits for the server).
 */
export function optimisticView(
  view: HookView,
  clips: TrackClip[],
  extra: HookPickView[] = [],
): HookView | null {
  if (clips.some((c) => !findBase(view, c.chapter_id, extra))) return null;
  const lens = lensOf(view, extra);
  const sorted = settle(clips, lens);
  const old = new Map(view.picks.map((p) => [p.id, p]));
  const remembered = new Map(extra.map((p) => [p.id, p]));
  const picks: HookPickView[] = sorted.map((c) => {
    const b = baseOf(view, c.chapter_id, extra);
    const prev = old.get(c.id) ?? remembered.get(c.id);
    const same =
      !!prev &&
      Math.abs(prev.at_s - c.at_s) <= AT_TOL_S &&
      prev.start_s === c.start_s &&
      prev.end_s === c.end_s;
    const rec_end_s = b.base_rec_end_s + c.end_s;
    return {
      ...b,
      ...c,
      len_s: lens(c),
      rec_start_s: b.base_rec_start_s - c.start_s,
      rec_end_s,
      video_start_s: prev?.video_start_s ?? null,
      video_end_s: prev?.video_end_s ?? null,
      rendered_rec_start_s: prev?.rendered_rec_start_s ?? null,
      rendered_rec_end_s: prev?.rendered_rec_end_s ?? null,
      pending: same ? prev.pending : true,
      beyond_chapter_s: Math.max(0, rec_end_s - b.chapter_rec_end_s),
    };
  });
  const lay = layout(sorted, view, extra);
  const changed = picks.length !== view.picks.length || picks.some((p) => p.pending);
  return {
    ...view,
    picks,
    track_end_s: lay.trackEnd,
    intro_after_s: lay.introAfter,
    empty_space_s: lay.emptyS,
    dirty: changed || view.dirty,
  };
}

/**
 * A short name for a clip: the game's first word (the champion for League,
 * from the storyboard) and the fight size from the label ("2v3"), else the
 * label.
 */
export function shortName(p: HookPickView, storyboard?: Storyboard): string {
  const who = storyboard?.games.find((g) => g.round === p.round)?.label.split(" ")[0];
  const size = /^\d+v\d+/.exec(p.label)?.[0];
  if (who && size) return `${who} ${size}`;
  return who ? `${who} · ${p.label}` : p.label;
}
