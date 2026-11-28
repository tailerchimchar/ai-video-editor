import { describe, expect, it } from "vitest";
import type { HookPickView, HookView, Storyboard } from "@/types/narration";
import {
  addToHook,
  applyTrim,
  changes,
  clampOffsets,
  closeGaps,
  dropAt,
  layout,
  lensOf,
  moveTo,
  optimisticView,
  seekTarget,
  settle,
  splitAt,
  toClips,
  trackTimeOf,
  type TrackClip,
} from "../hookTrack";

/** A pick whose base window is [rs, re] (recording s), placed at `at`. */
const pick = (
  id: string,
  chapter: string,
  at: number,
  rs: number,
  re: number,
  extra: Partial<HookPickView> = {},
): HookPickView => {
  const start_s = extra.start_s ?? 0;
  const end_s = extra.end_s ?? 0;
  return {
    id,
    chapter_id: chapter,
    round: "r1",
    chapter_index: 0,
    label: "2v2 skirmish in bot_lane",
    at_s: at,
    start_s,
    end_s,
    len_s: re - rs + start_s + end_s,
    rec_start_s: rs - start_s,
    rec_end_s: re + end_s,
    base_rec_start_s: rs,
    base_rec_end_s: re,
    offset_s: 10,
    events: [],
    chapter_rec_start_s: rs - 20,
    chapter_rec_end_s: re + 5,
    limit_lo_rec_s: 0,
    limit_hi_rec_s: null,
    video_start_s: at,
    video_end_s: at + (re - rs),
    pending: false,
    beyond_chapter_s: 0,
    ...extra,
  };
};

const hook = (picks: HookPickView[], extra: Partial<HookView> = {}): HookView => ({
  mode: "play",
  title: "t",
  rendered_intro_s: 20,
  track_end_s: 0,
  intro_after_s: 0,
  picks,
  base_digest: "sha256:000000000000",
  edits_digest: null,
  stale_edits: false,
  dirty: false,
  empty_space_s: 0,
  laptop_command: "story-join --apply-edits j",
  edits_file: "j.edits.json",
  ...extra,
});

/** a: 5 s at 0, b: 4 s at 8 (3 s of empty space before it), c: 4 s at 12. */
const view3 = () =>
  hook([
    pick("a", "r1:a", 0, 100, 105),
    pick("b", "r1:b", 8, 200, 204),
    pick("c", "r1:c", 12, 300, 304),
  ]);
const byId = (clips: TrackClip[], id: string) => clips.find((c) => c.id === id)!;

describe("settle", () => {
  it("pushes a clip that overlaps the previous one to its end", () => {
    const lens = () => 5;
    const out = settle(
      [
        { id: "b", chapter_id: "x", at_s: 3, start_s: 0, end_s: 0 },
        { id: "a", chapter_id: "x", at_s: 0, start_s: 0, end_s: 0 },
      ],
      lens,
    );
    expect(out.map((c) => [c.id, c.at_s])).toEqual([
      ["a", 0],
      ["b", 5],
    ]);
  });

  it("clamps at 0, rounds to 0.1 and rounds a push up so nothing overlaps", () => {
    const lens = (c: TrackClip) => (c.id === "a" ? 5.04 : 2);
    const out = settle(
      [
        { id: "a", chapter_id: "x", at_s: -1, start_s: 0, end_s: 0 },
        { id: "b", chapter_id: "x", at_s: 1.23, start_s: 0, end_s: 0 },
      ],
      lens,
    );
    expect(out.map((c) => c.at_s)).toEqual([0, 5.1]);
  });
});

describe("clampOffsets", () => {
  it("stops at the neighbour limits and keeps the window at least 2 s", () => {
    const p = pick("a", "r1:a", 0, 100, 110, { limit_lo_rec_s: 97, limit_hi_rec_s: 112 });
    expect(clampOffsets(p, 10, 10, "start")).toEqual({ start_s: 3, end_s: 2 });
    expect(clampOffsets(p, 0, -9.5, "start")).toEqual({ start_s: 0, end_s: -8 });
    expect(clampOffsets(p, -9.5, 0, "end")).toEqual({ start_s: -8, end_s: 0 });
  });
});

describe("applyTrim", () => {
  it("extending the end eats the empty space, then pushes the next clip", () => {
    const v = view3();
    const clips = toClips(v);
    const small = applyTrim(clips, byId(clips, "a"), "end", 0, 2, v);
    expect(byId(small, "a").end_s).toBe(2);
    expect(byId(small, "b").at_s).toBe(8);
    const big = applyTrim(clips, byId(clips, "a"), "end", 0, 5, v);
    expect(byId(big, "b").at_s).toBe(10);
    expect(byId(big, "c").at_s).toBe(14);
  });

  it("extending the start moves left into empty space, stops at the previous clip, grows right", () => {
    const v = view3();
    const clips = toClips(v);
    const out = applyTrim(clips, byId(clips, "b"), "start", 2, 0, v);
    expect(byId(out, "b")).toMatchObject({ at_s: 6, start_s: 2 });
    expect(byId(out, "c").at_s).toBe(12);
    const far = applyTrim(clips, byId(clips, "b"), "start", 5, 0, v);
    expect(byId(far, "b")).toMatchObject({ at_s: 5, start_s: 5 }); // stopped at the end of a
    expect(byId(far, "c").at_s).toBe(14); // b is 9 s long now: 5 → 14
  });

  it("trimming leaves empty space", () => {
    const v = view3();
    const clips = toClips(v);
    const end = applyTrim(clips, byId(clips, "b"), "end", 0, -1, v);
    expect(byId(end, "b")).toMatchObject({ at_s: 8, end_s: -1 });
    expect(byId(end, "c").at_s).toBe(12);
    const start = applyTrim(clips, byId(clips, "b"), "start", -1, 0, v);
    expect(byId(start, "b")).toMatchObject({ at_s: 9, start_s: -1 });
    expect(byId(start, "c").at_s).toBe(12);
  });
});

describe("dropAt / moveTo", () => {
  const v = view3();
  const clips = toClips(v);
  const lens = lensOf(v);
  const c = byId(clips, "c");

  it("snaps to a neighbour edge within 0.4 s", () => {
    // its start snaps to the end of a
    expect(
      dropAt(
        clips.filter((x) => x.id !== "b"),
        c,
        5.3,
        lens,
      ),
    ).toBe(5);
    // its end snaps to the start of b
    expect(
      dropAt(
        clips.filter((x) => x.id === "b"),
        c,
        3.8,
        lens,
      ),
    ).toBe(4);
  });

  it("onto another clip lands before or after it by half", () => {
    expect(dropAt(clips, c, 1, lens)).toBe(0);
    expect(dropAt(clips, c, 3, lens)).toBe(5);
  });

  it("moveTo puts the moved clip first on a tie and pushes the rest", () => {
    const out = moveTo(clips, c, 0, lens);
    expect(out.map((x) => [x.id, x.at_s])).toEqual([
      ["c", 0],
      ["a", 4],
      ["b", 9],
    ]);
  });
});

describe("addToHook / closeGaps", () => {
  const v = view3();
  const clips = toClips(v);
  const lens = (x: TrackClip) => (x.id === "n" ? 3 : x.id === "w" ? 6 : lensOf(v)(x));

  it("fills the first empty space that fits, else goes to the end", () => {
    const n = { id: "n", chapter_id: "r1:n", at_s: 0, start_s: 0, end_s: 0 };
    expect(byId(addToHook(clips, n, lens), "n").at_s).toBe(5);
    const w = { ...n, id: "w" };
    expect(byId(addToHook(clips, w, lens), "w").at_s).toBe(16);
  });

  it("closes the empty spaces", () => {
    expect(closeGaps(clips, lensOf(v)).map((x) => x.at_s)).toEqual([0, 5, 9]);
  });
});

describe("splitAt", () => {
  const v = hook([pick("a", "r1:a", 0, 116, 130)]);
  const clips = toClips(v);

  it("makes two pieces of one chapter: the left keeps the id", () => {
    const out = splitAt(clips, clips[0]!, 6, v);
    expect(out).toHaveLength(2);
    expect(out[0]).toMatchObject({ id: "a", chapter_id: "r1:a", at_s: 0, start_s: 0, end_s: -8 });
    expect(out[1]).toMatchObject({ chapter_id: "r1:a", at_s: 6, start_s: -6, end_s: 0 });
    expect(out[1]!.id).toMatch(/^[0-9a-f]{8}$/);
  });

  it("refuses near an edge and outside the clip", () => {
    expect(() => splitAt(clips, clips[0]!, 1, v)).toThrow(/too close/);
    expect(() => splitAt(clips, clips[0]!, 13, v)).toThrow(/too close/);
    expect(() => splitAt(clips, clips[0]!, 20, v)).toThrow(/outside/);
  });
});

describe("layout", () => {
  it("places clips, finds empty spaces, the intro after the swipes and the shift", () => {
    const v = view3();
    const lay = layout(toClips(v), v);
    expect(lay.hooks.map((h) => [h.vs, h.ve, h.n])).toEqual([
      [0, 5, 0],
      [8, 12, 1],
      [12, 16, 2],
    ]);
    expect(lay.gaps).toEqual([{ vs: 5, ve: 8 }]);
    expect(lay.trackEnd).toBe(16);
    expect(lay.introAfter).toBeCloseTo(13 - 1.5);
    expect(lay.shift).toBe(16 - 20);
  });

  it("labels the pieces of a split chapter part 1, part 2 in track order", () => {
    const v = hook([pick("a", "r1:a", 0, 116, 130)]);
    const clips = splitAt(toClips(v), toClips(v)[0]!, 6, v);
    expect(layout(clips, v).hooks.map((h) => h.part)).toEqual([1, 2]);
    expect(layout(toClips(v), v).hooks[0]!.part).toBeNull();
  });
});

describe("seekTarget / trackTimeOf", () => {
  const v = hook([
    pick("a", "r1:a", 0, 100, 105, { video_start_s: 0, video_end_s: 5 }),
    pick("b", "r1:b", 5, 200, 204, {
      video_start_s: 4.5,
      video_end_s: 8.5,
      start_s: 2,
      pending: true,
    }),
  ]);
  const lay = layout(toClips(v), v);
  const sb = {
    games: [
      {
        round: "r1",
        label: "Xayah 2/0/0",
        offset_s: 10,
        chapters: [{ id: "r1:b", video_start_s: 60 }],
      },
    ],
  } as unknown as Storyboard;

  it("seconds inside a rendered window play that piece", () => {
    expect(seekTarget(2, lay, v)).toBe(2);
    // b starts 2 s earlier: its first 2 track seconds are not in the mp4
    expect(seekTarget(8, lay, v)).toBeCloseTo(4.5 + 1);
  });

  it("gained seconds play the chapter's own footage when the storyboard is loaded", () => {
    expect(seekTarget(6, lay, v)).toBeNull();
    // rec 199, chapter rec start 180, chapter video start 60
    expect(seekTarget(6, lay, v, sb)).toBeCloseTo(60 + 19);
  });

  it("after the track, the mp4 slid by the shift", () => {
    expect(lay.trackEnd).toBe(11);
    expect(seekTarget(30, lay, v)).toBe(30 - lay.shift);
    expect(trackTimeOf(30 - lay.shift, lay, v)).toBeCloseTo(30);
    expect(trackTimeOf(2, lay, v)).toBeCloseTo(2);
  });
});

describe("changes", () => {
  it("lists trims, empty spaces and deleted picks; undo returns the fixed list", () => {
    const v = hook([
      pick("a", "r1:a", 0, 100, 105, { end_s: 2, pending: true }),
      pick("c", "r1:c", 12, 300, 304),
    ]);
    const deleted = pick("b", "r1:b", 8, 200, 204);
    const list = changes(v, [deleted]);
    const texts = list.map((c) => c.text);
    expect(texts[0]).toMatch(/^1 .*end \+2\.0 s$/);
    expect(texts).toContain("deleted 2v2 skirmish in bot_lane");
    expect(texts).toContain("1 empty space (closed on the laptop)");
    const clips = toClips(v);
    const undone = list[0]!.undo(clips);
    expect(byId(undone, "a").end_s).toBe(0);
    const back = list.find((c) => c.text.startsWith("deleted"))!.undo(clips);
    expect(byId(back, "b").at_s).toBe(7);
  });
});

describe("optimisticView", () => {
  it("recomputes lengths, windows and pending for the new picks", () => {
    const v = view3();
    const clips = applyTrim(toClips(v), byId(toClips(v), "a"), "end", 0, 2, v);
    const next = optimisticView(v, clips)!;
    const a = next.picks.find((p) => p.id === "a")!;
    expect(a).toMatchObject({ end_s: 2, len_s: 7, rec_end_s: 107, pending: true });
    expect(next.picks.find((p) => p.id === "c")!.pending).toBe(false);
    expect(next.track_end_s).toBe(16);
    expect(next.empty_space_s).toBeCloseTo(1);
  });

  it("is null when a pick names a chapter it has no base for", () => {
    const v = view3();
    const z = { id: "z", chapter_id: "r1:z", at_s: 0, start_s: 0, end_s: 0 };
    expect(optimisticView(v, [z])).toBeNull();
  });
});
