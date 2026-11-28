import { describe, expect, it } from "vitest";
import type { Take } from "@/types/narration";
import {
  effectiveEnd,
  effectiveStart,
  nudge,
  punchIn,
  splitAt,
  trimHead,
  trimTail,
} from "../takeEdits";

const take = (id: string, start: number, dur: number, extra: Partial<Take> = {}): Take => ({
  id,
  start_s: start,
  duration_s: dur,
  media: "m.webm",
  has_camera: false,
  created_at: "2026-01-01T00:00:00Z",
  media_in_s: 0,
  offset_s: 0,
  gain: 1,
  muted: false,
  ...extra,
});

let n = 0;
const newId = () => `new${++n}`;

describe("takeEdits", () => {
  it("effective bounds include offset", () => {
    const t = take("a", 10, 5, { offset_s: 1 });
    expect(effectiveStart(t)).toBe(11);
    expect(effectiveEnd(t)).toBe(16);
  });
  it("trimHead moves start and media_in", () => {
    const t = trimHead(take("a", 10, 10, { media_in_s: 2 }), 13);
    expect(t.start_s).toBe(13);
    expect(t.media_in_s).toBe(5);
    expect(t.duration_s).toBe(7);
  });
  it("trimTail clamps to media length", () => {
    const t = take("a", 0, 10, { media_in_s: 2 });
    expect(trimTail(t, 5).duration_s).toBe(5);
    expect(trimTail(t, 50, 12).duration_s).toBe(10);
  });
  it("splitAt", () => {
    const [a, b] = splitAt(take("a", 0, 10, { media_in_s: 1 }), 4, () => "b");
    expect(a.duration_s).toBe(4);
    expect(b.id).toBe("b");
    expect(b.start_s).toBe(4);
    expect(b.media_in_s).toBe(5);
    expect(b.duration_s).toBe(6);
  });
  it("nudge clamps", () => {
    expect(nudge(take("a", 0, 1), 9).offset_s).toBe(3);
    expect(nudge(take("a", 0, 1), -9).offset_s).toBe(-3);
  });
  it("punchIn trims, removes and head-trims", () => {
    const old = [
      take("A", 0, 25),
      take("B", 30, 10),
      take("C", 43, 30),
      take("D", 50, 5, { media: "d.webm" }),
    ];
    const N = take("N", 18, 37);
    const out = punchIn(old, N, newId);
    const by = Object.fromEntries(out.map((t) => [t.id, t]));
    expect(by.A!.duration_s).toBe(18);
    expect(by.B).toBeUndefined();
    expect(by.D).toBeUndefined();
    expect(by.C!.start_s).toBe(55);
    expect(by.C!.media_in_s).toBe(12);
    expect(by.C!.duration_s).toBe(18);
    expect(by.N).toBeDefined();
  });
  it("punchIn splits a spanning take", () => {
    const out = punchIn([take("A", 0, 60)], take("N", 20, 10), () => "tail");
    const by = Object.fromEntries(out.map((t) => [t.id, t]));
    expect(by.A!.duration_s).toBe(20);
    expect(by.tail!.start_s).toBe(30);
    expect(by.tail!.media_in_s).toBe(30);
    expect(by.tail!.duration_s).toBe(30);
    expect(by.tail!.media).toBe("m.webm");
    expect(by.N).toBeDefined();
  });
  it("punchIn exact boundary touches nothing", () => {
    const A = take("A", 0, 10);
    const out = punchIn([A], take("N", 10, 5), newId);
    expect(out.find((t) => t.id === "A")).toEqual(A);
    expect(out).toHaveLength(2);
  });
});
