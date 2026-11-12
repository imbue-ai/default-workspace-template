// @vitest-environment jsdom
/**
 * The backdrop over a real store: the stage holding the grid, the pages' host, and the windows is inert while
 * the window views somebody else's desktop (the multiplayer spec, section 8.3), and takes input again on one of
 * the viewer's own.
 */
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resetPresenceForTesting, setOwnIdentityForTesting } from "../model/Presence";
import { activePlacements } from "../reducers/desktopState";
import { DesktopStore } from "../store/DesktopStore";
import { FakeDesktopApi, FakeDesktopSocket } from "../testing/fakeShell";
import { appRecord, desktopRecord, placementRecord, themeMetricsRecord, windowRecord } from "../testing/records";
import { Backdrop } from "./Backdrop";
import type { BackdropAttrs } from "./Backdrop";

const METRICS = themeMetricsRecord();
const CLIENT = "client-1";
const NO_LINK = { desktopId: null, open: null, launch: null };

let api: FakeDesktopApi;
let socket: FakeDesktopSocket;
let store: DesktopStore;

function render(): HTMLElement {
  const desktop = store.getState().desktops.find((candidate) => candidate.id === store.getState().activeDesktopId);
  if (desktop === undefined) throw new Error("no active desktop");
  const attrs: BackdropAttrs = {
    store,
    desktop,
    placements: activePlacements(store.getState()),
    focusedWindowId: null,
    selectedShortcutKey: null,
    openMenuWindowId: null,
    floatingEntries: [],
    openEntryMenuWindowId: null,
    sizeMenuTrigger: () => ({}),
    onEntryClick: vi.fn(),
    onEntryContextMenu: vi.fn(),
    isOverlayOpen: false,
    onSelectShortcut: vi.fn(),
    onRunShortcut: vi.fn(),
    onShortcutContextMenu: vi.fn(),
    onBackdropContextMenu: vi.fn(),
    overview: null,
    letterbox: null,
    onWindowControl: vi.fn(),
    onShowDetachedWindow: vi.fn(),
    onHideWindowGhost: vi.fn(),
    onBringBackWindow: vi.fn(),
    onPagesHostCreated: vi.fn(),
  };
  return mountView(() => m(Backdrop, attrs));
}

function stageOf(root: HTMLElement): HTMLElement {
  const stage = root.querySelector<HTMLElement>(".desktop-stage");
  if (stage === null) throw new Error("no stage");
  return stage;
}

beforeEach(async () => {
  api = new FakeDesktopApi();
  socket = new FakeDesktopSocket();
  api.apps = [appRecord("docs")];
  api.desktops = [
    desktopRecord("home", { windows: [windowRecord("win-1", "docs", "/a")] }),
    desktopRecord("alice", { owner: "user:user-alice-0001", windows: [windowRecord("win-a", "docs", "/a")] }),
  ];
  api.writeLayout("home", CLIENT, { updated_at: null, placements: [placementRecord("win-1")] });
  api.writeLayout("alice", api.ownerLayoutClientId, {
    updated_at: "2026-10-29T00:00:00Z",
    placements: [placementRecord("win-a")],
  });
  setOwnIdentityForTesting({
    owner: true,
    user_id: "user-josh-0001",
    email: "josh@example.com",
    participant: "user:user-josh-0001",
  });
  store = new DesktopStore({
    clientId: CLIENT,
    api,
    socket,
    metrics: METRICS,
    modes: { isPhone: false, isTouch: false },
    redraw: () => undefined,
    reloadInterface: () => undefined,
  });
  store.setBackdropSize({ width: 1000, height: 800 });
  await store.start(NO_LINK);
});

afterEach(() => {
  unmountViews();
  resetPresenceForTesting();
});

describe("the stage while viewing", () => {
  it("is inert while the window views somebody else's desktop, and not on one of the viewer's own", async () => {
    const own = render();
    const ownStage = stageOf(own);
    expect(ownStage.hasAttribute("inert")).toBe(false);
    expect(ownStage.querySelector(".windows-viewing")).toBeNull();
    expect(ownStage.querySelector(".shortcut-grid")).not.toBeNull();
    expect(ownStage.querySelector(".live-pages")).not.toBeNull();
    unmountViews();

    await store.switchDesktop("alice");
    const viewed = render();
    const viewedStage = stageOf(viewed);
    // The grid, the pages' host, and the windows all sit under the one inert stage.
    expect(viewedStage.hasAttribute("inert")).toBe(true);
    expect(viewedStage.querySelector(".shortcut-grid")).not.toBeNull();
    expect(viewedStage.querySelector(".live-pages")).not.toBeNull();
    expect(viewedStage.querySelector(".windows-viewing")).not.toBeNull();
    // The band around the stage is outside it, so what lies there (the chip, the tray) is not inert.
    expect(viewed.querySelector(".backdrop")?.hasAttribute("inert")).toBe(false);
    unmountViews();

    await store.stopViewing();
    expect(stageOf(render()).hasAttribute("inert")).toBe(false);
  });
});
