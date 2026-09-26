import { describe, expect, it } from "vitest";

import { matchesQuery } from "./search";

describe("matchesQuery", () => {
  it("needs every token somewhere, in any order, case-insensitively", () => {
    expect(matchesQuery("open term", "Open new terminal")).toBe(true);
    expect(matchesQuery("TERMINAL open", "Open new terminal")).toBe(true);
    expect(matchesQuery("open browser", "Open new terminal")).toBe(false);
    expect(matchesQuery("digest kanjun", "Inbox Digest", "Triage your mail.", "kanjun")).toBe(true);
  });

  it("matches everything on a blank query", () => {
    expect(matchesQuery("   ", "anything")).toBe(true);
  });
});
