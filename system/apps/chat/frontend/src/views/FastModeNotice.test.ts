import { describe, expect, it } from "vitest";
import { fastModeNoticeText } from "./FastModeNotice";

describe("fastModeNoticeText", () => {
  it("adds the line the harness declared after the default body, and nothing without one", () => {
    expect(fastModeNoticeText(2, "Fast mode always uses API billing, not subscription usage.")).toBe(
      "Fast mode is off now: this chat ran fast for its first 2 turns. Change this in the model picker. " +
        "Fast mode always uses API billing, not subscription usage.",
    );
    expect(fastModeNoticeText(2, null)).toBe(
      "Fast mode is off now: this chat ran fast for its first 2 turns. Change this in the model picker.",
    );
  });

  it("counts a single turn in the singular", () => {
    expect(fastModeNoticeText(1, null)).toContain("its first 1 turn.");
  });
});
