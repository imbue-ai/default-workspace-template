import { describe, expect, it } from "vitest";
import { EMPTY_LAYOUT } from "../model/records";
import type { Layout } from "../model/records";
import { desktopRecord, placementRecord, windowRecord } from "../testing/records";
import { cascadeFrame } from "./frames";
import {
  dropStalePlacements,
  effectivePlacements,
  focusedWindowId,
  mostRecentlyFocusedWindowOfApp,
  placementOf,
  withWindowFrame,
  withWindowMinimized,
  withWindowPlacedOnOpen,
  withWindowRaised,
  withWindowRestored,
  withWindowState,
  withoutPlacement,
} from "./stack";

const desktop = desktopRecord("home", {
  windows: [
    windowRecord("win-1", "docs", "/a"),
    windowRecord("win-2", "notes", "/b"),
    windowRecord("win-3", "docs", "/c"),
  ],
});

function layoutOf(...ids: string[]): Layout {
  return { updated_at: "2026-09-19T00:00:00Z", placements: ids.map((id) => placementRecord(id)) };
}

describe("effectivePlacements", () => {
  it("reads a missing placement as minimized at the bottom of the stack with the next cascade frame", () => {
    const placements = effectivePlacements(layoutOf("win-2"), desktop);
    expect(placements.map((placement) => placement.window_id)).toEqual(["win-1", "win-3", "win-2"]);
    expect(placements[0]).toEqual({ window_id: "win-1", frame: cascadeFrame(1), state: "NORMAL", is_minimized: true });
    expect(placements[2].is_minimized).toBe(false);
  });

  it("drops a placement of a window the desktop no longer holds", () => {
    const placements = effectivePlacements(layoutOf("win-gone", "win-1"), desktop);
    expect(placements.map((placement) => placement.window_id)).toEqual(["win-2", "win-3", "win-1"]);
  });
});

describe("focus", () => {
  it("is the last placement that is not minimized, or nothing", () => {
    expect(focusedWindowId([placementRecord("win-1"), placementRecord("win-2", { is_minimized: true })])).toBe(
      "win-1",
    );
    expect(focusedWindowId([placementRecord("win-1", { is_minimized: true })])).toBeNull();
  });

  it("finds the app's window nearest the top of the stack, minimized or not", () => {
    const layout: Layout = {
      updated_at: null,
      placements: [
        placementRecord("win-3"),
        placementRecord("win-1", { is_minimized: true }),
        placementRecord("win-2"),
      ],
    };
    expect(mostRecentlyFocusedWindowOfApp(layout, desktop, "docs")?.id).toBe("win-1");
    expect(mostRecentlyFocusedWindowOfApp(layout, desktop, "notes")?.id).toBe("win-2");
    expect(mostRecentlyFocusedWindowOfApp(layout, desktop, "files")).toBeNull();
  });
});

describe("the verbs", () => {
  const layout = layoutOf("win-1", "win-2");

  it("raise moves the placement to the top and un-minimizes it", () => {
    const minimized: Layout = {
      ...layout,
      placements: [placementRecord("win-1", { is_minimized: true }), placementRecord("win-2")],
    };
    const raised = withWindowRaised(minimized, "win-1");
    expect(raised.placements.map((placement) => placement.window_id)).toEqual(["win-2", "win-1"]);
    expect(raised.placements[1].is_minimized).toBe(false);
  });

  it("minimize leaves the placement where it stands", () => {
    const minimized = withWindowMinimized(layout, "win-1");
    expect(minimized.placements.map((placement) => placement.window_id)).toEqual(["win-1", "win-2"]);
    expect(minimized.placements[0].is_minimized).toBe(true);
  });

  it("restore puts the window on top, shown and normal", () => {
    const snapped: Layout = {
      ...layout,
      placements: [placementRecord("win-1", { state: "MAXIMIZED", is_minimized: true })],
    };
    const restored = withWindowRestored(snapped, "win-1");
    expect(restored.placements[0]).toMatchObject({ window_id: "win-1", state: "NORMAL", is_minimized: false });
  });

  it("a state or a frame keeps the other and raises", () => {
    const snapped = withWindowState(layout, "win-1", "SNAPPED_LEFT");
    expect(snapped.placements[1]).toMatchObject({ window_id: "win-1", state: "SNAPPED_LEFT", frame: cascadeFrame(0) });
    const frame = { x: 0.2, y: 0.2, width: 0.4, height: 0.4 };
    const placed = withWindowFrame(snapped, "win-1", frame);
    expect(placed.placements[1]).toMatchObject({ window_id: "win-1", state: "NORMAL", frame });
  });

  it("a verb on a window with no placement starts from the default", () => {
    const raised = withWindowRaised(EMPTY_LAYOUT, "win-9");
    expect(raised.placements).toEqual([{ ...placementRecord("win-9"), frame: cascadeFrame(0) }]);
    expect(placementOf(layoutOf("win-1"), "win-9").is_minimized).toBe(true);
  });

  it("without and stale drops answer the same layout when nothing changes", () => {
    expect(withoutPlacement(layout, "win-9")).toBe(layout);
    expect(withoutPlacement(layout, "win-1").placements.map((placement) => placement.window_id)).toEqual(["win-2"]);
    expect(dropStalePlacements(layout, new Set(["win-1", "win-2"]))).toBe(layout);
    expect(dropStalePlacements(layout, new Set(["win-2"])).placements).toHaveLength(1);
  });

  it("an open lands on top at the next cascade frame", () => {
    const opened = withWindowPlacedOnOpen(layout, "win-3");
    expect(opened.placements[2]).toEqual({
      window_id: "win-3",
      frame: cascadeFrame(2),
      state: "NORMAL",
      is_minimized: false,
    });
  });
});
