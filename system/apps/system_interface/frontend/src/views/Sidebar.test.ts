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

import type { SidebarTabRow } from "./Sidebar";
import { nextGlyphIndex, nextProjectName, pinnedAppNamesForView, placeMenu } from "./Sidebar";
import { SQUIGGLE_GLYPHS } from "./squiggles";

/** A tab-list row, built the way the workspace builds them (see
 *  `getSidebarRows`): the ref carries the kind, and nothing else matters here. */
function row(ref: string, kind: SidebarTabRow["kind"]): SidebarTabRow {
  return { ref, kind, label: ref, isOpen: false };
}

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

describe("pinnedAppNamesForView", () => {
  const ROWS = [
    row("chat:agent-1", "chat"),
    row("service:grafana", "app"),
    row("terminal:build", "terminal"),
    row("service:browser?session=2", "browser"),
    row("service:docs", "app"),
    row("url:abc123", "url"),
  ];

  it("takes the app members, in member order", () => {
    // Member order, not alphabetical: the rail's shortcuts read in the order
    // the apps were pinned.
    expect(pinnedAppNamesForView(ROWS, false)).toEqual(["grafana", "docs"]);
  });

  it("pins nothing in Everything", () => {
    // The unfiltered view lists every app on the machine already, so there is
    // nothing for it to shortcut.
    expect(pinnedAppNamesForView(ROWS, true)).toEqual([]);
  });

  it("is empty for a view holding no apps", () => {
    expect(pinnedAppNamesForView([row("chat:agent-1", "chat")], false)).toEqual([]);
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
