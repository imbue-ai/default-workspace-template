import { describe, expect, it } from "vitest";
import { ELSEWHERE_STATE, FOLLOW_STATE, reduceScrollPosition, reduceScrollbarInteraction } from "./state";
import type { ScrollAnchor, ScrollPositionState, ScrollbarInteractionState, ScrollbarMapping } from "./types";

const anchor: ScrollAnchor = { rowKey: "evt-42", offsetPx: 12 };
const otherAnchor: ScrollAnchor = { rowKey: "evt-7", offsetPx: -3 };
const userControlled: ScrollPositionState = { kind: "USER_CONTROLLED", anchor };

const mapping: ScrollbarMapping = {
  segments: [{ kind: "physical", trackStart: 0, trackEnd: 1, heightPx: 1000 }],
  totalEvents: 10,
};
const otherMapping: ScrollbarMapping = {
  segments: [{ kind: "virtual", trackStart: 0, trackEnd: 1, firstIndex: 0, endIndex: 10 }],
  totalEvents: 10,
};
const scrollbarState: ScrollbarInteractionState = { kind: "SCROLLBAR", frozen: mapping };

describe("reduceScrollPosition", () => {
  it("FOLLOW leaves on any scroll that is not at the tail (any intent to scroll up)", () => {
    const next = reduceScrollPosition(FOLLOW_STATE, {
      kind: "USER_SCROLLED",
      source: "wheel",
      anchor,
      atTail: false,
    });
    expect(next).toEqual({ kind: "USER_CONTROLLED", anchor });
  });

  it("FOLLOW stays FOLLOW (same reference) for a scroll that stays at the tail", () => {
    const next = reduceScrollPosition(FOLLOW_STATE, {
      kind: "USER_SCROLLED",
      source: "wheel",
      anchor,
      atTail: true,
    });
    expect(next).toBe(FOLLOW_STATE);
  });

  it("FOLLOW stays FOLLOW on new messages", () => {
    expect(reduceScrollPosition(FOLLOW_STATE, { kind: "EVENTS_APPENDED" })).toBe(FOLLOW_STATE);
  });

  it("every input source can disengage FOLLOW", () => {
    for (const source of ["wheel", "keyboard", "scrollbar", "selection-autoscroll"] as const) {
      const next = reduceScrollPosition(FOLLOW_STATE, { kind: "USER_SCROLLED", source, anchor, atTail: false });
      expect(next.kind).toBe("USER_CONTROLLED");
    }
  });

  it("USER_CONTROLLED returns to FOLLOW only by scrolling all the way to the tail", () => {
    const stillUp = reduceScrollPosition(userControlled, {
      kind: "USER_SCROLLED",
      source: "wheel",
      anchor: otherAnchor,
      atTail: false,
    });
    expect(stillUp).toEqual({ kind: "USER_CONTROLLED", anchor: otherAnchor });

    const atBottom = reduceScrollPosition(userControlled, {
      kind: "USER_SCROLLED",
      source: "wheel",
      anchor: otherAnchor,
      atTail: true,
    });
    expect(atBottom).toBe(FOLLOW_STATE);
  });

  it("USER_CONTROLLED re-anchors on every non-tail scroll", () => {
    const next = reduceScrollPosition(userControlled, {
      kind: "USER_SCROLLED",
      source: "keyboard",
      anchor: otherAnchor,
      atTail: false,
    });
    expect(next).toEqual({ kind: "USER_CONTROLLED", anchor: otherAnchor });
  });

  it("USER_CONTROLLED keeps its anchor when new messages stream in", () => {
    expect(reduceScrollPosition(userControlled, { kind: "EVENTS_APPENDED" })).toBe(userControlled);
  });

  it("sending a message snaps USER_CONTROLLED back to FOLLOW", () => {
    expect(reduceScrollPosition(userControlled, { kind: "MESSAGE_SENT" })).toBe(FOLLOW_STATE);
  });

  it("sending a message while FOLLOW keeps the same state reference", () => {
    expect(reduceScrollPosition(FOLLOW_STATE, { kind: "MESSAGE_SENT" })).toBe(FOLLOW_STATE);
  });

  it("a scrollbar jump lands in USER_CONTROLLED from either state", () => {
    expect(reduceScrollPosition(FOLLOW_STATE, { kind: "JUMPED_TO_INDEX", anchor })).toEqual(userControlled);
    expect(reduceScrollPosition(userControlled, { kind: "JUMPED_TO_INDEX", anchor: otherAnchor })).toEqual({
      kind: "USER_CONTROLLED",
      anchor: otherAnchor,
    });
  });
});

describe("reduceScrollbarInteraction", () => {
  it("engaging the scrollbar freezes the mapping captured at engage time", () => {
    const next = reduceScrollbarInteraction(ELSEWHERE_STATE, { kind: "SCROLLBAR_ENGAGED", mappingAtEngage: mapping });
    expect(next).toEqual({ kind: "SCROLLBAR", frozen: mapping });
  });

  it("re-engaging while already on the scrollbar keeps the original frozen mapping", () => {
    const next = reduceScrollbarInteraction(scrollbarState, {
      kind: "SCROLLBAR_ENGAGED",
      mappingAtEngage: otherMapping,
    });
    expect(next).toBe(scrollbarState);
  });

  it("any other interaction returns to ELSEWHERE", () => {
    expect(reduceScrollbarInteraction(scrollbarState, { kind: "OTHER_INTERACTION" })).toBe(ELSEWHERE_STATE);
  });

  it("other interactions while ELSEWHERE keep the same state reference", () => {
    expect(reduceScrollbarInteraction(ELSEWHERE_STATE, { kind: "OTHER_INTERACTION" })).toBe(ELSEWHERE_STATE);
  });
});
