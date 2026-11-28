import { describe, expect, it } from "vitest";
import { ApiError } from "@/api/client";
import { STALE_HOOK_MESSAGE, hookErrorState } from "../useHookPicks";

describe("hookErrorState", () => {
  it("a 404 on the hook query is no hook data, not an error", () => {
    const s = hookErrorState(new ApiError("no hook data", 404, null), null, false);
    expect(s).toEqual({ error: null, stale: false, broken: false });
  });

  it("any other hook query error shows, with the discard button", () => {
    const s = hookErrorState(new ApiError("cannot read j.edits.json", 422, null), null, false);
    expect(s.error).toContain("cannot read j.edits.json");
    expect(s.broken).toBe(true);
  });

  it("a 409 on a save is the stale message", () => {
    const s = hookErrorState(null, new ApiError("stale", 409, null), false);
    expect(s).toEqual({ error: STALE_HOOK_MESSAGE, stale: true, broken: false });
  });

  it("stale edits on a loaded view are the stale message", () => {
    expect(hookErrorState(null, null, true).stale).toBe(true);
  });
});
