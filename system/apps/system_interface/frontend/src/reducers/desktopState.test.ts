import { describe, expect, it } from "vitest";
import { cascadeFrame } from "../geometry/frames";
import { appRecord, desktopRecord, placementRecord, windowRecord } from "../testing/records";
import {
  activeFocusedWindowId,
  activePlacements,
  initialDesktopState,
  isAppStoppable,
  isLayoutDirty,
  reduceDesktopState,
  renderedState,
  taskbarEntries,
  windowTitle,
} from "./desktopState";
import type { DesktopEvent, DesktopState } from "./desktopState";

const MODES = { isCompact: false, isTouch: false };
const home = desktopRecord("home", {
  windows: [windowRecord("win-1", "docs", "/a"), windowRecord("win-2", "notes", "/b")],
});
const work = desktopRecord("work");

function reduceAll(state: DesktopState, ...events: DesktopEvent[]): DesktopState {
  return events.reduce(reduceDesktopState, state);
}

function loaded(): DesktopState {
  return reduceAll(
    initialDesktopState("client-1", MODES),
    { type: "apps_updated", apps: [appRecord("docs"), appRecord("notes")] },
    { type: "desktops_updated", desktops: [home, work] },
    { type: "desktop_activated", desktopId: "home" },
    { type: "layout_loaded", desktopId: "home", layout: { updated_at: "t1", placements: [placementRecord("win-1")] } },
  );
}

describe("loading", () => {
  it("knows when the apps, the desktops, and the layout have arrived", () => {
    const state = loaded();
    expect(state.isAppsLoaded && state.isDesktopsLoaded && state.isLayoutLoaded).toBe(true);
    expect(isLayoutDirty(state)).toBe(false);
    expect(activePlacements(state).map((placement) => placement.window_id)).toEqual(["win-2", "win-1"]);
    expect(activeFocusedWindowId(state)).toBe("win-1");
  });

  it("ignores a layout for a desktop that is not active", () => {
    const state = reduceDesktopState(loaded(), {
      type: "layout_loaded",
      desktopId: "work",
      layout: { updated_at: "t9", placements: [] },
    });
    expect(state.layout.updated_at).toBe("t1");
  });

  it("forgets the layout on a switch until the new one is fetched", () => {
    const state = reduceDesktopState(loaded(), { type: "desktop_activated", desktopId: "work" });
    expect(state.isLayoutLoaded).toBe(false);
    expect(state.layout.placements).toEqual([]);
    expect(isLayoutDirty(state)).toBe(false);
  });

  it("lands on the first desktop when the active one is deleted", () => {
    const state = reduceDesktopState(loaded(), { type: "desktops_updated", desktops: [work] });
    expect(state.activeDesktopId).toBe("work");
    expect(state.isLayoutLoaded).toBe(false);
  });
});

describe("the window verbs", () => {
  it("mark the layout dirty until a save of that version lands", () => {
    const raised = reduceDesktopState(loaded(), { type: "window_raised", windowId: "win-2" });
    expect(isLayoutDirty(raised)).toBe(true);
    expect(activeFocusedWindowId(raised)).toBe("win-2");
    const saved = reduceDesktopState(raised, {
      type: "layout_saved",
      desktopId: "home",
      version: raised.layoutVersion,
      updatedAt: "t2",
    });
    expect(isLayoutDirty(saved)).toBe(false);
    expect(saved.layout.updated_at).toBe("t2");
  });

  it("a save of an older version leaves a later gesture dirty", () => {
    const first = reduceDesktopState(loaded(), { type: "window_minimized", windowId: "win-1" });
    const second = reduceDesktopState(first, { type: "window_state_set", windowId: "win-2", state: "SNAPPED_LEFT" });
    const saved = reduceDesktopState(second, {
      type: "layout_saved",
      desktopId: "home",
      version: first.layoutVersion,
      updatedAt: "t2",
    });
    expect(isLayoutDirty(saved)).toBe(true);
  });

  it("a save answered after a newer layout was loaded changes nothing", () => {
    const gestured = reduceDesktopState(loaded(), { type: "window_minimized", windowId: "win-1" });
    const reloaded = reduceDesktopState(gestured, {
      type: "layout_loaded",
      desktopId: "home",
      layout: { updated_at: "t3", placements: [placementRecord("win-2")] },
    });
    const late = reduceDesktopState(reloaded, {
      type: "layout_saved",
      desktopId: "home",
      version: gestured.layoutVersion,
      updatedAt: "t2",
    });
    expect(late).toBe(reloaded);
    expect(late.layout.updated_at).toBe("t3");
    expect(isLayoutDirty(late)).toBe(false);
  });

  it("a save that changed nothing keeps the stamp", () => {
    const state = reduceDesktopState(loaded(), {
      type: "layout_saved",
      desktopId: "home",
      version: 0,
      updatedAt: null,
    });
    expect(state.layout.updated_at).toBe("t1");
  });

  it("restore, state, and frame edit the placement", () => {
    const frame = { x: 0.1, y: 0.1, width: 0.5, height: 0.5 };
    const state = reduceAll(
      loaded(),
      { type: "window_state_set", windowId: "win-1", state: "MAXIMIZED" },
      { type: "window_frame_set", windowId: "win-2", frame },
      { type: "window_restored", windowId: "win-1" },
    );
    const placements = activePlacements(state);
    expect(placements.map((placement) => placement.window_id)).toEqual(["win-2", "win-1"]);
    expect(placements[0].frame).toEqual(frame);
    expect(placements[1].state).toBe("NORMAL");
  });
});

describe("opens and closes this client made", () => {
  it("adds the window to the desktop and places it on top without dirtying the layout", () => {
    const opened = windowRecord("win-3", "docs", "/new", { is_settling: true });
    const state = reduceDesktopState(loaded(), {
      type: "window_opened_here",
      desktopId: "home",
      window: opened,
      isNew: true,
    });
    expect(state.desktops[0].windows.map((window) => window.id)).toEqual(["win-1", "win-2", "win-3"]);
    const placements = activePlacements(state);
    expect(placements[placements.length - 1]).toEqual({
      window_id: "win-3",
      frame: cascadeFrame(1),
      state: "NORMAL",
      is_minimized: false,
    });
    expect(isLayoutDirty(state)).toBe(false);
  });

  it("raises an existing window when the open was answered with one", () => {
    const state = reduceDesktopState(loaded(), {
      type: "window_opened_here",
      desktopId: "home",
      window: home.windows[1],
      isNew: false,
    });
    expect(state.desktops[0].windows).toHaveLength(2);
    expect(activeFocusedWindowId(state)).toBe("win-2");
  });

  it("drops a closed window and its placement", () => {
    const state = reduceDesktopState(loaded(), { type: "window_closed_here", desktopId: "home", windowId: "win-1" });
    expect(state.desktops[0].windows.map((window) => window.id)).toEqual(["win-2"]);
    expect(state.layout.placements).toEqual([]);
  });
});

describe("selectors", () => {
  it("render every window maximized while compact, and title windows after their page or their app", () => {
    expect(renderedState(placementRecord("win-1", { state: "NORMAL" }), { isCompact: true, isTouch: false })).toBe(
      "MAXIMIZED",
    );
    expect(renderedState(placementRecord("win-1", { state: "SNAPPED_LEFT" }), MODES)).toBe("SNAPPED_LEFT");
    expect(windowTitle(windowRecord("win-1", "docs", "/", { title: "Plan" }), appRecord("docs"))).toBe("Plan");
    expect(windowTitle(windowRecord("win-1", "docs", "/"), appRecord("docs"))).toBe("Docs");
    expect(windowTitle(windowRecord("win-1", "docs", "/"), undefined)).toBe("docs");
  });

  it("list the taskbar's entries in opening order with their focus and minimized marks", () => {
    const state = reduceDesktopState(loaded(), {
      type: "render_modes_changed",
      modes: { isCompact: true, isTouch: true },
    });
    expect(state.modes.isTouch).toBe(true);
    expect(
      taskbarEntries(state).map((entry) => [entry.window.id, entry.title, entry.isMinimized, entry.isFocused]),
    ).toEqual([
      ["win-1", "Docs", false, true],
      ["win-2", "Notes", true, false],
    ]);
  });

  it("offers stop and start only for a supervised, non-critical app outside a critical program", () => {
    const state = reduceDesktopState(initialDesktopState("c", MODES), {
      type: "apps_updated",
      apps: [
        appRecord("shell", { critical: true, program: "shell" }),
        appRecord("helper", { program: "shell" }),
        appRecord("loose", { program: "" }),
        appRecord("docs"),
      ],
    });
    expect(isAppStoppable(state, state.apps[0])).toBe(false);
    expect(isAppStoppable(state, state.apps[1])).toBe(false);
    expect(isAppStoppable(state, state.apps[2])).toBe(false);
    expect(isAppStoppable(state, state.apps[3])).toBe(true);
  });
});
