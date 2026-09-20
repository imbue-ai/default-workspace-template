import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cascadeFrame } from "../geometry/frames";
import { activeFocusedWindowId, activePlacements, isLayoutDirty } from "../reducers/desktopState";
import { FakeDesktopApi, FakeDesktopSocket, settle } from "../testing/fakeShell";
import { appRecord, desktopRecord, placementRecord, windowRecord } from "../testing/records";
import type { ThemeMetrics } from "../theme/metrics";
import { DesktopStore, chooseInitialDesktopId } from "./DesktopStore";

const METRICS: ThemeMetrics = {
  titleBarHeight: 36,
  taskbarHeight: 48,
  cellWidth: 96,
  cellHeight: 112,
  gridInset: 16,
  windowMinWidth: 320,
  windowMinHeight: 240,
  titleMinVisible: 120,
  snapThreshold: 16,
  unsnapDistance: 12,
  dragThreshold: 4,
  touchTarget: 32,
};
const MODES = { isCompact: false, isTouch: false };
const CLIENT = "client-1";
const NO_LINK = { desktopId: null, open: null, launch: null };

function last<T>(items: readonly T[]): T | undefined {
  return items[items.length - 1];
}

let api: FakeDesktopApi;
let socket: FakeDesktopSocket;
let notices: string[];
let reloads: number;

function makeStore(): DesktopStore {
  const store = new DesktopStore({
    clientId: CLIENT,
    api,
    socket,
    metrics: METRICS,
    modes: MODES,
    redraw: () => undefined,
    notify: (message) => void notices.push(message),
    reloadInterface: () => void (reloads += 1),
  });
  store.setBackdropSize({ width: 1000, height: 800 });
  return store;
}

async function startedStore(): Promise<DesktopStore> {
  const store = makeStore();
  await store.start(NO_LINK);
  socket.deliver().onAppsUpdated([appRecord("docs"), appRecord("notes")]);
  return store;
}

beforeEach(() => {
  vi.useFakeTimers();
  api = new FakeDesktopApi();
  socket = new FakeDesktopSocket();
  notices = [];
  reloads = 0;
  api.desktops = [
    desktopRecord("home", { windows: [windowRecord("win-1", "docs", "/a"), windowRecord("win-2", "notes", "/b")] }),
    desktopRecord("work"),
  ];
  api.writeLayout("home", CLIENT, { updated_at: null, placements: [placementRecord("win-1")] });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("bootstrap", () => {
  it("lands on the recorded desktop, reports it, and fetches its layout", async () => {
    api.clients = [
      { id: CLIENT, active_desktop: "work" },
      { id: "other", active_desktop: "home" },
    ];
    const store = await startedStore();
    expect(store.getState().activeDesktopId).toBe("work");
    expect(socket.reports).toEqual([{ activeDesktop: "work", previousDesktop: "" }]);
    expect(store.getState().isLayoutLoaded).toBe(true);
  });

  it("a deep link's desktop wins, and its open and launch wait for the apps to arrive over the socket", async () => {
    api.clients = [{ id: CLIENT, active_desktop: "work" }];
    const store = makeStore();
    const started = store.start({
      desktopId: "home",
      open: { app: "docs", path: "/a" },
      launch: { app: "notes", launch: "new" },
    });
    await settle();
    expect(store.getState().activeDesktopId).toBe("home");
    expect(api.calls.filter((call) => call.startsWith("openWindow"))).toEqual([]);
    socket.deliver().onAppsUpdated([appRecord("docs"), appRecord("notes")]);
    await started;
    expect(api.calls.filter((call) => call.startsWith("openWindow"))).toEqual([
      "openWindow:home:docs:/a:focus:-",
      "openWindow:home:notes:/new:new:new",
    ]);
  });

  it("re-reports the client state when the socket reconnects", async () => {
    const store = await startedStore();
    socket.deliver().onConnected();
    expect(socket.reports.map((report) => report.activeDesktop)).toEqual(["home", "home"]);
    expect(store.getState().isDesktopsLoaded).toBe(true);
  });

  it("tells the user when the desktops cannot be read, instead of failing silently", async () => {
    api.refusal = "the shell is restarting";
    const store = makeStore();
    await store.start(NO_LINK);
    expect(notices).toEqual(["Could not read the desktops: the shell is restarting"]);
    expect(store.getState().activeDesktopId).toBeNull();
  });

  it("chooses the first desktop when nothing names one", () => {
    const desktops = [desktopRecord("a"), desktopRecord("b")];
    expect(chooseInitialDesktopId(desktops, "zzz", "zzz")).toBe("a");
    expect(chooseInitialDesktopId(desktops, null, "b")).toBe("b");
    expect(chooseInitialDesktopId(desktops, "b", "a")).toBe("b");
    expect(chooseInitialDesktopId([], "b", "a")).toBeNull();
  });
});

describe("saving", () => {
  it("saves a gesture after the debounce with the stamp it was based on, and takes the new stamp", async () => {
    const store = await startedStore();
    const base = store.getState().layout.updated_at;
    store.minimizeWindow("win-1");
    expect(isLayoutDirty(store.getState())).toBe(true);
    expect(api.calls.filter((call) => call.startsWith("savePlacements"))).toEqual([]);
    await vi.advanceTimersByTimeAsync(300);
    await settle();
    expect(api.calls.filter((call) => call.startsWith("savePlacements"))).toHaveLength(1);
    expect(isLayoutDirty(store.getState())).toBe(false);
    expect(store.getState().layout.updated_at).not.toBe(base);
    expect(api.layoutOf("home", CLIENT).placements[0].is_minimized).toBe(true);
  });

  it("takes the shell's layout when a save is refused as stale", async () => {
    const store = await startedStore();
    // The shell wrote a newer layout meanwhile (an agent op).
    api.writeLayout("home", CLIENT, { updated_at: null, placements: [placementRecord("win-2")] });
    store.minimizeWindow("win-1");
    await vi.advanceTimersByTimeAsync(300);
    await settle();
    expect(
      activePlacements(store.getState()).map((placement) => [placement.window_id, placement.is_minimized]),
    ).toEqual([
      ["win-1", true],
      ["win-2", false],
    ]);
    expect(isLayoutDirty(store.getState())).toBe(false);
  });

  it("ignores the broadcast of its own save and refetches for anyone else's", async () => {
    const store = await startedStore();
    store.minimizeWindow("win-1");
    await vi.advanceTimersByTimeAsync(300);
    await settle();
    const ownSaveId = api.calls.find((call) => call.startsWith("savePlacements"))?.split(":")[2] ?? "";
    const fetchesBefore = api.calls.filter((call) => call.startsWith("fetchPlacements")).length;
    socket.deliver().onPlacementsUpdated({ desktopId: "home", clientId: CLIENT, saveId: ownSaveId });
    socket
      .deliver()
      .onPlacementsUpdated({ desktopId: "home", clientId: "someone-else", saveId: "save-0000000000000000" });
    socket.deliver().onPlacementsUpdated({ desktopId: "work", clientId: CLIENT, saveId: "save-0000000000000000" });
    await settle();
    expect(api.calls.filter((call) => call.startsWith("fetchPlacements")).length).toBe(fetchesBefore);

    api.writeLayout("home", CLIENT, {
      updated_at: null,
      placements: [placementRecord("win-2", { state: "MAXIMIZED" })],
    });
    socket.deliver().onPlacementsUpdated({ desktopId: "home", clientId: CLIENT, saveId: "save-1111111111111111" });
    await settle();
    expect(activePlacements(store.getState()).find((placement) => placement.window_id === "win-2")?.state).toBe(
      "MAXIMIZED",
    );
  });

  it("flushes a pending save before switching desktops, and reports the switch with its previous desktop", async () => {
    const store = await startedStore();
    store.minimizeWindow("win-1");
    await store.switchDesktop("work");
    expect(api.layoutOf("home", CLIENT).placements[0].is_minimized).toBe(true);
    expect(last(socket.reports)).toEqual({ activeDesktop: "work", previousDesktop: "home" });
    expect(store.getState().activeDesktopId).toBe("work");
  });
});

describe("a deleted active desktop", () => {
  it("lands on the first remaining desktop, reports the move, and fetches that layout", async () => {
    const store = await startedStore();
    api.writeLayout("work", CLIENT, { updated_at: null, placements: [] });
    socket.deliver().onDesktopsUpdated([store.getState().desktops[1]]);
    await settle();
    expect(store.getState().activeDesktopId).toBe("work");
    expect(last(socket.reports)).toEqual({ activeDesktop: "work", previousDesktop: "" });
    expect(store.getState().isLayoutLoaded).toBe(true);
  });
});

describe("opening", () => {
  it("opens through the shell, places the window on top at once, and takes the shell's stamp", async () => {
    const store = await startedStore();
    const windowId = await store.openWindowAt("docs", "/new", "new", "new");
    await settle();
    expect(windowId).not.toBeNull();
    expect(api.calls).toContain("openWindow:home:docs:/new:new:new");
    const placements = activePlacements(store.getState());
    expect(last(placements)).toMatchObject({ window_id: windowId, frame: cascadeFrame(1), is_minimized: false });
    expect(store.isPlacedHere(windowId ?? "")).toBe(true);
    expect(store.getState().layout.updated_at).toBe(api.layoutOf("home", CLIENT).updated_at);
    expect(isLayoutDirty(store.getState())).toBe(false);
  });

  it("saves a gesture still waiting in the debounce before opening, so the refetch keeps it", async () => {
    const store = await startedStore();
    store.minimizeWindow("win-1");
    const windowId = await store.openWindowAt("docs", "/new", "new", "new");
    await settle();
    const stored = api.layoutOf("home", CLIENT).placements;
    expect(stored.find((placement) => placement.window_id === "win-1")?.is_minimized).toBe(true);
    expect(last(stored)?.window_id).toBe(windowId);
    expect(activePlacements(store.getState()).find((placement) => placement.window_id === "win-1")?.is_minimized).toBe(
      true,
    );
    expect(isLayoutDirty(store.getState())).toBe(false);
  });

  it("a focus open of a window already at the path raises it instead", async () => {
    const store = await startedStore();
    const windowId = await store.openWindowAt("notes", "/b", null, "focus");
    expect(windowId).toBe("win-2");
    expect(activeFocusedWindowId(store.getState())).toBe("win-2");
  });

  it("a focus shortcut raises the app's most recent window, and opens only when there is none", async () => {
    const store = await startedStore();
    await store.runLaunch("docs", "new", "focus");
    expect(activeFocusedWindowId(store.getState())).toBe("win-1");
    expect(api.calls.filter((call) => call.startsWith("openWindow"))).toEqual([]);
    await store.switchDesktop("work");
    await store.runLaunch("docs", "new", "focus");
    expect(api.calls).toContain("openWindow:work:docs:/new:new:new");
  });

  it("a seeded launch carries its params as the query string", async () => {
    const store = await startedStore();
    await store.openLaunchPath("docs", "new", { message: "hello there" });
    expect(api.calls).toContain("openWindow:home:docs:/new?message=hello+there:new:new");
  });

  it("tells the user when the shell refuses, and about a launch path that does not exist", async () => {
    const store = await startedStore();
    api.refusal = "No registered app named 'docs'";
    expect(await store.openWindowAt("docs", "/x", null, "focus")).toBeNull();
    api.refusal = null;
    await store.runLaunch("gone", "new", "new");
    expect(notices).toEqual([
      "Could not open docs: No registered app named 'docs'",
      "Cannot open: gone is not registered",
    ]);
  });

  it("opens a page's shell:open on the posting window's app and desktop", async () => {
    const store = await startedStore();
    await store.switchDesktop("work");
    await store.openPathFromWindow("win-2", "/c", "focus");
    expect(store.getState().activeDesktopId).toBe("home");
    expect(api.calls).toContain("openWindow:home:notes:/c:focus:-");
  });
});

describe("windows", () => {
  it("closes for everyone and drops the placement", async () => {
    const store = await startedStore();
    await store.closeWindow("win-1");
    expect(api.calls).toContain("closeWindow:home:win-1");
    expect(activePlacements(store.getState()).map((placement) => placement.window_id)).toEqual(["win-2"]);
  });

  it("the close chord tells the focused page first", async () => {
    const store = await startedStore();
    const requested: string[] = [];
    store.setPageDriver({
      reload: () => undefined,
      reloadApp: () => undefined,
      requestClose: (id) => void requested.push(id),
    });
    await store.closeFocusedWindow();
    expect(requested).toEqual(["win-1"]);
    expect(api.calls).toContain("closeWindow:home:win-1");
  });

  it("a taskbar click restores, minimizes the focused, or raises", async () => {
    const store = await startedStore();
    store.toggleTaskbarEntry("win-2");
    expect(activeFocusedWindowId(store.getState())).toBe("win-2");
    store.toggleTaskbarEntry("win-2");
    expect(activeFocusedWindowId(store.getState())).toBe("win-1");
    store.toggleTaskbarEntry("win-2");
    expect(activeFocusedWindowId(store.getState())).toBe("win-2");
  });

  it("defers restoring a window that is still settling on another client's open", async () => {
    const store = await startedStore();
    const settling = windowRecord("win-3", "docs", "/new", { is_settling: true });
    const home = store.getState().desktops[0];
    socket
      .deliver()
      .onDesktopsUpdated([{ ...home, windows: [...home.windows, settling] }, store.getState().desktops[1]]);
    store.restoreWindow("win-3");
    expect(activeFocusedWindowId(store.getState())).toBe("win-1");
    socket
      .deliver()
      .onDesktopsUpdated([
        { ...home, windows: [...home.windows, { ...settling, path: "/?doc=9", is_settling: false }] },
        store.getState().desktops[1],
      ]);
    expect(activeFocusedWindowId(store.getState())).toBe("win-3");
  });

  it("drops a deferred restore when the user leaves the desktop", async () => {
    const store = await startedStore();
    const settling = windowRecord("win-3", "docs", "/new", { is_settling: true });
    const home = store.getState().desktops[0];
    const work = store.getState().desktops[1];
    socket.deliver().onDesktopsUpdated([{ ...home, windows: [...home.windows, settling] }, work]);
    store.restoreWindow("win-3");
    await store.switchDesktop("work");
    await store.switchDesktop("home");
    socket
      .deliver()
      .onDesktopsUpdated([
        { ...home, windows: [...home.windows, { ...settling, path: "/?doc=9", is_settling: false }] },
        work,
      ]);
    expect(activeFocusedWindowId(store.getState())).toBe("win-1");
  });

  it("a settling window the shell placed in this client's layout (an agent's open) is this client's to show", async () => {
    const store = await startedStore();
    const settling = windowRecord("win-3", "docs", "/new", { is_settling: true });
    const home = store.getState().desktops[0];
    socket
      .deliver()
      .onDesktopsUpdated([{ ...home, windows: [...home.windows, settling] }, store.getState().desktops[1]]);
    expect(store.isPlacedHere("win-3")).toBe(false);
    api.writeLayout("home", CLIENT, {
      updated_at: null,
      placements: [placementRecord("win-1"), placementRecord("win-3")],
    });
    socket.deliver().onPlacementsUpdated({ desktopId: "home", clientId: CLIENT, saveId: "save-shell" });
    await settle();
    expect(store.isPlacedHere("win-3")).toBe(true);
    expect(activeFocusedWindowId(store.getState())).toBe("win-3");
    // Its restore is not deferred, since the page that clears the settling is this client's to load.
    store.minimizeWindow("win-3");
    store.restoreWindow("win-3");
    expect(activeFocusedWindowId(store.getState())).toBe("win-3");
  });

  it("raising the window already on top is not a gesture: nothing dirty, nothing saved", async () => {
    const store = await startedStore();
    const layout = store.getState().layout;
    store.raiseWindow("win-1");
    expect(store.getState().layout).toBe(layout);
    expect(isLayoutDirty(store.getState())).toBe(false);
  });

  it("toggles maximize, except in compact mode", async () => {
    const store = await startedStore();
    store.toggleMaximized("win-1");
    expect(last(activePlacements(store.getState()))?.state).toBe("MAXIMIZED");
    store.toggleMaximized("win-1");
    expect(last(activePlacements(store.getState()))?.state).toBe("NORMAL");
    store.setThemeMetrics(METRICS, { isCompact: true, isTouch: false });
    store.toggleMaximized("win-1");
    expect(last(activePlacements(store.getState()))?.state).toBe("NORMAL");
  });

  it("routes the agent's refresh ops to the pages and the reload to the interface", async () => {
    const store = await startedStore();
    const reloaded: string[] = [];
    store.setPageDriver({
      reload: (id) => void reloaded.push(`window:${id}`),
      reloadApp: (app) => void reloaded.push(`app:${app}`),
      requestClose: () => undefined,
    });
    socket.deliver().onLayoutOp({ op: "refresh", args: { window: "win-1" }, requester: "" });
    socket.deliver().onLayoutOp({ op: "refresh", args: { app: "docs" }, requester: "" });
    socket.deliver().onLayoutOp({ op: "reload_system_interface", args: {}, requester: "" });
    expect(reloaded).toEqual(["window:win-1", "app:docs"]);
    expect(reloads).toBe(1);
  });

  it("follows a pushed desktop switch without re-reporting a previous desktop", async () => {
    const store = await startedStore();
    socket.deliver().onActiveDesktopChanged({ clientId: CLIENT, desktopId: "work" });
    await settle();
    expect(store.getState().activeDesktopId).toBe("work");
    expect(last(socket.reports)).toEqual({ activeDesktop: "work", previousDesktop: "" });
    socket.deliver().onActiveDesktopChanged({ clientId: "other", desktopId: "home" });
    await settle();
    expect(store.getState().activeDesktopId).toBe("work");
  });
});

describe("gestures", () => {
  it("a move writes the frame on release, and a release in a zone writes the state instead", async () => {
    const store = await startedStore();
    store.beginWindowMove("win-1", { x: 100, y: 60 });
    store.updateWindowMove({ x: 150, y: 90 });
    expect(store.getGesture()).toMatchObject({ kind: "move", windowId: "win-1", zone: null });
    store.endWindowMove({ x: 150, y: 90 });
    const moved = last(activePlacements(store.getState()));
    expect(moved?.window_id).toBe("win-1");
    expect(moved?.frame.x).toBeCloseTo(0.1, 6);
    expect(moved?.frame.y).toBeCloseTo(0.0975, 6);
    expect(store.getGesture()).toBeNull();

    store.beginWindowMove("win-1", { x: 200, y: 100 });
    store.updateWindowMove({ x: 5, y: 400 });
    expect(store.getGesture()).toMatchObject({ zone: "SNAPPED_LEFT" });
    expect(store.snapPreviewRect()).toEqual({ x: 0, y: 0, width: 500, height: 800 });
    store.endWindowMove({ x: 5, y: 400 });
    const snapped = last(activePlacements(store.getState()));
    expect(snapped?.state).toBe("SNAPPED_LEFT");
    expect(snapped?.frame).toEqual(moved?.frame);
  });

  it("dragging a maximized window un-snaps it after the release distance", async () => {
    const store = await startedStore();
    store.setWindowState("win-1", "MAXIMIZED");
    store.beginWindowMove("win-1", { x: 500, y: 18 });
    store.updateWindowMove({ x: 505, y: 20 });
    expect(store.getGesture()).toMatchObject({ isUnsnapped: false });
    store.updateWindowMove({ x: 500, y: 300 });
    expect(store.getGesture()).toMatchObject({ isUnsnapped: true, zone: null });
    store.endWindowMove({ x: 500, y: 300 });
    const placement = last(activePlacements(store.getState()));
    expect(placement?.state).toBe("NORMAL");
    expect(placement?.frame.width).toBeCloseTo(0.6, 6);
  });

  it("a resize writes the frame; resizing a snapped window un-snaps it first", async () => {
    const store = await startedStore();
    store.beginWindowResize("win-1", "se");
    store.updateWindowResize({ x: 100, y: 50 });
    store.endWindowResize({ x: 100, y: 50 });
    const resized = last(activePlacements(store.getState()));
    expect(resized?.frame.width).toBeCloseTo(0.7, 6);
    expect(resized?.frame.height).toBeCloseTo(0.7625, 6);

    store.setWindowState("win-1", "SNAPPED_RIGHT");
    store.beginWindowResize("win-1", "w");
    expect(last(activePlacements(store.getState()))).toMatchObject({ state: "NORMAL", frame: { x: 0.5, width: 0.5 } });
  });

  it("in compact mode window gestures do nothing", async () => {
    const store = await startedStore();
    store.setThemeMetrics(METRICS, { isCompact: true, isTouch: true });
    store.beginWindowMove("win-1", { x: 100, y: 60 });
    expect(store.getGesture()).toBeNull();
  });

  it("a shortcut drag moves the shortcut to the cell under the pointer", async () => {
    const store = await startedStore();
    store.beginShortcutDrag("docs", "new", { x: 30, y: 40 }, { x: 10, y: 10 });
    store.updateShortcutDrag({ x: 230, y: 260 });
    expect(store.getGesture()).toMatchObject({
      kind: "shortcut",
      targetCell: { column: 2, row: 2 },
      iconPosition: { x: 220, y: 250 },
    });
    store.endShortcutDrag({ x: 230, y: 260 });
    await settle();
    expect(api.calls).toContain("moveDesktopShortcut:home:docs:new:2,2");
    expect(store.getGesture()).toBeNull();
  });
});

describe("desktops and shortcuts", () => {
  it("creating a desktop switches to it", async () => {
    const store = await startedStore();
    await store.createDesktop("Desktop 1", "#123456", 2);
    expect(store.getState().activeDesktopId).toBe("desktop-1");
    expect(store.getState().desktops.map((desktop) => desktop.id)).toEqual(["home", "work", "desktop-1"]);
  });

  it("adds a shortcut at the first free cell over the current grid", async () => {
    const store = await startedStore();
    await store.addShortcut("notes", "new", "focus");
    expect(api.calls).toContain("setDesktopShortcut:home:notes:new:0,0");
    await store.addShortcut("docs", "new", "new");
    expect(api.calls).toContain("setDesktopShortcut:home:docs:new:1,0");
    await store.removeShortcut("notes", "new");
    expect(store.getState().desktops[0].shortcuts.map((shortcut) => shortcut.target.app)).toEqual(["docs"]);
  });

  it("the launcher opens and closes", async () => {
    const store = await startedStore();
    store.openLauncher();
    expect(store.isLauncherOpen()).toBe(true);
    store.closeLauncher();
    expect(store.isLauncherOpen()).toBe(false);
  });

  it("reports a page's location and a lifecycle refusal", async () => {
    const store = await startedStore();
    await store.reportLocation("win-1", "/?doc=2", "Plan");
    expect(api.calls).toContain("reportWindowLocation:home:win-1:/?doc=2:Plan");
    api.refusal = "critical";
    await store.setAppLifecycle("docs", "stop");
    expect(notices).toEqual(["Failed to stop docs: critical"]);
  });
});
