import { describe, expect, it } from "vitest";
import { isWorkingActivityState } from "./activityState";

describe("isWorkingActivityState — stop-button visibility gate", () => {
  it("treats THINKING / TOOL_RUNNING as an interruptible turn", () => {
    expect(isWorkingActivityState("THINKING")).toBe(true);
    expect(isWorkingActivityState("TOOL_RUNNING")).toBe(true);
  });

  it("treats COMPACTING as working", () => {
    expect(isWorkingActivityState("COMPACTING")).toBe(true);
  });

  it("treats IDLE as not working (nothing to interrupt)", () => {
    expect(isWorkingActivityState("IDLE")).toBe(false);
  });

  it("treats null / undefined (no activity tracking) as not working", () => {
    expect(isWorkingActivityState(null)).toBe(false);
    expect(isWorkingActivityState(undefined)).toBe(false);
  });

  it("treats an unknown / future state value as not working", () => {
    expect(isWorkingActivityState("SOMETHING_NEW")).toBe(false);
  });
});
