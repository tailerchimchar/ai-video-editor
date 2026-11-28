import { describe, expect, it } from "vitest";
import { clampScroll, fit, pxToS, sToPx, tickEvery, zoomAround } from "../timeline";

describe("timeline", () => {
  it("fit fills the width", () => {
    expect(fit(800, 792.9).pxPerS).toBeCloseTo(1.009, 3);
  });
  it("sToPx/pxToS roundtrip", () => {
    const v = { pxPerS: 20, scrollS: 5, widthPx: 800 };
    expect(sToPx(v, 7)).toBe(40);
    expect(pxToS(v, 40)).toBe(7);
  });
  it("zoomAround keeps the anchor second fixed", () => {
    const v = { pxPerS: 10, scrollS: 20, widthPx: 800 };
    const z = zoomAround(v, 2, 300, 600);
    expect(z.pxPerS).toBe(20);
    expect(pxToS(z, 300)).toBeCloseTo(pxToS(v, 300), 9);
  });
  it("zoomAround clamps pxPerS", () => {
    const v = { pxPerS: 10, scrollS: 0, widthPx: 800 };
    expect(zoomAround(v, 1000, 0, 600).pxPerS).toBe(400);
    expect(zoomAround(v, 0.0001, 0, 600).pxPerS).toBeCloseTo(800 / 600);
  });
  it("clampScroll bounds scroll", () => {
    const v = { pxPerS: 10, scrollS: 999, widthPx: 800 };
    expect(clampScroll(v, 100).scrollS).toBe(20);
    expect(clampScroll({ ...v, scrollS: -5 }, 100).scrollS).toBe(0);
  });
  it("tickEvery", () => {
    expect(tickEvery(1)).toBe(120);
    expect(tickEvery(10)).toBe(10);
    expect(tickEvery(0.01)).toBe(300);
  });
});
