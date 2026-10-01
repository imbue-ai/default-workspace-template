import { describe, expect, it } from "vitest";
import { parseSoloMode } from "./soloMode";

describe("solo mode", () => {
  it("reads the window the page is to show alone, and whether the chrome reopened its desktop window", () => {
    expect(parseSoloMode("?solo=win-0123")).toEqual({ windowId: "win-0123", isReopened: false });
    expect(parseSoloMode("?desktop=home&solo=win-0123&reopened=1")).toEqual({
      windowId: "win-0123",
      isReopened: true,
    });
    expect(parseSoloMode("?solo=win-0123&reopened=0")).toEqual({ windowId: "win-0123", isReopened: false });
    expect(parseSoloMode("?solo=")).toBeNull();
    expect(parseSoloMode("?reopened=1")).toBeNull();
    expect(parseSoloMode("")).toBeNull();
  });
});
