import { describe, expect, it } from "vitest";
import { isScrollMeasurable } from "./scrollVisibility";

describe("isScrollMeasurable", () => {
  it("is true for a visible element with a positive height and a live offset parent", () => {
    expect(isScrollMeasurable({ clientHeight: 800, hasOffsetParent: true })).toBe(true);
  });

  it("is false when the panel is hidden: zero height and no offset parent (display:none ancestor)", () => {
    // The core of the bug: an inactive dockview tab stays mounted but its
    // ancestor is display:none, so the scroll element reports a 0 height and a
    // null offsetParent. Scroll work must not run in this state.
    expect(isScrollMeasurable({ clientHeight: 0, hasOffsetParent: false })).toBe(false);
  });

  it("is false when height is zero even if an offset parent is reported", () => {
    expect(isScrollMeasurable({ clientHeight: 0, hasOffsetParent: true })).toBe(false);
  });

  it("is false when there is no offset parent even if a stale height is reported", () => {
    expect(isScrollMeasurable({ clientHeight: 800, hasOffsetParent: false })).toBe(false);
  });
});
