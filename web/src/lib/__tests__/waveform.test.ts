import { describe, expect, it } from "vitest";
import { columnPeaks, envelope, mergeChannels, peaksForWidth } from "../waveform";

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
  it("envelope takes the abs max per window, normalised", () => {
    const ch = new Float32Array([0.1, -0.2, 0.4, 0, 0.1, 0.05]);
    expect(Array.from(envelope(ch, 6, 3))).toEqual([0.5, 1, 0.25]);
    expect(Array.from(envelope(new Float32Array(4), 4, 2))).toEqual([0, 0]);
  });
  it("columnPeaks maxes the samples under each column", () => {
    const env = [0, 0.2, 0.9, 0.1, 0.5, 0.3];
    // rate 2 Hz, 1 px per s: each column covers two samples
    expect(Array.from(columnPeaks(env, 2, 0, 1, 3))).toEqual(
      [0.2, 0.9, 0.5].map((v) => Math.fround(v)),
    );
    // zoomed in past the sample rate: columns repeat the sample they fall in
    expect(Array.from(columnPeaks(env, 2, 1, 4, 4))).toEqual(
      [0.9, 0.9, 0.1, 0.1].map((v) => Math.fround(v)),
    );
    // outside the envelope is silence
    expect(Array.from(columnPeaks(env, 2, -1, 1, 2))).toEqual([0, 0.2].map((v) => Math.fround(v)));
  });
});
