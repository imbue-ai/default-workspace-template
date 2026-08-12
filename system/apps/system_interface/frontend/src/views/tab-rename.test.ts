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
  it("allows any row, backgrounded ones included", () => {
    // The whole point of keying names by ref: a member with no panel -- still
    // running, just not docked -- has somewhere to keep a name, so the gesture
    // is offered on it like on any other row. Whether a row is open no longer
    // reaches this decision at all, which is what the argument shape asserts.
    expect(tabRenameBlockedReason({ isStagedForRemoval: false })).toBeNull();
  });

  it("refuses a row already staged for removal, and says why", () => {
    // That row is struck through and leaving this list on Save; naming it in
    // the same visit is withheld with an explanation and one click of Undo.
    const reason = tabRenameBlockedReason({ isStagedForRemoval: true });
    expect(reason).not.toBeNull();
    expect(reason).toContain("Undo the removal");
  });
});
