import { describe, expect, it } from "vitest";
import { createHistory } from "../history";

describe("createHistory", () => {
  it("undoes and redoes in order", () => {
    const h = createHistory<number>();
    expect(h.canUndo).toBe(false);
    h.push(1);
    h.push(2);
    h.push(3);
    expect(h.canUndo).toBe(true);
    expect(h.undo()).toBe(2);
    expect(h.undo()).toBe(1);
    expect(h.undo()).toBeNull();
    expect(h.canRedo).toBe(true);
    expect(h.redo()).toBe(2);
    expect(h.redo()).toBe(3);
    expect(h.redo()).toBeNull();
  });

  it("a new push clears the redo", () => {
    const h = createHistory<string>();
    h.push("a");
    h.push("b");
    h.undo();
    h.push("c");
    expect(h.canRedo).toBe(false);
    expect(h.redo()).toBeNull();
    expect(h.undo()).toBe("a");
  });

  it("keeps at most `limit` states", () => {
    const h = createHistory<number>(3);
    for (const n of [1, 2, 3, 4, 5]) h.push(n);
    expect(h.undo()).toBe(4);
    expect(h.undo()).toBe(3);
    expect(h.undo()).toBeNull();
  });
});
