import { describe, expect, it } from "vitest";

import {
  applyAnchorAdjustment,
  withViewportAnchor,
  type AnchorableElement,
  type AnchorScrollContainer,
} from "./toggle-anchor";

/** A fake anchor whose viewport-top and connectedness the test mutates. */
function fakeAnchor(top: number): AnchorableElement & { setTop: (t: number) => void; disconnect: () => void } {
  let current = top;
  let connected = true;
  return {
    getBoundingClientRect: () => ({ top: current }),
    get isConnected() {
      return connected;
    },
    setTop: (t: number) => {
      current = t;
    },
    disconnect: () => {
      connected = false;
    },
  };
}

function fakeScrollEl(scrollTop: number): AnchorScrollContainer {
  return {
    getBoundingClientRect: () => ({ top: 0, left: 0, width: 800 }),
    scrollTop,
  };
}

describe("applyAnchorAdjustment", () => {
  it("scrolls by the anchor's movement so its viewport position is restored", () => {
    const scrollEl = fakeScrollEl(500);
    const anchor = fakeAnchor(100);
    anchor.setTop(140); // the toggle above grew content by 40px
    applyAnchorAdjustment(scrollEl, { anchor, topBefore: 100, fallback: null, fallbackTopBefore: 0 });
    expect(scrollEl.scrollTop).toBe(540);
  });

  it("falls back to the toggle header when the anchor was removed by the collapse", () => {
    const scrollEl = fakeScrollEl(500);
    const anchor = fakeAnchor(100);
    anchor.disconnect(); // the collapsed body contained the probed line
    const header = fakeAnchor(-30);
    header.setTop(-80); // header moved up by 50px
    applyAnchorAdjustment(scrollEl, { anchor, topBefore: 100, fallback: header, fallbackTopBefore: -30 });
    expect(scrollEl.scrollTop).toBe(450);
  });

  it("leaves scrollTop alone when both anchor and fallback are gone", () => {
    const scrollEl = fakeScrollEl(500);
    const anchor = fakeAnchor(100);
    anchor.disconnect();
    applyAnchorAdjustment(scrollEl, { anchor, topBefore: 100, fallback: null, fallbackTopBefore: 0 });
    expect(scrollEl.scrollTop).toBe(500);
  });

  it("leaves scrollTop alone when nothing moved", () => {
    const scrollEl = fakeScrollEl(500);
    const anchor = fakeAnchor(100);
    applyAnchorAdjustment(scrollEl, { anchor, topBefore: 100, fallback: null, fallbackTopBefore: 0 });
    expect(scrollEl.scrollTop).toBe(500);
  });
});

describe("withViewportAnchor", () => {
  it("runs the mutation even without a scroll container", () => {
    let mutated = false;
    withViewportAnchor(null, null, () => {
      mutated = true;
    });
    expect(mutated).toBe(true);
  });

  it("anchors on the fallback when no element can be probed (non-DOM container)", () => {
    // A fake container is not an HTMLElement, so the probe yields nothing and
    // the fallback (the toggle header) carries the anchoring.
    const scrollEl = fakeScrollEl(200);
    const header = fakeAnchor(50);
    withViewportAnchor(scrollEl, header, () => {
      header.setTop(90); // expanding above pushed the header down 40px
    });
    expect(scrollEl.scrollTop).toBe(240);
  });

  it("runs the mutation without adjustment when there is nothing to anchor on", () => {
    const scrollEl = fakeScrollEl(200);
    let mutated = false;
    withViewportAnchor(scrollEl, null, () => {
      mutated = true;
    });
    expect(mutated).toBe(true);
    expect(scrollEl.scrollTop).toBe(200);
  });
});
