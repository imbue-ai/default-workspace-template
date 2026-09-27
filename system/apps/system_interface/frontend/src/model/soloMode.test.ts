import { describe, expect, it } from "vitest";
import { parseSoloWindowId, stripSoloParam } from "./soloMode";

describe("solo mode", () => {
  it("reads the window the page is to show alone, and strips only that parameter", () => {
    expect(parseSoloWindowId("?solo=win-0123")).toBe("win-0123");
    expect(parseSoloWindowId("?desktop=home&solo=win-0123")).toBe("win-0123");
    expect(parseSoloWindowId("?solo=")).toBeNull();
    expect(parseSoloWindowId("")).toBeNull();
    expect(stripSoloParam("?solo=win-0123")).toBe("");
    expect(stripSoloParam("?desktop=home&solo=win-0123")).toBe("?desktop=home");
    expect(stripSoloParam("?desktop=home")).toBe("?desktop=home");
  });
});
