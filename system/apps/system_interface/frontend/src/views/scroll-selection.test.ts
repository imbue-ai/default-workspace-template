import { describe, expect, it } from "vitest";
import { isSelectionActiveWithin } from "./scroll-selection";

describe("isSelectionActiveWithin", () => {
  it("is inactive with no range", () => {
    expect(
      isSelectionActiveWithin({ hasRange: false, isCollapsed: true, anchorWithin: false, focusWithin: false }),
    ).toBe(false);
  });

  it("is inactive when collapsed (a bare caret)", () => {
    expect(isSelectionActiveWithin({ hasRange: true, isCollapsed: true, anchorWithin: true, focusWithin: true })).toBe(
      false,
    );
  });

  it("is inactive when neither endpoint is inside this view", () => {
    expect(
      isSelectionActiveWithin({ hasRange: true, isCollapsed: false, anchorWithin: false, focusWithin: false }),
    ).toBe(false);
  });

  it("is active when the anchor is inside this view (drag started here, dragged out)", () => {
    expect(
      isSelectionActiveWithin({ hasRange: true, isCollapsed: false, anchorWithin: true, focusWithin: false }),
    ).toBe(true);
  });

  it("is active when the focus is inside this view (drag started outside)", () => {
    expect(
      isSelectionActiveWithin({ hasRange: true, isCollapsed: false, anchorWithin: false, focusWithin: true }),
    ).toBe(true);
  });
});
