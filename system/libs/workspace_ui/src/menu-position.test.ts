import { describe, expect, it } from "vitest";

import { isInSafeTriangle, placeMenu, placeSubmenu } from "./menu-position";

const VIEWPORT = { width: 1000, height: 800 };
const SIZE = { width: 200, height: 100 };

describe("placeMenu", () => {
  it("hangs under the anchor with the shared gap, left edges aligned", () => {
    const anchor = { left: 50, right: 90, top: 100, bottom: 120, width: 40 };
    expect(placeMenu(anchor, SIZE, VIEWPORT, "below")).toEqual({ left: 50, top: 124 });
  });

  it("flips over the anchor when there is no room under it", () => {
    const low = { left: 50, right: 90, top: 760, bottom: 780, width: 40 };
    // 760 - 4 - 100: the gap sits between the menu's bottom edge and the anchor's top.
    expect(placeMenu(low, SIZE, VIEWPORT, "below")).toEqual({ left: 50, top: 656 });
  });

  it("hangs over the anchor for `above`, and flips under it when there is no room over", () => {
    const anchor = { left: 50, right: 90, top: 400, bottom: 420, width: 40 };
    expect(placeMenu(anchor, SIZE, VIEWPORT, "above")).toEqual({ left: 50, top: 296 });
    const high = { left: 50, right: 90, top: 20, bottom: 40, width: 40 };
    expect(placeMenu(high, SIZE, VIEWPORT, "above")).toEqual({ left: 50, top: 44 });
  });

  it("aligns right edges for `end`", () => {
    const anchor = { left: 500, right: 540, top: 100, bottom: 120, width: 40 };
    expect(placeMenu(anchor, SIZE, VIEWPORT, "below", "end")).toEqual({ left: 340, top: 124 });
  });

  it("sits beside the anchor for `right`, and flips to its left when it would overflow", () => {
    const anchor = { left: 200, right: 240, top: 100, bottom: 120, width: 40 };
    expect(placeMenu(anchor, SIZE, VIEWPORT, "right")).toEqual({ left: 244, top: 100 });
    const farRight = { left: 900, right: 940, top: 100, bottom: 120, width: 40 };
    expect(placeMenu(farRight, SIZE, VIEWPORT, "right")).toEqual({ left: 696, top: 100 });
  });

  it("lets a menu sit flush with an anchor that is itself at the window's left edge", () => {
    // The rail lives at x=0: its menus hang off it rather than a margin away from it.
    const anchor = { left: 0, right: 40, top: 100, bottom: 120, width: 40 };
    expect(placeMenu(anchor, SIZE, VIEWPORT, "below")).toEqual({ left: 0, top: 124 });
  });

  it("clamps a menu taller than the space it has to the top margin", () => {
    const anchor = { left: 50, right: 90, top: 100, bottom: 120, width: 40 };
    const tall = { width: 200, height: 790 };
    expect(placeMenu(anchor, tall, VIEWPORT, "below").top).toBe(6);
  });
});

/** A menu open near the bottom of a 1280x800 window, which is where the composer puts it.
 *  `rowTop` is the row that opened the submenu; `contentHeight` is a short four-row list. */
const BASE = {
  menuLeft: 400,
  menuWidth: 300,
  rowTop: 528,
  submenuPadding: 5,
  contentHeight: 138,
  submenuWidth: 300,
  maxSubmenuHeight: 368,
  viewportWidth: 1280,
  viewportHeight: 800,
  margin: 8,
  overlap: 5,
};

describe("placeSubmenu", () => {
  it("tucks under the menu's right edge and lines its first row up with the row", () => {
    // top is `rowTop - submenuPadding`, which puts the submenu's first ROW on `rowTop`.
    expect(placeSubmenu(BASE)).toMatchObject({ left: 695, top: 523, side: "trailing", isSlid: false });
  });

  it("slides up rather than being squeezed by the space below the row", () => {
    // The whole reason this file exists: the menu opens from the composer at the bottom of the
    // panel, so a ten-row catalog opened from a low row has nothing below it to grow into.
    const tall = placeSubmenu({ ...BASE, rowTop: 700, contentHeight: 368 });
    expect(tall.isSlid).toBe(true);
    // Slid up by exactly enough to stand on the bottom margin, and no shorter for it.
    expect(tall.top).toBe(800 - 8 - 368);
    expect(tall.maxHeight).toBe(368);
  });

  it("slides by only as much as it has to", () => {
    // 21px short of fitting, so it moves 21px -- not to some fixed anchor.
    const barely = placeSubmenu({ ...BASE, rowTop: 680 });
    expect(barely.top).toBe(800 - 8 - 138);
    expect(barely.isSlid).toBe(true);
  });

  it("holds the alignment whenever the box fits", () => {
    const high = placeSubmenu({ ...BASE, rowTop: 100, contentHeight: 368 });
    expect(high).toMatchObject({ top: 95, isSlid: false });
  });

  it("caps a list too tall for the window, and measures the slide against the cap", () => {
    const capped = placeSubmenu({ ...BASE, rowTop: 300, contentHeight: 2000, maxSubmenuHeight: 2000 });
    // The window is 800 tall with an 8px margin either side.
    expect(capped.maxHeight).toBe(784);
    expect(capped.top).toBe(8);
  });

  it("flips to the leading side when the trailing side would not fit", () => {
    const flipped = placeSubmenu({ ...BASE, menuLeft: 900 });
    expect(flipped.side).toBe("leading");
    expect(flipped.left).toBe(900 + 5 - 300);
  });

  it("pins to the left margin when the window is narrower than the box itself", () => {
    // Neither side fits a 300px box in a 200px window; the left margin keeps the first
    // characters readable.
    const pinned = placeSubmenu({ ...BASE, viewportWidth: 200, menuLeft: 20 });
    expect(pinned.left).toBe(8);
  });

  it("stops at the right margin when the trailing side overflows and the leading side is off-screen", () => {
    const pushed = placeSubmenu({ ...BASE, viewportWidth: 500, menuLeft: 100 });
    expect(pushed.side).toBe("trailing");
    expect(pushed.left).toBe(500 - 8 - 300);
  });

  it("keeps the box on screen when the row is off it", () => {
    const above = placeSubmenu({ ...BASE, rowTop: -50 });
    expect(above.top).toBe(8);
  });
});

describe("isInSafeTriangle", () => {
  const base = { edgeX: 700, top: 400, bottom: 600 };

  it("holds a pointer heading up and across towards the submenu", () => {
    expect(isInSafeTriangle({ x: 600, y: 520 }, { x: 500, y: 550 }, base)).toBe(true);
  });

  it("lets go of a pointer heading straight up the menu instead", () => {
    expect(isInSafeTriangle({ x: 500, y: 450 }, { x: 500, y: 550 }, base)).toBe(false);
  });

  it("lets go once the pointer is past the submenu's edge", () => {
    expect(isInSafeTriangle({ x: 750, y: 500 }, { x: 500, y: 550 }, base)).toBe(false);
  });

  it("holds the apex itself and the edge's corners", () => {
    const apex = { x: 500, y: 550 };
    expect(isInSafeTriangle(apex, apex, base)).toBe(true);
    expect(isInSafeTriangle({ x: 700, y: 400 }, apex, base)).toBe(true);
    expect(isInSafeTriangle({ x: 700, y: 600 }, apex, base)).toBe(true);
  });

  it("holds nothing off the line when the submenu has no height to aim at", () => {
    const flat = { edgeX: 700, top: 500, bottom: 500 };
    expect(isInSafeTriangle({ x: 600, y: 520 }, { x: 500, y: 550 }, flat)).toBe(false);
  });
});
