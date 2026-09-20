import { describe, expect, it } from "vitest";
import { appRecord, desktopRecord, placementRecord, windowRecord } from "../testing/records";
import { initialDesktopState, reduceDesktopState } from "./desktopState";
import type { DesktopEvent, DesktopState } from "./desktopState";
import { cellForAddedShortcut, nextDesktopName, nextGlyphIndex, resolveLaunchRun } from "./shortcuts";

const MODES = { isCompact: false, isTouch: false };
const home = desktopRecord("home", {
  windows: [windowRecord("win-1", "docs", "/a"), windowRecord("win-2", "docs", "/b")],
  shortcuts: [
    { target: { kind: "launch", app: "docs", launch: "new" }, mode: "focus", cell: { column: 0, row: 0 } },
    { target: { kind: "launch", app: "notes", launch: "new" }, mode: "new", cell: { column: 0, row: 1 } },
  ],
});

function stateWith(...events: DesktopEvent[]): DesktopState {
  return [
    { type: "apps_updated", apps: [appRecord("docs"), appRecord("notes")] } as DesktopEvent,
    { type: "desktops_updated", desktops: [home] } as DesktopEvent,
    { type: "desktop_activated", desktopId: "home" } as DesktopEvent,
    ...events,
  ].reduce(reduceDesktopState, initialDesktopState("client-1", MODES));
}

describe("resolveLaunchRun", () => {
  it("a focus shortcut raises the app's most recently focused window when there is one", () => {
    const state = stateWith({
      type: "layout_loaded",
      desktopId: "home",
      layout: {
        updated_at: null,
        placements: [placementRecord("win-2"), placementRecord("win-1", { is_minimized: true })],
      },
    });
    expect(resolveLaunchRun(state, "docs", "new", "focus")).toEqual({ kind: "raise", windowId: "win-1" });
  });

  it("a focus shortcut with nothing to focus, and a new shortcut always, open at the launch path", () => {
    const state = stateWith();
    expect(resolveLaunchRun(state, "notes", "new", "focus")).toEqual({
      kind: "open",
      app: "notes",
      path: "/new",
      launch: "new",
    });
    expect(resolveLaunchRun(state, "docs", "new", "new")).toEqual({
      kind: "open",
      app: "docs",
      path: "/new",
      launch: "new",
    });
  });

  it("says why a shortcut cannot run", () => {
    const state = stateWith();
    expect(resolveLaunchRun(state, "gone", "new", "focus")).toEqual({
      kind: "unavailable",
      reason: "gone is not registered",
    });
    expect(resolveLaunchRun(state, "docs", "odd", "focus")).toEqual({
      kind: "unavailable",
      reason: "Docs has no launch path odd",
    });
  });
});

describe("new desktops and added shortcuts", () => {
  it("mints the first free Desktop N and the first unused glyph", () => {
    expect(nextDesktopName([{ id: "home", name: "Home" }])).toBe("Desktop 1");
    expect(
      nextDesktopName([
        { id: "desktop-1", name: "Renamed" },
        { id: "x", name: "desktop 2" },
      ]),
    ).toBe("Desktop 3");
    expect(nextGlyphIndex([0, 1, 3], 10)).toBe(2);
    expect(nextGlyphIndex([0, 1, 2, 3, 4, 5, 6, 7, 8, 9], 10)).toBe(0);
    expect(nextGlyphIndex([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 0], 10)).toBe(1);
  });

  it("puts an added shortcut at the first free cell in reading order over the current grid", () => {
    expect(cellForAddedShortcut(home, { columns: 3, rows: 3 })).toEqual({ column: 1, row: 0 });
    expect(cellForAddedShortcut(home, { columns: 1, rows: 3 })).toEqual({ column: 0, row: 2 });
  });
});
