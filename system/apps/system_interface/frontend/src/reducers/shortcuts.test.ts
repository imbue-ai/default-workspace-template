import { describe, expect, it } from "vitest";
import {
  appRecord,
  chatLikeAppRecord,
  desktopRecord,
  layoutRecord,
  placementRecord,
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

  it("a focus shortcut with nothing to focus, and a new shortcut always, open at the launch path", () => {
    const state = stateWith();
    expect(resolveLaunchRun(state, "notes", "new", "focus")).toEqual({ kind: "open", app: "notes", launch: "new" });
    expect(resolveLaunchRun(state, "docs", "new", "new")).toEqual({ kind: "open", app: "docs", launch: "new" });
  });

  it("a shortcut to the pin path raises the pinned window in either mode, even over a more recent window", () => {
    const desktop = desktopRecord("home", {
      windows: [windowRecord("win-pin", "chat", "/", { is_pinned: true }), windowRecord("win-other", "chat", "/")],
    });
    const state = [
      { type: "apps_updated", apps: [chatLikeAppRecord("chat")] } as DesktopEvent,
      { type: "desktops_updated", desktops: [desktop] } as DesktopEvent,
      { type: "desktop_activated", desktopId: "home" } as DesktopEvent,
      {
        type: "layout_loaded",
        desktopId: "home",
        layout: layoutRecord([placementRecord("win-pin", { is_minimized: true }), placementRecord("win-other")]),
      } as DesktopEvent,
    ].reduce(reduceDesktopState, initialDesktopState("client-1", MODES));
    expect(resolveLaunchRun(state, "chat", "root", "new")).toEqual({ kind: "raise", windowId: "win-pin" });
    expect(resolveLaunchRun(state, "chat", "root", "focus")).toEqual({ kind: "raise", windowId: "win-pin" });
  });

  it("a shortcut to the pin path opens as its mode says while the desktop holds no pinned window", () => {
    const state = [
      { type: "apps_updated", apps: [chatLikeAppRecord("chat")] } as DesktopEvent,
      { type: "desktops_updated", desktops: [desktopRecord("home")] } as DesktopEvent,
      { type: "desktop_activated", desktopId: "home" } as DesktopEvent,
    ].reduce(reduceDesktopState, initialDesktopState("client-1", MODES));
    expect(resolveLaunchRun(state, "chat", "root", "new")).toEqual({ kind: "open", app: "chat", launch: "root" });
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
