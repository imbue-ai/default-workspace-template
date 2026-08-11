import { describe, expect, it, vi } from "vitest";

// Mithril captures `requestAnimationFrame` at import time so it can schedule
// redraws. Vitest's default (node) environment has no such global, so provide
// a polyfill before any import is evaluated.
vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

// The rail reaches the live workspace only through the app list, which the
// helpers under test never consult.
vi.mock("../models/AgentManager", () => ({
  getApps: () => [],
}));

import { nextGlyphIndex, nextProjectName, placeMenu, shortcutAppNames, togglePins } from "./Sidebar";
import { SQUIGGLE_GLYPHS } from "./squiggles";

const VIEWPORT = { width: 1000, height: 800 };

describe("placeMenu", () => {
  it("hangs a 'below' menu off the anchor's bottom-left", () => {
    const anchor = { left: 40, right: 280, top: 100, bottom: 134, width: 240 };
    expect(placeMenu(anchor, { width: 240, height: 200 }, VIEWPORT, "below")).toEqual({ left: 40, top: 134 });
  });

  it("flips a 'below' menu above its anchor when it would run off the bottom", () => {
    const anchor = { left: 40, right: 280, top: 600, bottom: 640, width: 240 };
    expect(placeMenu(anchor, { width: 240, height: 300 }, VIEWPORT, "below")).toEqual({ left: 40, top: 300 });
  });

  it("puts a 'right' menu beside its anchor's top-right", () => {
    const anchor = { left: 100, right: 140, top: 200, bottom: 228, width: 40 };
    expect(placeMenu(anchor, { width: 180, height: 120 }, VIEWPORT, "right")).toEqual({ left: 140, top: 200 });
  });

  it("flips a 'right' menu to the left of its anchor when it would run off the edge", () => {
    const anchor = { left: 900, right: 940, top: 200, bottom: 228, width: 40 };
    expect(placeMenu(anchor, { width: 180, height: 120 }, VIEWPORT, "right")).toEqual({ left: 720, top: 200 });
  });

  it("clamps to the viewport when neither side fits", () => {
    const anchor = { left: 990, right: 998, top: 780, bottom: 796, width: 8 };
    // Too wide to flip (the left side would start at -2), so it clamps instead.
    expect(placeMenu(anchor, { width: 992, height: 400 }, VIEWPORT, "right")).toEqual({ left: 6, top: 394 });
  });
});

describe("shortcutAppNames", () => {
  it("lists the view's own apps by default", () => {
    expect(shortcutAppNames(["docs", "grafana"], { pinned: [], unpinned: [] })).toEqual(["docs", "grafana"]);
  });

  it("drops the ones this view unpinned and appends the ones it pinned", () => {
    expect(shortcutAppNames(["docs", "grafana"], { pinned: ["redis"], unpinned: ["docs"] })).toEqual([
      "grafana",
      "redis",
    ]);
  });

  it("never lists an app twice when a pin and the view agree", () => {
    expect(shortcutAppNames(["docs"], { pinned: ["docs"], unpinned: [] })).toEqual(["docs"]);
  });
});

describe("togglePins", () => {
  it("records unpinning an app the view shows", () => {
    expect(togglePins({ pinned: [], unpinned: [] }, "docs", true, false)).toEqual({ pinned: [], unpinned: ["docs"] });
  });

  it("records pinning an app the view does not show", () => {
    expect(togglePins({ pinned: [], unpinned: [] }, "redis", false, true)).toEqual({
      pinned: ["redis"],
      unpinned: [],
    });
  });

  it("keeps no entry that would have no effect", () => {
    // Re-pinning one of the view's own apps just clears the unpin, and
    // unpinning an app from elsewhere just drops the explicit pin.
    expect(togglePins({ pinned: [], unpinned: ["docs"] }, "docs", true, true)).toEqual({ pinned: [], unpinned: [] });
    expect(togglePins({ pinned: ["redis"], unpinned: [] }, "redis", false, false)).toEqual({
      pinned: [],
      unpinned: [],
    });
  });
});

describe("nextProjectName", () => {
  it("starts at one on an empty machine", () => {
    expect(nextProjectName([])).toBe("Project 1");
  });

  it("skips the numbers already taken, whatever their casing", () => {
    expect(nextProjectName(["project 1", "Newsreader", "PROJECT 2"])).toBe("Project 3");
  });

  it("fills a gap rather than counting past it", () => {
    expect(nextProjectName(["Project 1", "Project 3"])).toBe("Project 2");
  });
});

describe("nextGlyphIndex", () => {
  it("takes the first unused glyph", () => {
    expect(nextGlyphIndex([0, 1, 3])).toBe(2);
  });

  it("starts repeating once every glyph is in use", () => {
    const allGlyphs = SQUIGGLE_GLYPHS.map((_glyph, index) => index);
    expect(nextGlyphIndex(allGlyphs)).toBe(0);
  });
});
