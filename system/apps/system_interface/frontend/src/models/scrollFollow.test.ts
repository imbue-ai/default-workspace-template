import { describe, expect, it } from "vitest";
import { nextUserScrolledUp, isSelectionActiveWithin } from "./scrollFollow";

const base = { userMovedUp: false, userMovedDown: false, hasMoreAfter: false };

describe("nextUserScrolledUp", () => {
  it("detaches on any user-evidenced upward movement, even while touching the bottom", () => {
    // The core of the jitter bug: while streaming, a small wheel-up must stop
    // tail-following so the next redraw does not re-pin to the bottom.
    expect(nextUserScrolledUp({ ...base, userMovedUp: true, isAtBottom: true, wasUserScrolledUp: false })).toBe(true);
  });

  it("detaches on an upward movement high above the bottom", () => {
    expect(nextUserScrolledUp({ ...base, userMovedUp: true, isAtBottom: false, wasUserScrolledUp: false })).toBe(true);
  });

  it("reattaches when a downward movement touches the true tail", () => {
    expect(nextUserScrolledUp({ ...base, userMovedDown: true, isAtBottom: true, wasUserScrolledUp: true })).toBe(
      false,
    );
  });

  it("does not reattach on a downward movement that stops short of the bottom", () => {
    // Touch, not a proximity band: stopping near the tail is still reading.
    expect(nextUserScrolledUp({ ...base, userMovedDown: true, isAtBottom: false, wasUserScrolledUp: true })).toBe(
      true,
    );
  });

  it("does not reattach at the bottom of a jumped window that has newer history below", () => {
    // After an offset jump the window sits off the live tail, so newer events
    // remain unloaded below; that window's bottom is not the tail.
    expect(
      nextUserScrolledUp({
        ...base,
        userMovedDown: true,
        isAtBottom: true,
        hasMoreAfter: true,
        wasUserScrolledUp: true,
      }),
    ).toBe(true);
  });

  it("preserves a reader's detachment on machinery movement at the bottom", () => {
    // A page landing's shrink clamps the reader to the new bottom, then a
    // native-anchoring adjustment fires a scroll event there. No user input was
    // involved (both movement flags false): the reader must not be reattached
    // and snapped to a tail they never asked for.
    expect(nextUserScrolledUp({ ...base, isAtBottom: true, wasUserScrolledUp: true })).toBe(true);
  });

  it("preserves following on machinery movement (a tail pin's echo, a shrink-clamp)", () => {
    expect(nextUserScrolledUp({ ...base, isAtBottom: true, wasUserScrolledUp: false })).toBe(false);
    // Content collapsing below a follower (eviction, a turn regrouping) clamps
    // scrollTop with no user involved: keep following.
    expect(nextUserScrolledUp({ ...base, isAtBottom: false, wasUserScrolledUp: false })).toBe(false);
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
