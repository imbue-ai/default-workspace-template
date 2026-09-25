import { describe, expect, it } from "vitest";
import { fastModeNoticeText } from "./FastModeNotice";

describe("fastModeNoticeText", () => {
  it("adds the API-billing line for a claude chat only", () => {
    expect(fastModeNoticeText(2, "claude")).toBe(
      "Fast mode is off now: this chat ran fast for its first 2 turns. Change this in the model picker. " +
        "Fast mode always uses API billing, not subscription usage.",
    );
    expect(fastModeNoticeText(2, "codex")).toBe(
      "Fast mode is off now: this chat ran fast for its first 2 turns. Change this in the model picker.",
    );
  });

  it("counts a single turn in the singular", () => {
    expect(fastModeNoticeText(1, undefined)).toContain("its first 1 turn.");
  });
});
