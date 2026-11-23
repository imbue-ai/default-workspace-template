import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { placementOf } from "../geometry/stack";
import { ONBOARDING_CHECKLIST_FRAME, ONBOARDING_CHECKLIST_MESSAGE_TYPE } from "../model/tourCards";
import { activeFocusedWindowId } from "../reducers/desktopState";
import { FakeDesktopApi, FakeDesktopSocket, settle } from "../testing/fakeShell";
import {
  appRecord,
  chatLikeAppRecord,
  desktopRecord,
  placementRecord,
  themeMetricsRecord,
  windowRecord,
} from "../testing/records";
import { DesktopStore } from "./DesktopStore";
import { runTourAction } from "./tourActions";

const CLIENT = "client-1";
const NO_LINK = { desktopId: null, open: null, launch: null };

let api: FakeDesktopApi;
let store: DesktopStore;

/** A started store whose home desktop holds a docs window on top, the avatar's pinned window (buddy's, win-9)
 *  minimized under it, and a window of the app that lists the onboarding items (win-5), also minimized. */
beforeEach(async () => {
  vi.useFakeTimers();
  api = new FakeDesktopApi();
  api.apps = [
    appRecord("docs"),
    chatLikeAppRecord("buddy"),
    appRecord("items", {
      message_handlers: [{ type: ONBOARDING_CHECKLIST_MESSAGE_TYPE, path: null, show: "/" }],
    }),
  ];
  api.desktops = [
    desktopRecord("home", {
      windows: [
        windowRecord("win-1", "docs", "/a"),
        windowRecord("win-5", "items", "/"),
        windowRecord("win-9", "buddy", "/", { is_pinned: true, scope: "independent" }),
      ],
    }),
  ];
  api.writeLayout("home", CLIENT, { updated_at: null, placements: [placementRecord("win-1")] });
  store = new DesktopStore({
    clientId: CLIENT,
    api,
    socket: new FakeDesktopSocket(),
    metrics: themeMetricsRecord(),
    modes: { isPhone: false, isTouch: false },
    redraw: () => undefined,
    reloadInterface: () => undefined,
  });
  store.setBackdropSize({ width: 1000, height: 800 });
  await store.start(NO_LINK);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("what the shell does for the tour", () => {
  it("snaps the avatar's window to the right half of the desktop", () => {
    runTourAction(store, "snap-avatar-window-right");

    expect(placementOf(store.getState().layout, "win-9").state).toBe("SNAPPED_RIGHT");
  });

  it("brings the avatar's window forward, restoring it when it is minimized", () => {
    expect(placementOf(store.getState().layout, "win-9").is_minimized).toBe(true);

    runTourAction(store, "show-avatar-window");

    expect(placementOf(store.getState().layout, "win-9").is_minimized).toBe(false);
    expect(activeFocusedWindowId(store.getState())).toBe("win-9");
  });

  it("opens the onboarding checklist by its message's type, down the left of the desktop and in front", async () => {
    api.relayDeliveries = [{ app: "items", window_id: "win-5", has_no_desktop_window: false }];
    const docsBefore = placementOf(store.getState().layout, "win-1");

    runTourAction(store, "show-onboarding-checklist");
    await settle();

    expect(api.relayedMessages).toEqual([
      { type: "onboarding:show-checklist", clientId: CLIENT, payload: {}, sender: "system_interface" },
    ]);
    const placed = placementOf(store.getState().layout, "win-5");
    expect(placed.frame).toEqual({ x: 0, y: 0, width: 0.3, height: 0.85 });
    expect(placed.frame).toEqual(ONBOARDING_CHECKLIST_FRAME);
    expect(placed.is_minimized).toBe(false);
    expect(activeFocusedWindowId(store.getState())).toBe("win-5");
    expect(placementOf(store.getState().layout, "win-1")).toEqual(docsBefore);
  });

  it("does nothing for the checklist when no app registered its message", async () => {
    api.apps = [appRecord("docs"), chatLikeAppRecord("buddy")];
    const bare = new DesktopStore({
      clientId: CLIENT,
      api,
      socket: new FakeDesktopSocket(),
      metrics: themeMetricsRecord(),
      modes: { isPhone: false, isTouch: false },
      redraw: () => undefined,
      reloadInterface: () => undefined,
    });
    await bare.start(NO_LINK);

    runTourAction(bare, "show-onboarding-checklist");
    await settle();

    expect(api.relayedMessages).toEqual([]);
  });
});
