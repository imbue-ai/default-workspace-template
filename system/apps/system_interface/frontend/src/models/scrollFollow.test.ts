import { describe, expect, it } from "vitest";
import { nextUserScrolledUp, isSelectionActiveWithin } from "./scrollFollow";

const base = { isClamp: false, didScrollUp: false, didScrollDown: false, hasMoreAfter: false };

describe("nextUserScrolledUp", () => {
  it("disengages following on any upward scroll, even within the bottom band", () => {
    // The core of the jitter bug: while streaming, the viewport sits within the
    // bottom band and a small upward scroll must stop tail-following so the next
    // redraw does not re-pin it to the bottom.
    expect(nextUserScrolledUp({ ...base, didScrollUp: true, isNearBottom: true, wasUserScrolledUp: false })).toBe(
      true,
    );
  });

  it("disengages following on an upward scroll high above the bottom", () => {
    expect(nextUserScrolledUp({ ...base, didScrollUp: true, isNearBottom: false, wasUserScrolledUp: false })).toBe(
      true,
    );
  });

  it("re-arms following on a downward scroll into the true tail", () => {
    expect(nextUserScrolledUp({ ...base, didScrollDown: true, isNearBottom: true, wasUserScrolledUp: true })).toBe(
      false,
    );
  });

  it("does not re-arm on a downward scroll still above the bottom band", () => {
    expect(nextUserScrolledUp({ ...base, didScrollDown: true, isNearBottom: false, wasUserScrolledUp: true })).toBe(
      true,
    );
  });

  it("does not re-arm at the bottom of a jumped window that has newer history below", () => {
    // After an offset jump the window sits off the live tail, so newer events
    // remain unloaded below; being near that window's bottom is not the tail.
    expect(
      nextUserScrolledUp({
        ...base,
        didScrollDown: true,
        isNearBottom: true,
        hasMoreAfter: true,
        wasUserScrolledUp: true,
      }),
    ).toBe(true);
  });

  it("does not re-arm a scrolled-up reader on a zero-delta event at the bottom", () => {
    // The landing-clamp cascade: an older page lands, its scrollHeight shrink
    // clamps the reading user to the new bottom, and a native-anchoring
    // adjustment (or a programmatic pin's echo) then fires a scroll event with
    // no direction at that clamped position. Position alone is not intent; the
    // reader must not be snapped to the tail they never asked for.
    expect(nextUserScrolledUp({ ...base, isNearBottom: true, wasUserScrolledUp: true })).toBe(true);
  });

  it("keeps following on a zero-delta event at the bottom (a tail pin's own echo)", () => {
    expect(nextUserScrolledUp({ ...base, isNearBottom: true, wasUserScrolledUp: false })).toBe(false);
  });

  it("keeps following through a shrink-clamp (does not read the clamp as scroll-up)", () => {
    // Eviction / a turn collapsing into one row shortens the content; the browser
    // pushes scrollTop up to the new max. didScrollUp looks true, but a follower
    // must keep following (the same redraw re-pins to the true tail).
    expect(
      nextUserScrolledUp({
        ...base,
        didScrollUp: true,
        isNearBottom: true,
        isClamp: true,
        wasUserScrolledUp: false,
      }),
    ).toBe(false);
  });

  it("does not re-engage following for a scrolled-up reader on a shrink-clamp", () => {
    // A reader parked in history must not be yanked to the tail just because
    // content below them collapsed and the browser clamped scrollTop.
    expect(
      nextUserScrolledUp({
        ...base,
        didScrollUp: true,
        isNearBottom: true,
        isClamp: true,
        wasUserScrolledUp: true,
      }),
    ).toBe(true);
  });
});

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
