import { describe, expect, it } from "vitest";
import type { GameAudioRegion } from "@/types/narration";
import { clampRegions } from "../regions";

const r = (id: string, start_s: number, end_s: number): GameAudioRegion => ({
  id,
  start_s,
  end_s,
  level: 0.5,
});

describe("clampRegions", () => {
  it("cuts regions at the video's end and drops what is left too short", () => {
    const out = clampRegions(
      [r("a", 10, 20), r("b", 95, 110), r("c", 99.9, 104), r("d", 120, 130)],
      100,
    );
    expect(out).toEqual([r("a", 10, 20), r("b", 95, 100)]);
  });
  it("leaves regions alone while the duration is unknown", () => {
    const regions = [r("a", 10, 200)];
    expect(clampRegions(regions, 0)).toBe(regions);
  });
  it("returns the same list when nothing changes", () => {
    const regions = [r("a", 10, 20)];
    expect(clampRegions(regions, 100)).toBe(regions);
  });
});
