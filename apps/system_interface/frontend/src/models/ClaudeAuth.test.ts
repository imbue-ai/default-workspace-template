import { describe, expect, it } from "vitest";

import { shouldOpenLoginModalForHarness } from "./ClaudeAuth";

describe("shouldOpenLoginModalForHarness", () => {
  it("opens for claude", () => {
    expect(shouldOpenLoginModalForHarness("claude")).toBe(true);
  });

  it("opens for a missing/unknown harness, matching the backend's parse_harness fallback", () => {
    expect(shouldOpenLoginModalForHarness(null)).toBe(true);
    expect(shouldOpenLoginModalForHarness(undefined)).toBe(true);
  });

  it("does not open for a known non-claude harness -- the modal only knows claude auth login", () => {
    expect(shouldOpenLoginModalForHarness("codex")).toBe(false);
    expect(shouldOpenLoginModalForHarness("antigravity")).toBe(false);
    expect(shouldOpenLoginModalForHarness("opencode")).toBe(false);
  });
});
