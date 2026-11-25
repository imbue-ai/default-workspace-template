// @vitest-environment jsdom
/**
 * The backdrop over a real store: the stage holding the grid, the pages' host, and the windows is inert while
 * the window views somebody else's desktop (the multiplayer spec, section 8.3), and takes input again on one of
 * the viewer's own; and the shield lies over a window's content under an open menu or the launcher, and on a
 * touch client over every window but the focused one.
 */
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resetPresenceForTesting, setOwnIdentityForTesting } from "../model/Presence";
import { activeFocusedWindowId, activePlacements } from "../reducers/desktopState";
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

function render(overrides: Partial<BackdropAttrs> = {}): HTMLElement {
  const desktop = store.getState().desktops.find((candidate) => candidate.id === store.getState().activeDesktopId);
  if (desktop === undefined) throw new Error("no active desktop");
  const attrs: BackdropAttrs = {
    store,
    desktop,
    placements: activePlacements(store.getState()),
    focusedWindowId: activeFocusedWindowId(store.getState()),
    selectedShortcutKey: null,
    openMenuWindowId: null,
    floatingEntries: [],
    openEntryMenuWindowId: null,
    sizeMenuTrigger: () => ({}),
    onEntryClick: vi.fn(),
    onEntryContextMenu: vi.fn(),
    isOverlayOpen: false,
    isAppShareOffered: false,
    onSelectShortcut: vi.fn(),
    onRunShortcut: vi.fn(),
    onShortcutContextMenu: vi.fn(),
    onBackdropContextMenu: vi.fn(),
    overview: null,
    letterbox: null,
    onWindowControl: vi.fn(),
    onPagesHostCreated: vi.fn(),
    ...overrides,
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
    desktopRecord("home", { windows: [windowRecord("win-1", "docs", "/a"), windowRecord("win-2", "docs", "/b")] }),
    desktopRecord("alice", { owner: "user:user-alice-0001", windows: [windowRecord("win-a", "docs", "/a")] }),
  ];
  api.writeLayout("home", CLIENT, {
    updated_at: null,
    placements: [placementRecord("win-1"), placementRecord("win-2")],
  });
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

describe("Share in a window's title bar", () => {
  const shareOf = (root: HTMLElement, windowId: string): Element | null =>
    root.querySelector(`[data-window-id="${windowId}"] [data-window-control="share"]`);

  it("is drawn on the focused window alone, and only where the shell offers it", () => {
    const offered = render({ isAppShareOffered: true });
    expect(shareOf(offered, "win-2")).not.toBeNull();
    expect(shareOf(offered, "win-1")).toBeNull();
    unmountViews();

    expect(shareOf(render({ isAppShareOffered: false }), "win-2")).toBeNull();
  });

  it("is drawn for a critical app, and never for an internal one", () => {
    socket.deliver().onAppsUpdated([appRecord("docs", { critical: true })]);
    expect(shareOf(render({ isAppShareOffered: true }), "win-2")).not.toBeNull();
    unmountViews();

    socket.deliver().onAppsUpdated([appRecord("docs", { internal: true })]);
    expect(shareOf(render({ isAppShareOffered: true }), "win-2")).toBeNull();
  });

  it("is not drawn for an app that is part of the workspace itself", () => {
    socket.deliver().onAppsUpdated([appRecord("docs", { system: true })]);
    const root = render({ isAppShareOffered: true });
    expect(root.querySelector('[data-window-id="win-2"] [data-window-control="refresh"]')).not.toBeNull();
    expect(shareOf(root, "win-2")).toBeNull();
  });

  it("is not drawn on a window whose app the shell no longer lists", () => {
    socket.deliver().onAppsUpdated([]);
    const root = render({ isAppShareOffered: true });
    expect(root.querySelector('[data-window-id="win-2"] [data-window-control="close"]')).not.toBeNull();
    expect(shareOf(root, "win-2")).toBeNull();
  });
});

describe("the shield over a window's content", () => {
  const shieldOf = (root: HTMLElement, windowId: string): Element | null =>
    root.querySelector(`[data-window-id="${windowId}"] [data-window-shield]`);

  it("lies over no window with nothing open, the unfocused one included", () => {
    const root = render();
    expect(root.querySelector('[data-window-id="win-1"][data-focused="false"]')).not.toBeNull();
    expect(root.querySelector('[data-window-id="win-2"][data-focused="true"]')).not.toBeNull();
    expect(shieldOf(root, "win-1")).toBeNull();
    expect(shieldOf(root, "win-2")).toBeNull();
  });

  it("lies over every window but the focused one on a touch client", () => {
    store.setThemeMetrics(METRICS, { isPhone: false, isTouch: true });
    const root = render();
    expect(shieldOf(root, "win-1")).not.toBeNull();
    expect(shieldOf(root, "win-2")).toBeNull();
  });

  it("lies over every window, the focused one too, while a menu or the launcher is open", () => {
    const root = render({ isOverlayOpen: true });
    expect(shieldOf(root, "win-1")).not.toBeNull();
    expect(shieldOf(root, "win-2")).not.toBeNull();
    unmountViews();
    store.setThemeMetrics(METRICS, { isPhone: false, isTouch: true });
    const touchRoot = render({ isOverlayOpen: true });
    expect(shieldOf(touchRoot, "win-1")).not.toBeNull();
    expect(shieldOf(touchRoot, "win-2")).not.toBeNull();
  });
});

describe("the window being moved", () => {
  it("has a compositor layer of its own until the move ends, and no other window has one", () => {
    const root = render();
    const promoted = (): (string | null)[] =>
      [...root.querySelectorAll<HTMLElement>("[data-window-id]")]
        .filter((chrome) => chrome.style.willChange === "transform")
        .map((chrome) => chrome.getAttribute("data-window-id"));
    expect(promoted()).toEqual([]);
    store.beginWindowMove("win-1", { x: 300, y: 60 });
    m.redraw.sync();
    expect(promoted()).toEqual(["win-1"]);
    store.endWindowMove({ x: 300, y: 60 });
    m.redraw.sync();
    expect(promoted()).toEqual([]);
  });
});
