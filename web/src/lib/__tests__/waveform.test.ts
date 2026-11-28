import { describe, expect, it } from "vitest";
import { mergeChannels, peaksForWidth } from "../waveform";

describe("waveform", () => {
  it("ramp into 10 columns increases to 1", () => {
    const ch = new Float32Array(1000).map((_, i) => i / 999);
    const p = peaksForWidth(ch, 10);
    expect(p.length).toBe(10);
    for (let i = 1; i < 10; i++) expect(p[i]!).toBeGreaterThan(p[i - 1]!);
    expect(p[9]).toBe(1);
  });
  it("silence gives zeros", () => {
    expect(Array.from(peaksForWidth(new Float32Array(50), 5))).toEqual([0, 0, 0, 0, 0]);
  });
  it("uses abs", () => {
    const p = peaksForWidth(new Float32Array([-1, 0.5]), 2);
    expect(Array.from(p)).toEqual([1, 0.5]);
  });
  it("mergeChannels averages", () => {
    const m = mergeChannels([new Float32Array([1, 0]), new Float32Array([0, 1])]);
    expect(Array.from(m)).toEqual([0.5, 0.5]);
  });
});
