import { describe, expect, it } from "vitest";
import {
  appRecord,
  desktopRecord,
  launchPathRecord,
  layoutRecord,
  placementRecord,
  screenAppRecord,
  windowRecord,
} from "../testing/records";
import { initialDesktopState, reduceDesktopState } from "./desktopState";
import type { DesktopEvent, DesktopState } from "./desktopState";
import { cellForAddedShortcut, nextDesktopName, resolveLaunchRun } from "./shortcuts";

const MODES = { isPhone: false, isTouch: false };
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
      layout: layoutRecord([placementRecord("win-2"), placementRecord("win-1", { is_minimized: true })]),
    });
    expect(resolveLaunchRun(state, "docs", "new", "focus")).toEqual({ kind: "raise", windowId: "win-1" });
  });

  it("a focus shortcut of a one-window app's POST launch path runs it even with a window to focus", () => {
    const screen = screenAppRecord();
    // A many-window app (its default shortcut opens new windows): a focus shortcut of its POST path still raises.
    const terminal = appRecord("terminal", {
      launch_paths: [launchPathRecord({ id: "new", path: "/new", method: "POST" })],
      default_shortcuts: [{ launch: "new", mode: "new" }],
    });
    const viewing = desktopRecord("home", {
      windows: [windowRecord("win-9", "screen", "/"), windowRecord("win-8", "terminal", "/t/1")],
    });
    const state = [
      { type: "apps_updated", apps: [screen, terminal] } as DesktopEvent,
      { type: "desktops_updated", desktops: [viewing] } as DesktopEvent,
      { type: "desktop_activated", desktopId: "home" } as DesktopEvent,
      {
        type: "layout_loaded",
        desktopId: "home",
        layout: layoutRecord([placementRecord("win-9"), placementRecord("win-8")]),
      } as DesktopEvent,
    ].reduce(reduceDesktopState, initialDesktopState("client-1", MODES));

    expect(resolveLaunchRun(state, "screen", "screen", "focus")).toEqual({ kind: "raise", windowId: "win-9" });
    expect(resolveLaunchRun(state, "screen", "browser", "focus")).toEqual({
      kind: "open",
      app: "screen",
      launch: "browser",
    });
    expect(resolveLaunchRun(state, "terminal", "new", "focus")).toEqual({ kind: "raise", windowId: "win-8" });
  });

  it("a focus shortcut with nothing to focus, and a new shortcut always, open at the launch path", () => {
    const state = stateWith();
    expect(resolveLaunchRun(state, "notes", "new", "focus")).toEqual({ kind: "open", app: "notes", launch: "new" });
    expect(resolveLaunchRun(state, "docs", "new", "new")).toEqual({ kind: "open", app: "docs", launch: "new" });
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

  it("an unknown app is connecting, not unregistered, until the first app list lands", () => {
    const connecting = [
      { type: "desktops_updated", desktops: [home] } as DesktopEvent,
      { type: "desktop_activated", desktopId: "home" } as DesktopEvent,
    ].reduce(reduceDesktopState, initialDesktopState("client-1", MODES));
    expect(resolveLaunchRun(connecting, "docs", "new", "focus")).toEqual({ kind: "connecting" });
    const loaded = reduceDesktopState(connecting, { type: "apps_updated", apps: [] });
    expect(resolveLaunchRun(loaded, "docs", "new", "focus")).toEqual({
      kind: "unavailable",
      reason: "docs is not registered",
    });
  });
});

describe("new desktops and added shortcuts", () => {
  it("mints the first free Desktop N", () => {
    expect(nextDesktopName([{ id: "home", name: "Home" }])).toBe("Desktop 1");
    expect(
      nextDesktopName([
        { id: "desktop-1", name: "Renamed" },
        { id: "x", name: "desktop 2" },
      ]),
    ).toBe("Desktop 3");
  });

  it("puts an added shortcut at the first free cell in reading order over the current grid", () => {
    expect(cellForAddedShortcut(home, { columns: 3, rows: 3 })).toEqual({ column: 1, row: 0 });
    expect(cellForAddedShortcut(home, { columns: 1, rows: 3 })).toEqual({ column: 0, row: 2 });
  });
});
