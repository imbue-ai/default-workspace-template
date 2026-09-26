import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cascadeFrame } from "../geometry/frames";
import { placementOf } from "../geometry/stack";
import { getPresentUsers, resetPresenceForTesting } from "../model/Presence";
import { activeFocusedWindowId, activePlacements, isLayoutDirty } from "../reducers/desktopState";
import { STILL_CONNECTING_NOTICE, resolveLaunchRun } from "../reducers/shortcuts";
import { FakeDesktopApi, FakeDesktopSocket, PINNED_WINDOW_FRAME, offerApps, settle } from "../testing/fakeShell";
import {
  appRecord,
  clientRecord,
  desktopRecord,
  launchPathRecord,
  placementRecord,
  presentUserRecord,
  themeMetricsRecord,
  windowRecord,
} from "../testing/records";
import type { AppRecord } from "../model/records";
import { DesktopStore, chooseInitialDesktopId } from "./DesktopStore";

const METRICS = themeMetricsRecord();
const MODES = { isCompact: false, isTouch: false };
const CLIENT = "client-1";
const NO_LINK = { desktopId: null, open: null, launch: null };
const PLAIN_BAR = { mode: "bar", style: "plain", position: null } as const;

function last<T>(items: readonly T[]): T | undefined {
  return items[items.length - 1];
}

let api: FakeDesktopApi;
let socket: FakeDesktopSocket;
let notices: string[];
let reloads: number;

function makeStore(redraw: () => void = () => undefined): DesktopStore {
  const store = new DesktopStore({
    clientId: CLIENT,
    api,
    socket,
    metrics: METRICS,
    modes: MODES,
    redraw,
    notify: (message) => void notices.push(message),
    reloadInterface: () => void (reloads += 1),
  });
  store.setBackdropSize({ width: 1000, height: 800 });
  return store;
}

/** A started store; the apps come from the inventory (``api.apps``), no socket message needed. */
async function startedStore(redraw: () => void = () => undefined): Promise<DesktopStore> {
  const store = makeStore(redraw);
  await store.start(NO_LINK);
  return store;
}

beforeEach(() => {
  vi.useFakeTimers();
  api = new FakeDesktopApi();
  socket = new FakeDesktopSocket();
  notices = [];
  reloads = 0;
  api.apps = [appRecord("docs"), appRecord("notes")];
  api.desktops = [
    desktopRecord("home", { windows: [windowRecord("win-1", "docs", "/a"), windowRecord("win-2", "notes", "/b")] }),
    desktopRecord("work"),
  ];
  api.writeLayout("home", CLIENT, { updated_at: null, placements: [placementRecord("win-1")] });
});

afterEach(() => {
  resetPresenceForTesting();
  vi.useRealTimers();
});

describe("bootstrap", () => {
  it("arrives first, reads the inventory, lands on the desktop the shell answers, reports it, and fetches its layout", async () => {
    api.clients = [
      clientRecord(CLIENT, { active_desktop: "work" }),
      clientRecord("other", { active_desktop: "home" }),
    ];
    const store = await startedStore();
    // The arrival is posted before the inventory is read (it may seed a desktop); the avatar read is independent.
    expect(api.calls.indexOf(`arriveClient:${CLIENT}`)).toBeGreaterThanOrEqual(0);
    expect(api.calls.indexOf(`arriveClient:${CLIENT}`)).toBeLessThan(api.calls.indexOf("fetchInventory"));
    expect(api.calls.filter((call) => call === "fetchInventory")).toHaveLength(1);
    expect(api.calls).not.toContain("fetchClients");
    expect(store.getState().activeDesktopId).toBe("work");
    expect(socket.reports).toEqual([{ activeDesktop: "work", previousDesktop: "" }]);
    expect(store.getState().isLayoutLoaded).toBe(true);
    expect(store.getReplacedDesktop()).toBeNull();
  });

  it("knows the apps from the inventory, so a desktop is complete with a socket that never connects", async () => {
    const store = await startedStore();
    // Nothing was delivered over the socket: the apps, the desktops, and the layout all came over REST.
    expect(socket.handlers).not.toBeNull();
    expect(store.getState().isAppsLoaded).toBe(true);
    expect(store.getState().apps.map((app) => app.name)).toEqual(["docs", "notes"]);
    expect(resolveLaunchRun(store.getState(), "notes", "new", "new")).toEqual({
      kind: "open",
      app: "notes",
      launch: "new",
    });
    await store.runLaunch("gone", "new", "focus");
    expect(notices).toEqual(["Cannot open: gone is not registered"]);
  });

  it("is connecting, not missing apps, until the inventory answers; the socket's app list also ends the wait", async () => {
    const answerReads = api.holdReads();
    const store = makeStore();
    const starting = store.start(NO_LINK);
    await settle();
    expect(store.getState().isAppsLoaded).toBe(false);
    expect(resolveLaunchRun(store.getState(), "docs", "new", "focus")).toEqual({ kind: "connecting" });
    await store.runLaunch("docs", "new", "focus");
    expect(notices).toEqual([STILL_CONNECTING_NOTICE]);
    let isAppsLoaded = false;
    void store.whenAppsLoaded().then(() => {
      isAppsLoaded = true;
    });
    socket.deliver().onAppsUpdated([appRecord("docs")]);
    await settle();
    expect(isAppsLoaded).toBe(true);
    expect(store.getState().isAppsLoaded).toBe(true);
    answerReads();
    await starting;
    expect(store.getState().apps.map((app) => app.name)).toEqual(["docs", "notes"]);
  });

  it("lands a first-time user on the desktop the shell seeded for them, and notices a replaced one until dismissed", async () => {
    const seeded = desktopRecord("alice-2", { name: "Alice 2" });
    api.arrival = { created_desktop: seeded, replaced_desktop_name: "Alice" };
    const redraws: number[] = [];
    const store = await startedStore(() => redraws.push(1));
    expect(store.getState().desktops.map((desktop) => desktop.id)).toEqual(["home", "work", "alice-2"]);
    expect(store.getState().activeDesktopId).toBe("alice-2");
    expect(store.getReplacedDesktop()).toEqual({ replacedName: "Alice", seededName: "Alice 2" });
    const before = redraws.length;
    store.dismissReplacedDesktopNotice();
    expect(store.getReplacedDesktop()).toBeNull();
    expect(redraws.length).toBe(before + 1);
  });

  it("the notice names the seeded desktop even when a deep link lands the client elsewhere", async () => {
    api.arrival = { created_desktop: desktopRecord("alice", { name: "Alice" }), replaced_desktop_name: "Alice" };
    const store = makeStore();
    await store.start({ desktopId: "work", open: null, launch: null });
    expect(store.getState().activeDesktopId).toBe("work");
    expect(store.getReplacedDesktop()).toEqual({ replacedName: "Alice", seededName: "Alice" });
  });

  it("falls back to the recorded desktop when the arrival cannot be settled", async () => {
    api.clients = [clientRecord(CLIENT, { active_desktop: "work" })];
    const store = makeStore();
    api.refusal = "shell down";
    const started = store.start(NO_LINK);
    api.refusal = null;
    await started;
    // The arrival was refused; the reads that followed were not, and the client's recorded desktop is the landing.
    expect(api.calls).toContain(`arriveClient:${CLIENT}`);
    expect(store.getState().activeDesktopId).toBe("work");
  });

  it("a deep link's desktop wins, and its open and launch run against the inventory's apps", async () => {
    api.clients = [clientRecord(CLIENT, { active_desktop: "work" })];
    const store = makeStore();
    await store.start({
      desktopId: "home",
      open: { app: "docs", path: "/a" },
      launch: { app: "notes", launch: "new" },
    });
    expect(store.getState().activeDesktopId).toBe("home");
    // The open goes straight to the windows route; the launch goes through the launch route, which opens.
    expect(api.calls.filter((call) => call.startsWith("openWindow") || call.startsWith("launch"))).toEqual([
      "openWindow:home:docs:/a:focus",
      "launch:home:notes:new:{}:new",
      "openWindow:home:notes:/new:new",
    ]);
  });

  it("re-reports the client state when the socket reconnects, and takes the layout written while it was down", async () => {
    const store = await startedStore();
    socket.deliver().onConnected();
    expect(socket.reports.map((report) => report.activeDesktop)).toEqual(["home", "home"]);
    expect(store.getState().isDesktopsLoaded).toBe(true);
    // The first connect leaves the fetch to the bootstrap; a reconnect reads the layout again.
    expect(api.calls.filter((call) => call === "fetchPlacements:home")).toHaveLength(1);
    api.writeLayout("home", CLIENT, { updated_at: null, placements: [placementRecord("win-2")] });
    socket.deliver().onConnected();
    await settle();
    expect(api.calls.filter((call) => call === "fetchPlacements:home")).toHaveLength(2);
    expect(activeFocusedWindowId(store.getState())).toBe("win-2");
  });

  it("adopts the desktop the shell recorded for the client while the socket was down, rather than asserting its own", async () => {
    const store = await startedStore();
    socket.deliver().onConnected();
    // Another window of this client switched to work meanwhile; the push never reached this one.
    api.clients = [clientRecord(CLIENT, { active_desktop: "work" })];
    api.writeLayout("work", CLIENT, { updated_at: null, placements: [] });
    socket.deliver().onConnected();
    await settle();
    expect(store.getState().activeDesktopId).toBe("work");
    expect(last(socket.reports)).toEqual({ activeDesktop: "work", previousDesktop: "" });
    expect(api.calls).toContain("fetchPlacements:work");
    expect(store.getState().isLayoutLoaded).toBe(true);
  });

  it("tells the user when the inventory cannot be read, instead of failing silently", async () => {
    api.refusal = "the shell is restarting";
    const store = makeStore();
    await store.start(NO_LINK);
    expect(notices).toEqual(["Could not read the desktops and apps: the shell is restarting"]);
    expect(store.getState().activeDesktopId).toBeNull();
    expect(store.getState().isAppsLoaded).toBe(false);
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

  it("a stream of broadcasts while a save waits does not push the save back", async () => {
    const store = await startedStore();
    store.minimizeWindow("win-1");
    for (let round = 0; round < 5; round += 1) {
      await vi.advanceTimersByTimeAsync(100);
      socket.deliver().onDesktopsUpdated([...store.getState().desktops]);
      socket.deliver().onAppsUpdated([appRecord("docs"), appRecord("notes")]);
    }
    await settle();
    expect(api.calls.filter((call) => call.startsWith("savePlacements"))).toHaveLength(1);
    expect(isLayoutDirty(store.getState())).toBe(false);
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

describe("location reports", () => {
  it("posts a report that differs from the stored record, and none that says what is stored", async () => {
    const store = await startedStore();
    expect(await store.reportLocation("win-1", "/a", "")).toBe(true);
    expect(await store.reportLocation("win-1", "/a", "Plan")).toBe(true);
    expect(api.calls.filter((call) => call.startsWith("reportWindowLocation"))).toEqual([
      "reportWindowLocation:home:win-1:client-1:/a:Plan",
    ]);
    // The answer is taken at once, ahead of the broadcast.
    const windows = store.getState().desktops[0].windows;
    expect(windows.find((window) => window.id === "win-1")?.title).toBe("Plan");
  });

  /** A started store whose home desktop holds one independent pinned window, ``win-1``. */
  async function independentStore(): Promise<DesktopStore> {
    api.desktops = [
      desktopRecord("home", {
        windows: [windowRecord("win-1", "docs", "/", { is_pinned: true, scope: "independent" })],
      }),
      desktopRecord("work"),
    ];
    return startedStore();
  }

  it("an independent window's report is this client's own: kept beside the layout, never on the shared record", async () => {
    const store = await independentStore();
    expect(await store.reportLocation("win-1", "/?doc=2", "Second")).toBe(true);
    expect(api.calls).toContain("reportWindowLocation:home:win-1:client-1:/?doc=2:Second");
    expect(store.getState().desktops[0].windows[0].path).toBe("/");
    expect(store.getState().layout.window_paths).toEqual({ "win-1": { path: "/?doc=2", title: "Second" } });
    expect(isLayoutDirty(store.getState())).toBe(false);
    // The same report again is not posted: the client's own path already says so.
    expect(await store.reportLocation("win-1", "/?doc=2", "Second")).toBe(true);
    expect(api.calls.filter((call) => call.startsWith("reportWindowLocation"))).toHaveLength(1);
    // The shell's announcement of the write refetches the layout, whose stamp is unchanged but whose paths moved.
    api.windowPaths.set("client-1/win-1", { path: "/?doc=3", title: "Third" });
    socket.deliver().onPlacementsUpdated({ desktopId: "home", clientId: CLIENT, saveId: "save-shell" });
    await settle();
    expect(store.getState().layout.window_paths).toEqual({ "win-1": { path: "/?doc=3", title: "Third" } });
    expect(store.getLayoutLoadsRevision()).toBe(2);
  });

  it("a stored path announced while a gesture waits to be saved does not throw the gesture away", async () => {
    api.writeLayout("home", CLIENT, { updated_at: null, placements: [] });
    const store = await independentStore();
    // The entry click restores the window; its page then reports where it is before the debounce has saved.
    store.restoreWindow("win-1");
    expect(isLayoutDirty(store.getState())).toBe(true);
    await store.reportLocation("win-1", "/?doc=2", "Second");
    socket.deliver().onPlacementsUpdated({ desktopId: "home", clientId: CLIENT, saveId: "save-shell" });
    await settle();
    expect(activeFocusedWindowId(store.getState())).toBe("win-1");
    expect(isLayoutDirty(store.getState())).toBe(true);
    expect(store.getState().layout.window_paths).toEqual({ "win-1": { path: "/?doc=2", title: "Second" } });
    await vi.advanceTimersByTimeAsync(300);
    await settle();
    expect(api.layoutOf("home", CLIENT).placements.map((placement) => placement.window_id)).toEqual(["win-1"]);
    expect(isLayoutDirty(store.getState())).toBe(false);
  });

  it("a refused report answers false and changes nothing", async () => {
    const store = await startedStore();
    api.refusal = "no such window";
    expect(await store.reportLocation("win-1", "/?doc=2", "")).toBe(false);
    expect(store.getState().desktops[0].windows[0].path).toBe("/a");
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
    const windowId = await store.openWindowAt("docs", "/new", "new");
    await settle();
    expect(windowId).not.toBeNull();
    expect(api.calls).toContain("openWindow:home:docs:/new:new");
    const placements = activePlacements(store.getState());
    expect(last(placements)).toMatchObject({ window_id: windowId, frame: cascadeFrame(1), is_minimized: false });
    expect(store.getState().layout.updated_at).toBe(api.layoutOf("home", CLIENT).updated_at);
    expect(isLayoutDirty(store.getState())).toBe(false);
  });

  it("saves a gesture still waiting in the debounce before opening, so the refetch keeps it", async () => {
    const store = await startedStore();
    store.minimizeWindow("win-1");
    const windowId = await store.openWindowAt("docs", "/new", "new");
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
    const windowId = await store.openWindowAt("notes", "/b", "focus");
    expect(windowId).toBe("win-2");
    expect(activeFocusedWindowId(store.getState())).toBe("win-2");
  });

  it("a focus shortcut raises the app's most recent window, and opens only when there is none", async () => {
    const store = await startedStore();
    await store.runLaunch("docs", "new", "focus");
    expect(activeFocusedWindowId(store.getState())).toBe("win-1");
    expect(api.calls.filter((call) => call.startsWith("launch"))).toEqual([]);
    await store.switchDesktop("work");
    await store.runLaunch("docs", "new", "focus");
    expect(api.calls).toContain("launch:work:docs:new:{}:new");
    expect(api.calls).toContain("openWindow:work:docs:/new:new");
  });

  /** A store whose home desktop holds buddy's independent pinned window, win-9, minimized for this client. */
  async function storeWithPinnedBuddy(): Promise<DesktopStore> {
    api.desktops = [
      desktopRecord("home", {
        windows: [
          windowRecord("win-1", "docs", "/a"),
          windowRecord("win-9", "buddy", "/", { is_pinned: true, scope: "independent" }),
        ],
      }),
    ];
    return startedStore();
  }

  it("a launch-path row opens a new window, or raises the pinned window when its path is the pin's", async () => {
    const store = await storeWithPinnedBuddy();
    await store.runLaunchRow("docs", "new");
    expect(api.calls).toContain("launch:home:docs:new:{}:new");
    socket.deliver().onAppsUpdated([
      appRecord("buddy", {
        pin: { path: "/", style: "plain", scope: "independent", default_mode: "bar" },
        launch_paths: [launchPathRecord({ id: "root", label: "Buddy", path: "/" })],
      }),
    ]);
    const launchesBefore = api.calls.filter((call) => call.startsWith("launch")).length;
    await store.runLaunchRow("buddy", "root");
    expect(api.calls.filter((call) => call.startsWith("launch"))).toHaveLength(launchesBefore);
    expect(activePlacements(store.getState()).find((placement) => placement.window_id === "win-9")?.is_minimized).toBe(
      false,
    );
    await store.runLaunchRow("buddy", "missing");
    expect(last(notices)).toBe("Cannot open: buddy has no launch path missing");
  });

  it("a free-text row launches into this client's view of the app's pinned window", async () => {
    const store = await storeWithPinnedBuddy();
    offerApps(api, socket, [
      appRecord("buddy", {
        pin: { path: "/", style: "plain", scope: "independent", default_mode: "bar" },
        launch_paths: [launchPathRecord({ id: "new", path: "/new", params: ["message"], text_param: "message" })],
      }),
    ]);
    expect(await store.runFreeText("buddy", "new", "hello there")).toBe(true);
    expect(last(api.calls.filter((call) => call.startsWith("launch")))).toBe(
      `launch:home:buddy:new:{"message":"hello there"}:window:win-9`,
    );
    expect(api.calls.filter((call) => call.startsWith("openWindow"))).toEqual([]);
    // The page the shell answered is applied as this client's own navigation, so the pinned page follows it.
    expect(store.takeOwnNavigation()).toEqual({ windowId: "win-9", path: "/new?message=hello+there" });
    expect(activePlacements(store.getState()).find((placement) => placement.window_id === "win-9")?.is_minimized).toBe(
      false,
    );
    // Empty text runs the launch path with no text param at all.
    expect(await store.runFreeText("buddy", "new", "")).toBe(true);
    expect(last(api.calls.filter((call) => call.startsWith("launch")))).toBe(`launch:home:buddy:new:{}:window:win-9`);
    // A text over a GET launch path's path bound is refused here, before the shell sees it.
    expect(await store.runFreeText("buddy", "new", "x".repeat(2100))).toBe(false);
    expect(last(notices)).toBe("Too long to send from here");
    // A POST launch path carries the text in a body: no bound, and the window lands where the app answers.
    offerApps(api, socket, [
      appRecord("buddy", {
        pin: { path: "/", style: "plain", scope: "independent", default_mode: "bar" },
        launch_paths: [
          launchPathRecord({
            id: "new",
            path: "/api/intake",
            method: "POST",
            params: ["message"],
            presets: { target: "new_chat" },
            text_param: "message",
          }),
        ],
      }),
    ]);
    api.postLaunchAnswer = "/?chat=agent-1";
    expect(await store.runFreeText("buddy", "new", "x".repeat(2100))).toBe(true);
    expect(store.takeOwnNavigation()).toEqual({ windowId: "win-9", path: "/?chat=agent-1" });
  });

  it("a free-text row opens a new window for an app with no pinned window here, and launches into a linked one", async () => {
    const store = await startedStore();
    offerApps(api, socket, [
      appRecord("docs", {
        launch_paths: [launchPathRecord({ id: "new", path: "/new", params: ["message"], text_param: "message" })],
      }),
      appRecord("buddy", {
        pin: { path: "/", style: "plain", scope: "linked", default_mode: "bar" },
        launch_paths: [launchPathRecord({ id: "new", path: "/new", params: ["message"], text_param: "message" })],
      }),
    ]);
    expect(await store.runFreeText("docs", "new", "hello")).toBe(true);
    expect(api.calls).toContain(`launch:home:docs:new:{"message":"hello"}:new`);
    expect(api.calls).toContain("openWindow:home:docs:/new?message=hello:new");
    // A linked pinned window is launched into too: the page a launch answers is pure, so the clients following
    // it run nothing.
    api.desktops = [
      desktopRecord("home", { windows: [windowRecord("win-9", "buddy", "/", { is_pinned: true, scope: "linked" })] }),
    ];
    socket.deliver().onDesktopsUpdated(api.desktops);
    expect(await store.runFreeText("buddy", "new", "hi")).toBe(true);
    expect(last(api.calls.filter((call) => call.startsWith("launch")))).toBe(
      `launch:home:buddy:new:{"message":"hi"}:window:win-9`,
    );
    expect(store.getState().desktops[0].windows[0].path).toBe("/new?message=hi");
  });

  it("shell:start-with-text runs the primary text action, and says so when there is none", async () => {
    const store = await startedStore();
    expect(await store.startWithText("hello")).toBe(false);
    expect(last(notices)).toBe("No app on this machine can start a chat");
    socket.deliver().onAppsUpdated([
      appRecord("docs", { launcher_rank: 20 }),
      appRecord("notes", {
        launcher_rank: 10,
        launch_paths: [
          launchPathRecord({ id: "new", path: "/new", params: ["message"], text_param: "message" }),
          launchPathRecord({ id: "send", path: "/send", params: ["message"], text_param: "message" }),
        ],
      }),
    ]);
    expect(await store.startWithText("hello")).toBe(true);
    expect(last(api.calls.filter((call) => call.startsWith("launch")))).toBe(
      `launch:home:notes:new:{"message":"hello"}:new`,
    );
  });

  it("shell:draft-text drafts through the first draft row when no pinned window takes one, and says so with none", async () => {
    const store = await startedStore();
    expect(await store.draftText("Explain this element:")).toBe(false);
    expect(last(notices)).toBe("No app on this machine can take a draft");
    offerApps(api, socket, [
      appRecord("docs", {
        launch_paths: [launchPathRecord({ id: "new", path: "/new", params: ["message"], text_param: "message" })],
      }),
      appRecord("notes", {
        launch_paths: [
          launchPathRecord({
            id: "draft",
            path: "/api/intake",
            method: "POST",
            params: ["message"],
            presets: { is_draft: "true" },
            draft_param: "message",
          }),
        ],
      }),
    ]);
    api.postLaunchAnswer = "/?note=1";
    expect(await store.draftText("Explain this element:")).toBe(true);
    expect(last(api.calls.filter((call) => call.startsWith("launch")))).toBe(
      `launch:home:notes:draft:{"message":"Explain this element:"}:new`,
    );
  });

  it("tells the user when the shell refuses, and about a launch path that does not exist", async () => {
    const store = await startedStore();
    api.refusal = "No registered app named 'docs'";
    expect(await store.openWindowAt("docs", "/x", "focus")).toBeNull();
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
    expect(api.calls).toContain("openWindow:home:notes:/c:focus");
  });
});

describe("windows", () => {
  it("closes for everyone and drops the placement", async () => {
    const store = await startedStore();
    await store.closeWindow("win-1");
    expect(api.calls).toContain("closeWindow:home:win-1");
    expect(activePlacements(store.getState()).map((placement) => placement.window_id)).toEqual(["win-2"]);
  });

  it("saves a gesture still waiting in the debounce before closing, so the shell's rewrite keeps it", async () => {
    api.writeLayout("home", CLIENT, {
      updated_at: null,
      placements: [placementRecord("win-1"), placementRecord("win-2")],
    });
    const store = await startedStore();
    store.minimizeWindow("win-1");
    await store.closeWindow("win-2");
    // The shell announces the layout it rewrote on the close with a save id of its own.
    socket.deliver().onPlacementsUpdated({ desktopId: "home", clientId: CLIENT, saveId: "save-shell" });
    await settle();
    await vi.advanceTimersByTimeAsync(300);
    await settle();
    const stored = api.layoutOf("home", CLIENT).placements;
    expect(stored.map((placement) => [placement.window_id, placement.is_minimized])).toEqual([["win-1", true]]);
    expect(
      activePlacements(store.getState()).map((placement) => [placement.window_id, placement.is_minimized]),
    ).toEqual([["win-1", true]]);
    expect(isLayoutDirty(store.getState())).toBe(false);
  });

  it("the close chord minimizes a pinned window instead of closing it", async () => {
    api.desktops = [
      desktopRecord("home", { windows: [windowRecord("win-1", "docs", "/", { is_pinned: true })] }),
      desktopRecord("work"),
    ];
    const store = await startedStore();
    const requested: string[] = [];
    store.setPageDriver({
      reload: () => undefined,
      reloadApp: () => undefined,
      requestClose: (id) => void requested.push(id),
    });
    await store.closeFocusedWindow();
    expect(requested).toEqual([]);
    expect(api.calls.filter((call) => call.startsWith("closeWindow"))).toEqual([]);
    expect(activeFocusedWindowId(store.getState())).toBeNull();
    expect(store.getState().desktops[0].windows.map((window) => window.id)).toEqual(["win-1"]);
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

  it("a move or resize in progress schedules no redraw; its start and end do", async () => {
    let redraws = 0;
    const store = await startedStore(() => void (redraws += 1));
    store.beginWindowMove("win-1", { x: 100, y: 60 });
    const afterBegin = redraws;
    expect(afterBegin).toBeGreaterThan(0);
    store.updateWindowMove({ x: 150, y: 90 });
    store.updateWindowMove({ x: 5, y: 400 });
    expect(store.gestureRectFor("win-1")).not.toBeNull();
    expect(store.windowRect("win-1")).toEqual(store.gestureRectFor("win-1"));
    expect(store.snapPreviewRect()).not.toBeNull();
    expect(redraws).toBe(afterBegin);
    store.endWindowMove({ x: 200, y: 200 });
    expect(redraws).toBeGreaterThan(afterBegin);
    expect(store.gestureRectFor("win-1")).toBeNull();
    expect(store.windowRect("win-1")).toEqual(store.renderedRect(placementOf(store.getState().layout, "win-1")));

    const beforeResize = redraws;
    store.beginWindowResize("win-1", "se");
    expect(redraws).toBeGreaterThan(beforeResize);
    const afterResizeBegin = redraws;
    store.updateWindowResize({ x: 20, y: 20 });
    store.updateWindowResize({ x: 40, y: 40 });
    expect(store.gestureRectFor("win-1")?.width).toBeCloseTo(640, 6);
    expect(redraws).toBe(afterResizeBegin);
    store.endWindowResize({ x: 40, y: 40 });
    expect(redraws).toBeGreaterThan(afterResizeBegin);
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

describe("presence", () => {
  it("a pushed presence set replaces the connected users and schedules a redraw", async () => {
    let redraws = 0;
    const store = await startedStore(() => void (redraws += 1));
    const before = redraws;
    expect(getPresentUsers()).toEqual([]);
    socket.deliver().onPresenceUpdated([presentUserRecord("user-bob-4471")]);
    expect(getPresentUsers().map((user) => user.user_id)).toEqual(["user-bob-4471"]);
    expect(redraws).toBe(before + 1);
    expect(store.getState().desktops).toHaveLength(2);
  });
});

describe("pinned entries", () => {
  const pinnedApp = appRecord("buddy", { pin: { path: "/", style: "avatar", scope: "linked", default_mode: "bar" } });

  async function pinnedStore(): Promise<DesktopStore> {
    api.desktops = [
      desktopRecord("home", {
        windows: [windowRecord("win-1", "docs", "/a"), windowRecord("win-9", "buddy", "/", { is_pinned: true })],
      }),
      desktopRecord("work"),
    ];
    api.clients = [clientRecord(CLIENT, { entries: { buddy: { mode: "floating", style: "plain", position: null } } })];
    const store = makeStore();
    await store.start(NO_LINK);
    socket.deliver().onAppsUpdated([appRecord("docs"), pinnedApp]);
    return store;
  }

  it("takes the record's entries and the workspace's selection again after a reconnect", async () => {
    const store = await pinnedStore();
    socket.deliver().onConnected();
    // Another window of this client moved the entry, and someone chose a design, while the socket was down.
    api.clients = [clientRecord(CLIENT, { entries: { buddy: { mode: "bar", style: "avatar", position: null } } })];
    api.avatars = { ...api.avatars, selected: "jelly-cat" };
    socket.deliver().onConnected();
    await settle();
    expect(store.getState().entries.buddy).toEqual({ mode: "bar", style: "avatar", position: null });
    expect(store.getState().avatar.design).toBe("jelly-cat");
  });

  it("an entries push that lands while the client records are read stands over the records' older answer", async () => {
    api.desktops = [desktopRecord("home", { windows: [windowRecord("win-9", "buddy", "/", { is_pinned: true })] })];
    api.clients = [clientRecord(CLIENT, { entries: { buddy: { mode: "floating", style: "plain", position: null } } })];
    const answerReads = api.holdReads();
    const store = makeStore();
    const starting = store.start(NO_LINK);
    await settle();
    socket.deliver().onClientEntriesChanged({
      clientId: CLIENT,
      entries: { buddy: { mode: "bar", style: "avatar", position: null } },
    });
    answerReads();
    await starting;
    expect(store.getState().entries).toEqual({ buddy: { mode: "bar", style: "avatar", position: null } });
  });

  it("leaves the pinned window where a close is refused", async () => {
    const store = await pinnedStore();
    await store.closeWindow("win-9");
    expect(api.calls).toContain("closeWindow:home:win-9");
    expect(store.getState().desktops[0].windows.map((window) => window.id)).toEqual(["win-1", "win-9"]);
  });

  it("the human-facing Close minimizes a pinned window and closes an ordinary one", async () => {
    const store = await pinnedStore();
    store.toggleTaskbarEntry("win-9");
    expect(placementOf(store.getState().layout, "win-9").is_minimized).toBe(false);
    await store.closeOrMinimizeWindow("win-9");
    expect(placementOf(store.getState().layout, "win-9").is_minimized).toBe(true);
    expect(api.calls.filter((call) => call.startsWith("closeWindow"))).toEqual([]);
    expect(store.getState().desktops[0].windows.map((window) => window.id)).toEqual(["win-1", "win-9"]);
    await store.closeOrMinimizeWindow("win-1");
    expect(api.calls).toContain("closeWindow:home:win-1");
  });

  it("drafts into the pinned window as a load the pages follow, not as the page's own report", async () => {
    api.desktops = [
      desktopRecord("home", {
        windows: [
          windowRecord("win-1", "docs", "/a"),
          windowRecord("win-9", "buddy", "/", { is_pinned: true, scope: "independent" }),
        ],
      }),
    ];
    const store = makeStore();
    await store.start(NO_LINK);
    const draftingApps = [
      appRecord("docs"),
      appRecord("buddy", {
        pin: { path: "/", style: "avatar", scope: "independent", default_mode: "floating" },
        launch_paths: [
          launchPathRecord({
            id: "draft",
            path: "/api/intake",
            method: "POST",
            params: ["message"],
            presets: { target: "current_chat", is_draft: "true" },
            draft_param: "message",
          }),
        ],
      }),
    ];
    offerApps(api, socket, draftingApps);
    api.postLaunchAnswer = "/?chat=agent-1";
    const loadsBefore = store.getLayoutLoadsRevision();
    expect(await store.draftIntoPinnedWindow("Draw me")).toBe(true);
    expect(last(api.calls.filter((call) => call.startsWith("launch")))).toBe(
      `launch:home:buddy:draft:{"message":"Draw me"}:window:win-9`,
    );
    // Applied as a layout load: the revision moved, so the live pages follow the stored path to the page.
    expect(store.getLayoutLoadsRevision()).toBe(loadsBefore + 1);
    expect(store.getState().layout.window_paths["win-9"]).toEqual({ path: "/?chat=agent-1", title: "" });
    // Marked as this client's own navigation for that follow, so the pages honour it even where the page just
    // reported leaving that path.
    expect(store.takeOwnNavigation()).toEqual({ windowId: "win-9", path: "/?chat=agent-1" });
    // And the window is shown.
    expect(activePlacements(store.getState()).find((placement) => placement.window_id === "win-9")?.is_minimized).toBe(
      false,
    );
    // A desktop with no pinned app taking a draft has nowhere to put one.
    socket.deliver().onAppsUpdated([appRecord("docs"), appRecord("buddy")]);
    expect(await store.draftIntoPinnedWindow("Draw me")).toBe(false);
  });

  it("navigates this client's view of a linked pinned window as the shell's own desktops record", async () => {
    const store = await pinnedStore();
    const desktopsBefore = store.getDesktopsRevision();
    const loadsBefore = store.getLayoutLoadsRevision();
    expect(await store.navigateOwnWindow("win-9", "/?doc=4")).toBe(true);
    expect(last(api.calls.filter((call) => call.startsWith("reportWindowLocation")))).toBe(
      `reportWindowLocation:home:win-9:${CLIENT}:/?doc=4:Buddy`,
    );
    // A linked window's path is the shared record's, so the answer lands as the shell's desktops record: that
    // revision moved, the layout's own paths did not.
    expect(store.getDesktopsRevision()).toBe(desktopsBefore + 1);
    expect(store.getLayoutLoadsRevision()).toBe(loadsBefore);
    expect(store.getState().desktops[0].windows.find((window) => window.id === "win-9")?.path).toBe("/?doc=4");
    expect(store.getState().layout.window_paths["win-9"]).toBeUndefined();
    // The navigation is marked as this client's own, handed over once.
    expect(store.takeOwnNavigation()).toEqual({ windowId: "win-9", path: "/?doc=4" });
    expect(store.takeOwnNavigation()).toBeNull();
    // A window the desktops do not hold is nothing to move, and a refusal is told to the user; neither leaves a
    // mark for some later follow to lift the stale-snapshot guard by.
    const callsBefore = api.calls.length;
    expect(await store.navigateOwnWindow("win-404", "/")).toBe(false);
    expect(api.calls.length).toBe(callsBefore);
    api.refusal = "no such client";
    expect(await store.navigateOwnWindow("win-9", "/?doc=5")).toBe(false);
    expect(notices).toEqual(["Could not move the window: no such client"]);
    expect(store.getDesktopsRevision()).toBe(desktopsBefore + 1);
    expect(store.takeOwnNavigation()).toBeNull();
  });

  it("restores a pinned window the client never placed at the frame the shell answered, not the cascade", async () => {
    const store = await pinnedStore();
    expect(store.windowRect("win-9")).toEqual({ x: 460, y: 40, width: 500, height: 720 });
    store.toggleTaskbarEntry("win-9");
    expect(activePlacements(store.getState()).find((placement) => placement.window_id === "win-9")).toMatchObject({
      frame: PINNED_WINDOW_FRAME,
      is_minimized: false,
    });
  });

  it("loads this client's entries with its record and takes the shell's word on a change", async () => {
    const store = await pinnedStore();
    expect(store.getState().entries).toEqual({ buddy: { mode: "floating", style: "plain", position: null } });
    socket.deliver().onClientEntriesChanged({ clientId: "other", entries: {} });
    expect(store.getState().entries.buddy.mode).toBe("floating");
    socket.deliver().onClientEntriesChanged({
      clientId: CLIENT,
      entries: { buddy: { mode: "bar", style: "avatar", position: null } },
    });
    expect(store.getState().entries).toEqual({ buddy: { mode: "bar", style: "avatar", position: null } });
  });

  it("writes one field of the presentation over the entry's current look", async () => {
    const store = await pinnedStore();
    await store.setEntryStyle("buddy", "avatar");
    expect(last(api.calls)).toBe("setEntryPresentation:client-1:buddy:floating:avatar:-");
    await store.setEntryMode("buddy", "bar");
    expect(last(api.calls)).toBe("setEntryPresentation:client-1:buddy:bar:avatar:-");
    expect(store.getState().entries.buddy).toEqual({ mode: "bar", style: "avatar", position: null });
    api.refusal = "no such client";
    await store.setEntryMode("buddy", "floating");
    expect(notices).toEqual(["Could not change the entry: no such client"]);
    expect(store.getState().entries.buddy.mode).toBe("bar");
    // An app with no pinned window on the active desktop has nothing to write.
    api.refusal = null;
    const attempted = api.calls.filter((call) => call.startsWith("setEntryPresentation")).length;
    await store.setEntryMode("docs", "floating");
    expect(api.calls.filter((call) => call.startsWith("setEntryPresentation"))).toHaveLength(attempted);
  });

  it("an entries push that lands while a write is answered stands over the answer and over a refusal's undo", async () => {
    const store = await pinnedStore();
    // Another window of this client wrote after this one; its push arrives before this write's older answer.
    const pushed = { buddy: { mode: "bar", style: "avatar", position: null }, pal: PLAIN_BAR } as const;
    const writing = store.setEntryMode("buddy", "bar");
    socket.deliver().onClientEntriesChanged({ clientId: CLIENT, entries: pushed });
    await writing;
    expect(store.getState().entries).toEqual(pushed);
    // A refused write does not put its old entry back over what a push meanwhile said of it.
    api.refusal = "no such client";
    const refused = store.setEntryMode("buddy", "floating");
    expect(store.getState().entries.buddy.mode).toBe("floating");
    socket.deliver().onClientEntriesChanged({ clientId: CLIENT, entries: { buddy: PLAIN_BAR } });
    await refused;
    expect(notices).toEqual(["Could not change the entry: no such client"]);
    expect(store.getState().entries).toEqual({ buddy: PLAIN_BAR });
  });

  it("drags a floating entry, clamped inside the backdrop, and writes its position once on release", async () => {
    const store = await pinnedStore();
    // Grabbed 10 pixels inside the box at its default corner (928, 732).
    store.beginFloatingEntryDrag("buddy", { x: 938, y: 742 }, { x: 10, y: 10 });
    expect(store.getGesture()).toMatchObject({
      kind: "floating-entry",
      app: "buddy",
      currentRect: { x: 928, y: 732 },
    });
    store.updateFloatingEntryDrag({ x: 110, y: 210 });
    expect(store.renderedFloatingEntryRect("buddy", null)).toEqual({ x: 100, y: 200, width: 56, height: 56 });
    store.updateFloatingEntryDrag({ x: 5, y: 1000 });
    expect(store.renderedFloatingEntryRect("buddy", null)).toEqual({ x: 0, y: 744, width: 56, height: 56 });
    store.endFloatingEntryDrag({ x: 110, y: 210 });
    // Landed where it was dropped before the shell has answered.
    expect(store.getGesture()).toBeNull();
    expect(store.getState().entries.buddy.position).toEqual({ x: 0.1, y: 0.25 });
    await settle();
    expect(api.calls.filter((call) => call.startsWith("setEntryPresentation"))).toEqual([
      "setEntryPresentation:client-1:buddy:floating:plain:0.1,0.25",
    ]);
    expect(store.renderedFloatingEntryRect("buddy", { x: 0.1, y: 0.25 })).toEqual({
      x: 100,
      y: 200,
      width: 56,
      height: 56,
    });
    expect(store.gestureRectFor("win-9")).toBeNull();
    // Compact mode has no floating entries to drag.
    store.setThemeMetrics(METRICS, { isCompact: true, isTouch: true });
    store.beginFloatingEntryDrag("buddy", { x: 10, y: 10 }, { x: 0, y: 0 });
    expect(store.getGesture()).toBeNull();
    // Nor does an app with no pinned window on the active desktop.
    store.setThemeMetrics(METRICS, { isCompact: false, isTouch: false });
    store.beginFloatingEntryDrag("docs", { x: 10, y: 10 }, { x: 0, y: 0 });
    expect(store.getGesture()).toBeNull();
  });
});

describe("the avatar", () => {
  it("reads the workspace's design at start and follows the status and selection pushes", async () => {
    api.avatars = { ...api.avatars, selected: "jelly-cat" };
    const store = await startedStore();
    expect(api.calls).toContain("fetchAvatars");
    expect(store.getState().avatar).toEqual({
      design: "jelly-cat",
      defaultDesign: "gummy-seal",
      status: { mood: "idle", is_stale: true },
    });
    socket.deliver().onAvatarStatus({ mood: "working", is_stale: false });
    expect(store.getState().avatar.status).toEqual({ mood: "working", is_stale: false });
    socket.deliver().onAvatarSelectionChanged("gummy-seal");
    expect(store.getState().avatar.design).toBe("gummy-seal");
  });

  it("a selection pushed while the catalog is read stands over the catalog's older answer", async () => {
    const answerReads = api.holdReads();
    const store = makeStore();
    const starting = store.start(NO_LINK);
    await settle();
    socket.deliver().onAvatarSelectionChanged("jelly-cat");
    answerReads();
    await starting;
    await settle();
    expect(store.getState().avatar).toMatchObject({ design: "jelly-cat", defaultDesign: "gummy-seal" });
  });

  it("selects a design through the shell and tells the user about a refusal", async () => {
    const store = await startedStore();
    await store.selectAvatar("jelly-cat");
    expect(last(api.calls)).toBe("selectAvatar:jelly-cat");
    // The window follows the broadcast, not the answer.
    expect(store.getState().avatar.design).toBe("gummy-seal");
    await store.selectAvatar("nobody");
    expect(notices).toEqual(["Could not change the avatar: No design nobody"]);
  });

  it("keeps the initial design when the catalog cannot be read", async () => {
    api.refusal = "down";
    const store = makeStore();
    await store.start(NO_LINK);
    api.refusal = null;
    expect(store.getState().avatar.design).toBe("gummy-seal");
  });
});

describe("desktops and shortcuts", () => {
  it("creating a desktop switches to it", async () => {
    const store = await startedStore();
    await store.createDesktop("Desktop 1", "#123456", 2);
    expect(store.getState().activeDesktopId).toBe("desktop-1");
    expect(store.getState().desktops.map((desktop) => desktop.id)).toEqual(["home", "work", "desktop-1"]);
  });

  it("adds a shortcut at the first free cell over the current grid, and not a second time", async () => {
    const store = await startedStore();
    await store.addShortcut("notes", "new", "focus");
    expect(api.calls).toContain("setDesktopShortcut:home:notes:new:0,0");
    await store.addShortcut("docs", "new", "new");
    expect(api.calls).toContain("setDesktopShortcut:home:docs:new:1,0");
    // Already on the desktop: the shell would move it and reset its mode, so nothing is posted.
    await store.addShortcut("docs", "new", "focus");
    expect(api.calls.filter((call) => call.startsWith("setDesktopShortcut"))).toHaveLength(2);
    expect(store.getState().desktops[0].shortcuts.map((shortcut) => shortcut.mode)).toEqual(["focus", "new"]);
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
    expect(api.calls).toContain("reportWindowLocation:home:win-1:client-1:/?doc=2:Plan");
    api.refusal = "critical";
    await store.setAppLifecycle("docs", "stop");
    expect(notices).toEqual(["Failed to stop docs: critical"]);
  });
});

describe("focus-chat", () => {
  /** An app that holds chats: it declares a launch path taking typed text as its ``message``, and pins a window. */
  function chatAppRecord(overrides: Partial<AppRecord> = {}): AppRecord {
    return appRecord("buddy", {
      pin: { path: "/", style: "avatar", scope: "independent", default_mode: "floating" },
      launch_paths: [
        launchPathRecord({ id: "root", path: "/" }),
        launchPathRecord({ id: "new", path: "/new", params: ["message"], text_param: "message" }),
      ],
      ...overrides,
    });
  }

  async function chatStore(apps: readonly AppRecord[] = [appRecord("docs"), chatAppRecord()]): Promise<DesktopStore> {
    const store = makeStore();
    await store.start(NO_LINK);
    socket.deliver().onAppsUpdated([...apps]);
    return store;
  }

  it("raises the window already showing the chat, switching to the desktop that holds it", async () => {
    api.desktops = [
      desktopRecord("home", {
        windows: [windowRecord("win-9", "buddy", "/", { is_pinned: true, scope: "independent" })],
      }),
      desktopRecord("work", { windows: [windowRecord("win-4", "buddy", "/chat-7")] }),
    ];
    const store = await chatStore();
    expect(store.getState().activeDesktopId).toBe("home");
    expect(await store.focusChat("chat-7")).toBe(true);
    // The chat is already on screen somewhere: it is switched to, and nothing is opened or moved.
    expect(store.getState().activeDesktopId).toBe("work");
    expect(api.calls.filter((call) => call.startsWith("openWindow"))).toEqual([]);
    expect(api.calls.filter((call) => call.startsWith("reportWindowLocation"))).toEqual([]);
    expect(placementOf(store.getState().layout, "win-4").is_minimized).toBe(false);
  });

  it("reads a subagent view of the chat as showing it", async () => {
    api.desktops = [
      desktopRecord("home", {
        windows: [
          windowRecord("win-9", "buddy", "/", { is_pinned: true, scope: "independent" }),
          windowRecord("win-4", "buddy", "/chat-7.agent-2.sess-3"),
        ],
      }),
    ];
    const store = await chatStore();
    expect(await store.focusChat("chat-7")).toBe(true);
    expect(api.calls.filter((call) => call.startsWith("reportWindowLocation"))).toEqual([]);
  });

  it("points this client's pinned chat window at a chat nothing is showing", async () => {
    api.desktops = [
      desktopRecord("home", {
        windows: [
          windowRecord("win-1", "docs", "/a"),
          windowRecord("win-9", "buddy", "/", { is_pinned: true, scope: "independent" }),
        ],
      }),
    ];
    const store = await chatStore();
    expect(await store.focusChat("chat-7")).toBe(true);
    expect(last(api.calls.filter((call) => call.startsWith("reportWindowLocation")))).toBe(
      `reportWindowLocation:home:win-9:${CLIENT}:/chat-7:Buddy`,
    );
    // The chat lands where this viewer reads chats, shown, and as this client's own navigation for the follow.
    expect(store.getState().layout.window_paths["win-9"]?.path).toBe("/chat-7");
    expect(placementOf(store.getState().layout, "win-9").is_minimized).toBe(false);
    expect(store.takeOwnNavigation()).toEqual({ windowId: "win-9", path: "/chat-7" });
    // And a second ask for the chat it now shows moves nothing.
    const callsBefore = api.calls.length;
    expect(await store.focusChat("chat-7")).toBe(true);
    expect(api.calls.length).toBe(callsBefore);
  });

  it("opens the chat in a window of its own when no pinned window takes it", async () => {
    api.desktops = [desktopRecord("home", { windows: [windowRecord("win-1", "docs", "/a")] })];
    const store = await chatStore([appRecord("docs"), chatAppRecord({ pin: null })]);
    expect(await store.focusChat("chat-7")).toBe(true);
    expect(api.calls.filter((call) => call.startsWith("openWindow"))).toEqual(["openWindow:home:buddy:/chat-7:focus"]);
  });

  it("answers false when no app on this machine holds chats", async () => {
    api.desktops = [desktopRecord("home", { windows: [windowRecord("win-1", "docs", "/a")] })];
    const store = await chatStore([appRecord("docs")]);
    expect(await store.focusChat("chat-7")).toBe(false);
    expect(api.calls.filter((call) => call.startsWith("openWindow"))).toEqual([]);
  });
});

describe("pulled-out windows", () => {
  /** A recorder for the shell's side of the pull-out conversation. */
  function popOutRecorder(): {
    bridge: import("./DesktopStore").PopOutBridge;
    calls: unknown[];
    reports: unknown[];
  } {
    const calls: unknown[] = [];
    const reports: unknown[] = [];
    return {
      calls,
      reports,
      bridge: {
        requestPopOut: (request) => calls.push(["request", request]),
        cancelPopOut: (windowId) => calls.push(["cancel", windowId]),
        endPopOut: (windowId) => calls.push(["end", windowId]),
        reportDetachedWindows: (windows) => reports.push(windows),
      },
    };
  }

  function makePopOutStore(options: { soloWindowId?: string | null } = {}): {
    store: DesktopStore;
    calls: unknown[];
    reports: unknown[];
  } {
    const recorder = popOutRecorder();
    const store = new DesktopStore({
      clientId: CLIENT,
      api,
      socket,
      metrics: METRICS,
      modes: MODES,
      redraw: () => undefined,
      notify: (message) => void notices.push(message),
      reloadInterface: () => void (reloads += 1),
      popOut: recorder.bridge,
      soloWindowId: options.soloWindowId ?? null,
    });
    store.setBackdropSize({ width: 1000, height: 800 });
    return { store, calls: recorder.calls, reports: recorder.reports };
  }

  const savedCalls = (): string[] => api.calls.filter((call) => call.startsWith("savePlacements"));

  it("pulls a dragged window out past the viewport, drops it again inside, and detaches it on release", async () => {
    const { store, calls } = makePopOutStore();
    await store.start(NO_LINK);
    store.setCanPopOut(true);
    store.beginWindowMove("win-1", { x: 100, y: 60 });
    // Past the right edge, but not yet by the tear-out distance: still an ordinary drag with a snap zone.
    store.updateWindowMove({ x: 1010, y: 300 });
    expect(store.getGesture()).toMatchObject({ isTearingOut: false, zone: "SNAPPED_RIGHT" });
    expect(calls).toEqual([]);
    // Past the edge by the distance: the chrome is asked for a window the size this one renders, held where
    // the pointer holds it, and no zone is offered meanwhile.
    store.updateWindowMove({ x: 1060, y: 300 });
    expect(store.getGesture()).toMatchObject({ isTearingOut: true, zone: null });
    expect(store.isTearingOut("win-1")).toBe(true);
    expect(calls).toHaveLength(1);
    expect(calls[0]).toMatchObject(["request", { windowId: "win-1", mode: "drag", title: "Docs" }]);
    const request = (calls[0] as [string, { width: number; height: number; grabX: number; grabY: number }])[1];
    expect(request.width).toBe(600);
    expect(request.height).toBe(560);
    expect(request.grabX).toBeLessThanOrEqual(request.width);
    expect(request.grabY).toBeLessThanOrEqual(request.height);
    // Back inside: the chrome drops its window and this one shows again.
    store.updateWindowMove({ x: 900, y: 300 });
    expect(store.getGesture()).toMatchObject({ isTearingOut: false });
    expect(calls[1]).toEqual(["cancel", "win-1"]);
    // Out again and released: the window is detached where it stood, its frame untouched, and saved at once.
    store.updateWindowMove({ x: 1060, y: 300 });
    store.endWindowMove({ x: 1060, y: 300 });
    expect(calls[3]).toEqual(["end", "win-1"]);
    const placement = placementOf(store.getState().layout, "win-1");
    expect(placement).toMatchObject({ is_detached: true, is_minimized: false, frame: cascadeFrame(0) });
    // Focus skips the pulled-out window (and the other one, which the client never placed, is minimized).
    expect(activeFocusedWindowId(store.getState())).toBeNull();
    await settle();
    expect(savedCalls()).toHaveLength(1);
    expect(api.layoutOf("home", CLIENT).placements.find((p) => p.window_id === "win-1")?.is_detached).toBe(true);
  });

  it("never pulls out until the chrome says it can, and cancels a tear-out with the gesture", async () => {
    const { store, calls } = makePopOutStore();
    await store.start(NO_LINK);
    store.beginWindowMove("win-1", { x: 100, y: 60 });
    store.updateWindowMove({ x: 1060, y: 300 });
    expect(store.getGesture()).toMatchObject({ isTearingOut: false, zone: "SNAPPED_RIGHT" });
    expect(calls).toEqual([]);
    store.endWindowMove({ x: 1060, y: 300 });
    expect(placementOf(store.getState().layout, "win-1").is_detached).toBe(false);
    store.setCanPopOut(true);
    store.beginWindowMove("win-1", { x: 100, y: 60 });
    // Above the top edge counts too: past the chrome's own bar.
    store.updateWindowMove({ x: 300, y: -50 });
    expect(store.getGesture()).toMatchObject({ isTearingOut: true });
    store.cancelGesture();
    expect(calls.map((call) => (call as unknown[])[0])).toEqual(["request", "cancel"]);
    expect(store.getGesture()).toBeNull();
    expect(placementOf(store.getState().layout, "win-1").is_detached).toBe(false);
  });

  it("opens a window in its own desktop window from the menu, shows it again, and brings it back", async () => {
    const { store, calls } = makePopOutStore();
    await store.start(NO_LINK);
    // Refused until the chrome can.
    await store.detachWindow("win-1");
    expect(calls).toEqual([]);
    store.setCanPopOut(true);
    await store.detachWindow("win-1");
    expect(calls[0]).toMatchObject(["request", { windowId: "win-1", mode: "open", grabX: 0, grabY: 0 }]);
    expect(placementOf(store.getState().layout, "win-1").is_detached).toBe(true);
    expect(savedCalls()).toHaveLength(1);
    // The taskbar entry of a pulled-out window shows its own window rather than restoring it here.
    store.toggleTaskbarEntry("win-1");
    expect(calls[1]).toMatchObject(["request", { windowId: "win-1", mode: "open" }]);
    expect(placementOf(store.getState().layout, "win-1").is_detached).toBe(true);
    // Back where a drop back onto the desktop named, on top, saved at once.
    await store.reattachWindow("win-1", { x: 0.2, y: 0.2, width: 0.5, height: 0.5 });
    const placement = last(activePlacements(store.getState()));
    expect(placement).toMatchObject({
      window_id: "win-1",
      is_detached: false,
      frame: { x: 0.2, y: 0.2, width: 0.5, height: 0.5 },
    });
    expect(savedCalls()).toHaveLength(2);
  });

  it("reports the active desktop's pulled-out windows with their titles whenever the set changes", async () => {
    const { store, reports } = makePopOutStore();
    await store.start(NO_LINK);
    // Reported once the layout is known (empty), and not again for redraws that change nothing.
    expect(reports).toEqual([[]]);
    store.raiseWindow("win-2");
    expect(reports).toHaveLength(1);
    store.setCanPopOut(true);
    await store.detachWindow("win-1");
    expect(reports).toHaveLength(2);
    expect(reports[1]).toEqual([{ windowId: "win-1", title: "Docs" }]);
    // A new title for a pulled-out window is a change worth reporting.
    socket.deliver().onDesktopsUpdated([
      desktopRecord("home", {
        windows: [windowRecord("win-1", "docs", "/a", { title: "Plan" }), windowRecord("win-2", "notes", "/b")],
      }),
      desktopRecord("work"),
    ]);
    expect(reports[2]).toEqual([{ windowId: "win-1", title: "Plan" }]);
    await store.reattachWindow("win-1", null);
    expect(reports[3]).toEqual([]);
  });

  it("a solo shell lands on its window's desktop without moving the client, and leaves the arrangement alone", async () => {
    api.clients = [clientRecord(CLIENT, { active_desktop: "home" })];
    api.desktops = [
      desktopRecord("home", { windows: [windowRecord("win-1", "docs", "/a")] }),
      desktopRecord("work", { windows: [windowRecord("win-5", "notes", "/n")] }),
    ];
    api.writeLayout("work", CLIENT, {
      updated_at: null,
      placements: [placementRecord("win-5", { is_detached: true })],
    });
    const { store, reports } = makePopOutStore({ soloWindowId: "win-5" });
    await store.start(NO_LINK);
    expect(store.getSoloWindowId()).toBe("win-5");
    expect(store.getState().activeDesktopId).toBe("work");
    // The client's own desktop is never reported from here: it belongs to the main window.
    expect(socket.reports).toEqual([]);
    expect(reports).toEqual([[{ windowId: "win-5", title: "Notes" }]]);
    // The page's own focus report, a chat ask, the close chord: none of them rearrange anything.
    store.raiseWindow("win-5");
    expect(placementOf(store.getState().layout, "win-5").is_detached).toBe(true);
    expect(isLayoutDirty(store.getState())).toBe(false);
    expect(await store.focusChat("chat-1")).toBe(false);
    await store.closeFocusedWindow();
    expect(api.calls.filter((call) => call.startsWith("closeWindow"))).toEqual([]);
    // A push moving the client to another desktop is the main window's business.
    socket.deliver().onActiveDesktopChanged({ clientId: CLIENT, desktopId: "home" });
    await settle();
    expect(store.getState().activeDesktopId).toBe("work");
    // Its own window's return is the one arrangement it writes, at once.
    await store.reattachWindow("win-5", null);
    expect(placementOf(store.getState().layout, "win-5").is_detached).toBe(false);
    expect(savedCalls()).toHaveLength(1);
    expect(reports[reports.length - 1]).toEqual([]);
  });

  it("a solo shell whose first layout does not say its window is out takes its own existence as the truth", async () => {
    api.desktops = [desktopRecord("home", { windows: [windowRecord("win-1", "docs", "/a")] })];
    api.writeLayout("home", CLIENT, { updated_at: null, placements: [placementRecord("win-1")] });
    const { store, reports } = makePopOutStore({ soloWindowId: "win-1" });
    await store.start(NO_LINK);
    expect(placementOf(store.getState().layout, "win-1").is_detached).toBe(true);
    await settle();
    expect(savedCalls()).toHaveLength(1);
    expect(reports[reports.length - 1]).toEqual([{ windowId: "win-1", title: "Docs" }]);
    // A later layout saying the window is back is the desktop's word: reported as such, not re-detached.
    api.writeLayout("home", CLIENT, { updated_at: null, placements: [placementRecord("win-1")] });
    socket.deliver().onPlacementsUpdated({ desktopId: "home", clientId: CLIENT, saveId: "save-elsewhere" });
    await settle();
    expect(placementOf(store.getState().layout, "win-1").is_detached).toBe(false);
    expect(reports[reports.length - 1]).toEqual([]);
  });
});
