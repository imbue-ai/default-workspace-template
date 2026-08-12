import { describe, expect, it } from "vitest";

import { MAX_TAB_TITLE_LENGTH, normalizeTabTitle, tabRenameBlockedReason } from "./tab-rename";

describe("normalizeTabTitle", () => {
  it("keeps an ordinary name as it was typed", () => {
    expect(normalizeTabTitle("Design notes")).toBe("Design notes");
  });

  it("trims the padding around a name", () => {
    expect(normalizeTabTitle("  Design notes  ")).toBe("Design notes");
  });

  it("collapses the whitespace a paste can carry into one space", () => {
    expect(normalizeTabTitle("Design\n\tnotes   here")).toBe("Design notes here");
  });

  it("refuses an empty title", () => {
    expect(normalizeTabTitle("")).toBeNull();
  });

  it("refuses a title that is only whitespace", () => {
    // The editor commits on blur, so this is what clearing the field and
    // clicking away has to mean: leave the name alone, exactly as Escape does.
    expect(normalizeTabTitle("   \n\t ")).toBeNull();
  });

  it("cuts a very long title down to the cap", () => {
    const long = "a".repeat(500);
    const normalized = normalizeTabTitle(long);
    expect(normalized).toHaveLength(MAX_TAB_TITLE_LENGTH);
    expect(normalized).toBe("a".repeat(MAX_TAB_TITLE_LENGTH));
  });

  it("leaves no trailing space when the cut lands between two words", () => {
    // 119 characters, then a space, then more: the cut falls on the space.
    const normalized = normalizeTabTitle(`${"a".repeat(MAX_TAB_TITLE_LENGTH - 1)} bbbb`);
    expect(normalized).toBe("a".repeat(MAX_TAB_TITLE_LENGTH - 1));
  });

  it("keeps a title that is exactly at the cap whole", () => {
    const exact = "b".repeat(MAX_TAB_TITLE_LENGTH);
    expect(normalizeTabTitle(exact)).toBe(exact);
  });
});

describe("tabRenameBlockedReason", () => {
  it("allows a row that has a tab", () => {
    expect(tabRenameBlockedReason({ isOpen: true })).toBeNull();
  });

  it("refuses a backgrounded row, and says why", () => {
    // A member with no panel has nothing to carry a name, so the affordance is
    // withheld with an explanation rather than silently doing nothing.
    const reason = tabRenameBlockedReason({ isOpen: false });
    expect(reason).not.toBeNull();
    expect(reason).toContain("Open this to rename it");
  });
});
