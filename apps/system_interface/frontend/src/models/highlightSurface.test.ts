import { describe, expect, it } from "vitest";

import { decideHighlightSurface } from "./highlightSurface";

describe("decideHighlightSurface", () => {
  it("opens a closed, unacknowledged highlighted tab (the reconnect case)", () => {
    // The Caretaker is present in the very first snapshot after a WS reconnect,
    // carrying a run key the user never acknowledged, tab closed. It must open --
    // and because the decision reads only the persisted ack, it does so with no
    // dependence on any in-session "already opened this key" state.
    expect(
      decideHighlightSurface({ isHighlighted: true, currentKey: "new", acknowledgedKey: "old", isTabOpen: false }),
    ).toBe("open");
  });

  it("opens when the run has never been acknowledged at all", () => {
    expect(
      decideHighlightSurface({ isHighlighted: true, currentKey: "k", acknowledgedKey: undefined, isTabOpen: false }),
    ).toBe("open");
  });

  it("leaves an already-open tab alone even with an unacknowledged new key", () => {
    // The tab is on screen; viewing it is how the run gets acknowledged.
    expect(
      decideHighlightSurface({ isHighlighted: true, currentKey: "new", acknowledgedKey: "old", isTabOpen: true }),
    ).toBe("noop");
  });

  it("does nothing once the current run is acknowledged", () => {
    expect(
      decideHighlightSurface({ isHighlighted: true, currentKey: "k", acknowledgedKey: "k", isTabOpen: true }),
    ).toBe("noop");
    expect(
      decideHighlightSurface({ isHighlighted: true, currentKey: "k", acknowledgedKey: "k", isTabOpen: false }),
    ).toBe("noop");
  });

  it("ignores non-highlighted agents", () => {
    expect(
      decideHighlightSurface({ isHighlighted: false, currentKey: "", acknowledgedKey: undefined, isTabOpen: false }),
    ).toBe("noop");
  });
});
