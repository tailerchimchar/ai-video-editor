import { describe, expect, it } from "vitest";
import type { Take } from "@/types/narration";
import { isVoiceAt, regionLevelAt, scheduleFrom } from "../mixSchedule";

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

describe("mixSchedule", () => {
  it("schedules mid-take and future takes, skipping muted/past", () => {
    const takes = [
      take("late", 20, 5, { gain: 2 }),
      take("mid", 8, 10, { media_in_s: 1 }),
      take("past", 0, 5),
      take("mute", 12, 5, { muted: true }),
    ];
    const s = scheduleFrom(takes, 10);
    expect(s.map((x) => x.takeId)).toEqual(["mid", "late"]);
    expect(s[0]).toEqual({ takeId: "mid", whenS: 0, offsetS: 3, durationS: 8, gain: 1 });
    expect(s[1]).toEqual({ takeId: "late", whenS: 10, offsetS: 0, durationS: 5, gain: 2 });
  });
  it("regionLevelAt", () => {
    const r = [{ id: "r", start_s: 5, end_s: 10, level: 0.5 }];
    expect(regionLevelAt(r, 0.8, 5)).toBeCloseTo(0.4);
    expect(regionLevelAt(r, 0.8, 10)).toBe(0.8);
    expect(regionLevelAt(r, 0.8, 1)).toBe(0.8);
  });
  it("isVoiceAt", () => {
    const takes = [take("a", 0, 5), take("m", 10, 5, { muted: true })];
    expect(isVoiceAt(takes, 2)).toBe(true);
    expect(isVoiceAt(takes, 5)).toBe(false);
    expect(isVoiceAt(takes, 12)).toBe(false);
  });
});
