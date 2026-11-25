// @vitest-environment jsdom
/**
 * The live-page layer against a real store over the fake shell: pages are created for the shown
 * windows of the active desktop, laid over their windows' content boxes in the interleaved
 * stacking order, interactive (inert unless focused on a touch client), hidden when minimized,
 * parked while the windows in front cover them, destroyed when closed; they are greeted after
 * every load, told shown and hidden, and follow their windows' stored paths in place or by
 * reload; their own ``shell:location``, ``shell:focused``, ``shell:open``,
 * ``shell:start-with-text``, and ``shell:report-bug`` reach the store; and the window whose page
 * the document's focus lands in is raised.
 */
import "../testing/dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  SHELL_CAPABILITIES,
  SHELL_CLOSE_REQUEST,
  SHELL_DRAFT_TEXT,
  SHELL_FOCUSED,
  SHELL_HANDSHAKE,
  SHELL_HIDDEN,
  SHELL_INPUT,
  SHELL_LOCATION,
  SHELL_NAVIGATE,
  SHELL_MESSAGE,
  SHELL_OPEN,
  SHELL_OPEN_LINK,
  SHELL_REPORT_BUG,
  SHELL_SHOWN,
  SHELL_SHOW_COACH_CARD,
  SHELL_AGENT_WORKING,
  SHELL_FIRST_CONVERSATION_STEP,
  SHELL_THEME,
  SHELL_START_WITH_TEXT,
} from "@imbue/workspace-ui/src/app_contract";
import type { ReportedFailure } from "@imbue/workspace-ui/src/app_contract";
import type { ShellCoachCardPlace } from "@imbue/workspace-ui/src/app_contract";
import { BOTTOM_RIGHT_QUARTER_FRAME } from "../geometry/frames";
import { initEmbedderRelay, resetEmbedderRelayForTesting } from "../relay";
import { pendingInputReportsForTesting, resetPresenceForTesting, setOwnIdentityForTesting } from "../model/Presence";
import { resetThemeClientForTests, wearTheme } from "@imbue/workspace-ui/src/themes/themeClient";
import type { Placement } from "../model/records";
import { activeFocusedWindowId, activePlacements } from "../reducers/desktopState";
import { DesktopStore } from "../store/DesktopStore";
import { FakeDesktopApi, FakeDesktopSocket, offerApps, settle } from "../testing/fakeShell";
import {
  appRecord,
  desktopRecord,
  launchPathRecord,
  placementRecord,
  themeMetricsRecord,
  windowRecord,
} from "../testing/records";
import {
  LivePagesLayer,
  DESKTOP_ROOT_ATTRIBUTE,
  FOCUS_WATCH_INTERVAL_MS,
  PAGE_FRAME_ALLOW,
  SHELL_PROMPT_SETTLE_TICKS,
  WATCHER_RAISE_HOLD_TICKS,
  pageFrameAllow,
} from "./livePages";
import type { LivePagesOptions } from "./livePages";

const METRICS = themeMetricsRecord();
const CLIENT = "client-1";
const NO_LINK = { desktopId: null, open: null, launch: null };
// Docs is built in; Notes is a user's own app, whose pages never offer Report a bug.
const docs = appRecord("docs", { url: "http://127.0.0.1:7001", system: true });
const notes = appRecord("notes", { url: "http://127.0.0.1:7002" });

let api: FakeDesktopApi;
let socket: FakeDesktopSocket;
let store: DesktopStore;
let backdrop: HTMLElement;
let host: HTMLElement;
let windows: HTMLElement;
let overviewTiles: HTMLElement;
let layer: LivePagesLayer;
let bugReportsOpened: (ReportedFailure | null)[];
/** The coach cards the pages asked the layer for, as a text and the place each names. */
let coachCards: [string, ShellCoachCardPlace][] = [];
/** What the layers said, each time they said it, of whether any page has an agent working in it. */
let pagesAgentWorking: boolean[] = [];
/** What the layers said, each time they said it, of whether any page says the workspace's first conversation is
 *  still following its script, and whether a page's own report brought the answer. */
let pagesFirstConversation: { isPending: boolean; isSaidByPage: boolean }[] = [];
const PAGE_OPTIONS: LivePagesOptions = {
  host: "127.0.0.1:8000",
  protocol: "http:",
  showCoachCard: (text, place) => coachCards.push([text, place]),
  notePagesAgentWorking: (isWorking) => pagesAgentWorking.push(isWorking),
  notePagesFirstConversationPending: (isPending, isSaidByPage) =>
    pagesFirstConversation.push({ isPending, isSaidByPage }),
};
/** Longer than the schedule the shell posts a raised window's page its focus grants over. */
const FOCUS_GRANT_SCHEDULE_MS = 2000;

/** Let the watcher run ``count`` ticks, and every other timer due by then. */
function tick(count = 1): void {
  vi.advanceTimersByTime(count * FOCUS_WATCH_INTERVAL_MS);
}

/** Let the shell post every focus grant it has pending, then the watcher run the ticks that put a landing past them. */
function pastThePromptSettle(): void {
  vi.advanceTimersByTime(FOCUS_GRANT_SCHEDULE_MS);
  tick(SHELL_PROMPT_SETTLE_TICKS + 1);
}

/** Stand in for the window chrome the views render: one content box per shown window at a fixed spot. */
function renderChrome(...extraWindowIds: string[]): void {
  windows.innerHTML = "";
  for (const windowId of ["win-1", "win-2", "win-3", ...extraWindowIds]) {
    const chrome = document.createElement("div");
    chrome.setAttribute("data-window-id", windowId);
    const content = document.createElement("div");
    content.setAttribute("data-window-content", "");
    content.getBoundingClientRect = () => ({ left: 100, top: 60, width: 500, height: 400 }) as DOMRect;
    chrome.appendChild(content);
    windows.appendChild(chrome);
  }
}

function frameOf(windowId: string): HTMLIFrameElement {
  const frame = host.querySelector<HTMLIFrameElement>(`iframe[data-live-page="${windowId}"]`);
  if (frame === null) throw new Error(`no page for ${windowId}`);
  return frame;
}

function wrapperOf(windowId: string): HTMLElement {
  return frameOf(windowId).parentElement as HTMLElement;
}

/** Whether a page is parked out of the browser's rendering: moved out of the viewport and invisible. */
function isParked(wrapper: HTMLElement): boolean {
  return wrapper.style.visibility === "hidden" && wrapper.style.transform !== "";
}

/** Draw a window's chrome at ``rect`` as the DOM would measure it now (a window still travelling included). */
function drawChromeAt(windowId: string, rect: { left: number; top: number; width: number; height: number }): void {
  const chrome = windows.querySelector(`[data-window-id="${windowId}"]`) as HTMLElement;
  chrome.getBoundingClientRect = () => rect as DOMRect;
}

/** The messages a page's window received, by type. */
function spyOnFrame(windowId: string): ReturnType<typeof vi.fn> {
  const frame = frameOf(windowId);
  const spy = vi.fn();
  const target = frame.contentWindow as Window;
  target.postMessage = spy as unknown as Window["postMessage"];
  return spy;
}

/** The ``ttyd-focus`` messages ``spy`` (from ``spyOnFrame``) has seen. */
function grantsSeen(spy: ReturnType<typeof vi.fn>): number {
  return spy.mock.calls.filter(([message]) => (message as { type?: string }).type === "ttyd-focus").length;
}

/** What the layers last said of whether any page has an agent working in it. */
function lastToldOfPages(): boolean | undefined {
  return pagesAgentWorking[pagesAgentWorking.length - 1];
}

function lastToldOfFirstConversation(): { isPending: boolean; isSaidByPage: boolean } | undefined {
  return pagesFirstConversation[pagesFirstConversation.length - 1];
}

function load(windowId: string): void {
  frameOf(windowId).dispatchEvent(new Event("load"));
}

/** Every URL the frame is pointed at from now on. */
function spyOnSrc(windowId: string): string[] {
  const urls: string[] = [];
  const frame = frameOf(windowId);
  frame.setAttribute = ((name: string, value: string) => {
    if (name === "src") urls.push(value);
    HTMLElement.prototype.setAttribute.call(frame, name, value);
  }) as typeof frame.setAttribute;
  return urls;
}

/** The page inside ``windowId``'s frame moves to ``path`` itself and reports it; the shell stores it and says so. */
async function navigateInPage(windowId: string, path: string): Promise<void> {
  load(windowId);
  messageFromPage(windowId, { type: SHELL_LOCATION, path, title: "" });
  await settle();
  socket.deliver().onDesktopsUpdated(api.desktops);
  layer.reconcile();
}

/** A message from the page inside ``windowId``'s frame, as the relay sees it. */
function messageFromPage(windowId: string, data: Record<string, unknown>): void {
  window.dispatchEvent(
    new MessageEvent("message", { data, source: frameOf(windowId).contentWindow, origin: window.location.origin }),
  );
}

/** An independent pinned ``win-4`` of the docs app on the home desktop, this client's page of it at ``/?doc=7``
 *  ("Seven"), placed by ``placements`` in this client's layout, shown with its chrome; answers its frame. */
async function showIndependentWindow(placements: Placement[]): Promise<HTMLIFrameElement> {
  const [home, work] = api.desktops;
  const independent = windowRecord("win-4", "docs", "/", { is_pinned: true, scope: "independent" });
  api.desktops = [{ ...home, windows: [...home.windows, independent] }, work];
  api.windowPaths.set(`${CLIENT}/win-4`, { path: "/?doc=7", title: "Seven" });
  api.writeLayout("home", CLIENT, { updated_at: null, placements });
  socket.deliver().onDesktopsUpdated(api.desktops);
  socket.deliver().onPlacementsUpdated({ desktopId: "home", clientId: CLIENT, saveId: "save-shell" });
  await settle();
  renderChrome("win-4");
  layer.reconcile();
  return frameOf("win-4");
}

/** This client views Alice's desktop, whose windows ``placements`` place on her 2000x1600 backdrop: drawn in a
 *  letterbox at half scale on this 1000x800 one. */
async function viewAlicesDesktopAtHalfScale(placements: Placement[]): Promise<void> {
  api.desktops = [
    ...api.desktops,
    desktopRecord("alice", {
      owner: "user:user-alice-0001",
      windows: placements.map((placement) => windowRecord(placement.window_id, "docs", `/${placement.window_id}`)),
    }),
  ];
  setOwnIdentityForTesting({
    owner: true,
    user_id: "user-josh-0001",
    email: "josh@example.com",
    participant: "user:user-josh-0001",
  });
  api.ownerViewport = { width: 2000, height: 1600 };
  api.writeLayout("alice", api.ownerLayoutClientId, { updated_at: "2026-10-29T00:00:00Z", placements });
  socket.deliver().onDesktopsUpdated(api.desktops);
  await settle();
  await store.switchDesktop("alice");
}

beforeEach(async () => {
  api = new FakeDesktopApi();
  socket = new FakeDesktopSocket();
  api.desktops = [
    desktopRecord("home", {
      windows: [
        windowRecord("win-1", "docs", "/?doc=1"),
        windowRecord("win-2", "notes", "/b"),
        windowRecord("win-3", "docs", "/new"),
      ],
    }),
    desktopRecord("work"),
  ];
  api.writeLayout("home", CLIENT, {
    updated_at: null,
    placements: [placementRecord("win-2", { is_minimized: true }), placementRecord("win-1")],
  });
  store = new DesktopStore({
    clientId: CLIENT,
    api,
    socket,
    metrics: METRICS,
    modes: { isPhone: false, isTouch: false },
    redraw: () => undefined,
    reloadInterface: () => undefined,
    reportBug: (failure) => void bugReportsOpened.push(failure),
  });
  bugReportsOpened = [];
  store.setBackdropSize({ width: 1000, height: 800 });
  backdrop = document.createElement("div");
  backdrop.getBoundingClientRect = () => ({ left: 0, top: 0, width: 1000, height: 800 }) as DOMRect;
  host = document.createElement("div");
  host.getBoundingClientRect = () => ({ left: 0, top: 0, width: 1000, height: 800 }) as DOMRect;
  windows = document.createElement("div");
  // As the Backdrop nests them: the pages host and the windows layer inside the desktop's stage, the overview's
  // tiles beside the stage under the backdrop's root.
  backdrop.setAttribute(DESKTOP_ROOT_ATTRIBUTE, "home");
  const stage = document.createElement("div");
  stage.append(host, windows);
  overviewTiles = document.createElement("div");
  backdrop.append(stage, overviewTiles);
  document.body.appendChild(backdrop);
  resetEmbedderRelayForTesting();
  initEmbedderRelay();
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
  coachCards = [];
  pagesAgentWorking = [];
  pagesFirstConversation = [];
  layer = new LivePagesLayer(host, store, PAGE_OPTIONS);
  layer.start();
  await store.start(NO_LINK);
  socket.deliver().onAppsUpdated([docs, notes]);
  renderChrome();
  layer.reconcile();
});

afterEach(() => {
  vi.useRealTimers();
  resetEmbedderRelayForTesting();
  resetThemeClientForTests();
  resetPresenceForTesting();
  backdrop.remove();
});

describe("creating and positioning", () => {
  it("creates a page for each shown window at its app's origin plus its path, over its content box", () => {
    const frame = frameOf("win-1");
    expect(frame.getAttribute("src")).toBe("http://127.0.0.1:7001/?doc=1");
    expect(frame.getAttribute("sandbox")).toContain("allow-same-origin");
    // The Screen's viewer locks the pointer and the chrome's immersive mode goes fullscreen through this frame.
    expect(frame.getAttribute("sandbox")).toContain("allow-pointer-lock");
    expect(frame.getAttribute("allow")).toBe("clipboard-read; clipboard-write; fullscreen");
    const wrapper = wrapperOf("win-1");
    expect(wrapper.style.left).toBe("100px");
    expect(wrapper.style.top).toBe("60px");
    expect(wrapper.style.width).toBe("500px");
    expect(isParked(wrapper)).toBe(false);
    // Placements: win-3 (missing, minimized) at 0, win-2 minimized at 1, win-1 focused at 2.
    expect(wrapper.style.zIndex).toBe("5");
    expect(wrapper.style.pointerEvents).toBe("auto");
  });

  it("hands the microphone, and the screen wake lock dictation holds, to the frames of an app whose manifest listens, and to no other", () => {
    expect(pageFrameAllow(appRecord("chat", { uses_microphone: true }))).toBe(
      `${PAGE_FRAME_ALLOW}; microphone; screen-wake-lock`,
    );
    expect(pageFrameAllow(appRecord("docs"))).toBe(PAGE_FRAME_ALLOW);
    expect(frameOf("win-1").getAttribute("allow")).not.toContain("microphone");
    expect(frameOf("win-1").getAttribute("allow")).not.toContain("screen-wake-lock");
  });

  it("creates no page for a minimized window until it is restored", () => {
    expect(layer.hasPage("win-2")).toBe(false);
    expect(layer.hasPage("win-3")).toBe(false);
    store.restoreWindow("win-2");
    layer.reconcile();
    expect(layer.hasPage("win-2")).toBe(true);
    store.restoreWindow("win-3");
    layer.reconcile();
    expect(layer.hasPage("win-3")).toBe(true);
  });

  it("creates the page of a window the shell placed in this client's layout (an agent's open)", async () => {
    api.writeLayout("home", CLIENT, {
      updated_at: null,
      placements: [
        placementRecord("win-2", { is_minimized: true }),
        placementRecord("win-1"),
        placementRecord("win-3"),
      ],
    });
    socket.deliver().onPlacementsUpdated({ desktopId: "home", clientId: CLIENT, saveId: "save-shell" });
    await settle();
    renderChrome();
    layer.reconcile();
    expect(layer.hasPage("win-3")).toBe(true);
    expect(frameOf("win-3").getAttribute("src")).toBe("http://127.0.0.1:7001/new");
  });

  it("places one page over its content box as it stands, keeping its stacking and interactivity", () => {
    const content = windows.querySelector('[data-window-id="win-1"] [data-window-content]') as HTMLElement;
    content.getBoundingClientRect = () => ({ left: 250, top: 90, width: 640, height: 480 }) as DOMRect;
    layer.placePage("win-1");
    const wrapper = wrapperOf("win-1");
    expect([wrapper.style.left, wrapper.style.top, wrapper.style.width, wrapper.style.height]).toEqual([
      "250px",
      "90px",
      "640px",
      "480px",
    ]);
    expect(wrapper.style.zIndex).toBe("5");
    expect(wrapper.style.pointerEvents).toBe("auto");
    // A hidden page and an unknown window are left alone.
    store.minimizeWindow("win-1");
    layer.reconcile();
    content.getBoundingClientRect = () => ({ left: 1, top: 2, width: 3, height: 4 }) as DOMRect;
    layer.placePage("win-1");
    layer.placePage("win-9");
    expect(isParked(wrapper)).toBe(true);
    expect(wrapper.style.left).toBe("250px");
  });

  it("hides a page while its window is being pulled out, and while it is out, and shows it again when back", async () => {
    const wrapper = wrapperOf("win-1");
    expect(isParked(wrapper)).toBe(false);
    // The drag past the viewport: the chrome draws the window under the cursor, so no page here meanwhile.
    layer.setTornOutWindow("win-1");
    expect(isParked(wrapper)).toBe(true);
    layer.setTornOutWindow(null);
    expect(isParked(wrapper)).toBe(false);
    // Pulled out: the page lives in the chrome's own desktop window, and this one keeps its frame for the return.
    api.writeLayout("home", CLIENT, {
      updated_at: null,
      placements: [placementRecord("win-1", { is_detached: true })],
    });
    socket.deliver().onPlacementsUpdated({ desktopId: "home", clientId: CLIENT, saveId: "save-elsewhere" });
    await settle();
    layer.reconcile();
    expect(isParked(wrapper)).toBe(true);
    await store.reattachWindow("win-1", null);
    renderChrome();
    layer.reconcile();
    expect(wrapperOf("win-1")).toBe(wrapper);
    expect(isParked(wrapper)).toBe(false);
  });

  it("keeps every page interactive, the unfocused ones too, and makes all of them inert during a gesture", () => {
    store.restoreWindow("win-2");
    layer.reconcile();
    expect(wrapperOf("win-2").style.pointerEvents).toBe("auto");
    expect(wrapperOf("win-1").style.pointerEvents).toBe("auto");
    layer.setGestureActive(true);
    expect(wrapperOf("win-2").style.pointerEvents).toBe("none");
    expect(wrapperOf("win-1").style.pointerEvents).toBe("none");
    layer.setGestureActive(false);
    expect(wrapperOf("win-2").style.pointerEvents).toBe("auto");
    expect(wrapperOf("win-1").style.pointerEvents).toBe("auto");
  });

  it("makes every page but the focused one inert on a touch client, and all of them during a gesture", () => {
    store.setThemeMetrics(METRICS, { isPhone: false, isTouch: true });
    store.restoreWindow("win-2");
    layer.reconcile();
    expect(wrapperOf("win-2").style.pointerEvents).toBe("auto");
    expect(wrapperOf("win-1").style.pointerEvents).toBe("none");
    layer.setGestureActive(true);
    expect(wrapperOf("win-2").style.pointerEvents).toBe("none");
    layer.setGestureActive(false);
    expect(wrapperOf("win-2").style.pointerEvents).toBe("auto");
    expect(wrapperOf("win-1").style.pointerEvents).toBe("none");
  });

  it("makes every page inert while this window views somebody else's desktop, the focused one included", async () => {
    api.desktops = [
      ...api.desktops,
      desktopRecord("alice", {
        owner: "user:user-alice-0001",
        windows: [windowRecord("win-a", "docs", "/a")],
      }),
    ];
    setOwnIdentityForTesting({
      owner: true,
      user_id: "user-josh-0001",
      email: "josh@example.com",
      participant: "user:user-josh-0001",
    });
    api.writeLayout("alice", api.ownerLayoutClientId, {
      updated_at: "2026-10-29T00:00:00Z",
      placements: [placementRecord("win-a")],
    });
    socket.deliver().onDesktopsUpdated(api.desktops);
    await settle();

    // The owner's layout focuses win-a, and even the focused page takes no press on a viewed desktop (8.3).
    await store.switchDesktop("alice");
    renderChrome("win-a");
    layer.reconcile();
    expect(activeFocusedWindowId(store.getState())).toBe("win-a");
    expect(wrapperOf("win-a").style.pointerEvents).toBe("none");
    // The page is told it is viewed, so a terminal attaches to its session as a viewer.
    const spy = spyOnFrame("win-a");
    load("win-a");
    expect(spy.mock.calls[0][0]).toMatchObject({ type: SHELL_HANDSHAKE, desktopId: "alice", isViewing: true });

    // Back on an own desktop, the focused page is interactive again.
    await store.stopViewing();
    renderChrome();
    layer.reconcile();
    expect(wrapperOf("win-1").style.pointerEvents).toBe("auto");
  });

  it("raises nothing for a viewed page's focus, and greets a page again on its capabilities, viewed or not", async () => {
    api.desktops = [
      ...api.desktops,
      desktopRecord("alice", {
        owner: "user:user-alice-0001",
        windows: [windowRecord("win-a", "docs", "/a"), windowRecord("win-b", "docs", "/b")],
      }),
    ];
    setOwnIdentityForTesting({
      owner: true,
      user_id: "user-josh-0001",
      email: "josh@example.com",
      participant: "user:user-josh-0001",
    });
    api.writeLayout("alice", api.ownerLayoutClientId, {
      updated_at: "2026-10-29T00:00:00Z",
      placements: [placementRecord("win-a"), placementRecord("win-b")],
    });
    socket.deliver().onDesktopsUpdated(api.desktops);
    await settle();
    await store.switchDesktop("alice");
    renderChrome("win-a", "win-b");
    layer.reconcile();
    expect(activeFocusedWindowId(store.getState())).toBe("win-b");

    // The page under win-a focuses itself (a composer on load): the owner's stack is not this window's to change,
    // and nothing is said about it.
    messageFromPage("win-a", { type: SHELL_FOCUSED });
    expect(activeFocusedWindowId(store.getState())).toBe("win-b");
    expect(store.toasts.current()).toEqual([]);

    // The page's contract module is up before its frame has loaded: it is greeted at once, viewed, so a terminal
    // attaches as a viewer from its first attach.
    const spy = spyOnFrame("win-a");
    messageFromPage("win-a", { type: SHELL_CAPABILITIES, navigation: true });
    expect(spy.mock.calls.map((call) => call[0])).toMatchObject([
      { type: SHELL_HANDSHAKE, windowId: "win-a", desktopId: "alice", isViewing: true },
      { type: SHELL_THEME },
    ]);

    // Back on an own desktop, a page's focus raises its window again, and its capabilities greet it unviewed.
    await store.stopViewing();
    renderChrome();
    layer.reconcile();
    store.restoreWindow("win-2");
    layer.reconcile();
    expect(activeFocusedWindowId(store.getState())).toBe("win-2");
    messageFromPage("win-1", { type: SHELL_FOCUSED });
    expect(activeFocusedWindowId(store.getState())).toBe("win-1");
    const ownSpy = spyOnFrame("win-1");
    messageFromPage("win-1", { type: SHELL_CAPABILITIES, navigation: true });
    expect(ownSpy.mock.calls[0][0]).toMatchObject({ type: SHELL_HANDSHAKE, desktopId: "home", isViewing: false });
  });

  it("tells a shown page again when whether its desktop is viewed changes, and only then", async () => {
    api.desktops = [
      ...api.desktops,
      desktopRecord("alice", {
        owner: "user:user-alice-0001",
        windows: [windowRecord("win-a", "docs", "/a")],
      }),
    ];
    // This client's own layout of Alice's desktop and her own both place win-a, so the page is the same one
    // before and after the heartbeat says who the viewer is.
    api.writeLayout("alice", CLIENT, { updated_at: "2026-10-29T00:00:00Z", placements: [placementRecord("win-a")] });
    api.writeLayout("alice", api.ownerLayoutClientId, {
      updated_at: "2026-10-29T00:01:00Z",
      placements: [placementRecord("win-a")],
    });
    socket.deliver().onDesktopsUpdated(api.desktops);
    await settle();

    // Until the first heartbeat has answered, the desktop reads as this window's own: the page is not viewed.
    await store.switchDesktop("alice");
    renderChrome("win-a");
    layer.reconcile();
    const spy = spyOnFrame("win-a");
    load("win-a");
    const handshakes = (): unknown[] =>
      spy.mock.calls.map((call) => call[0]).filter((message) => message.type === SHELL_HANDSHAKE);
    expect(handshakes()).toMatchObject([{ desktopId: "alice", isViewing: false }]);

    // The answer lands and the desktop is somebody else's: the page is told once that it is viewed.
    setOwnIdentityForTesting({
      owner: true,
      user_id: "user-josh-0001",
      email: "josh@example.com",
      participant: "user:user-josh-0001",
    });
    store.takeViewerIdentityKnown();
    await settle();
    renderChrome("win-a");
    layer.reconcile();
    expect(handshakes()).toMatchObject([{ isViewing: false }, { desktopId: "alice", isViewing: true }]);
    layer.reconcile();
    expect(handshakes()).toHaveLength(2);
  });

  it("lays a page over its content box in the stage's own pixels while a viewed desktop is drawn scaled down", async () => {
    await viewAlicesDesktopAtHalfScale([placementRecord("win-a")]);
    expect(store.viewingLetterbox()?.scale).toBe(0.5);

    // The chrome and the host lie under the letterbox's half scale, so the box measures at half its size and
    // offset on screen; the page's position and size are in the host's unscaled pixels, so both are doubled back.
    renderChrome("win-a");
    const content = windows.querySelector('[data-window-id="win-a"] [data-window-content]') as HTMLElement;
    content.getBoundingClientRect = () => ({ left: 150, top: 80, width: 250, height: 200 }) as DOMRect;
    host.getBoundingClientRect = () => ({ left: 100, top: 20, width: 1000, height: 800 }) as DOMRect;
    layer.reconcile();
    const wrapper = wrapperOf("win-a");
    expect([wrapper.style.left, wrapper.style.top, wrapper.style.width, wrapper.style.height]).toEqual([
      "100px",
      "120px",
      "500px",
      "400px",
    ]);
    expect(wrapper.style.transform).toBe("");

    // Back on an own desktop, a box measures as it is.
    host.getBoundingClientRect = () => ({ left: 0, top: 0, width: 1000, height: 800 }) as DOMRect;
    await store.stopViewing();
    renderChrome();
    layer.reconcile();
    expect([wrapperOf("win-1").style.left, wrapperOf("win-1").style.width]).toEqual(["100px", "500px"]);
  });

  it("hides a page when its window is minimized, keeping the same frame, and destroys it on close", async () => {
    const frame = frameOf("win-1");
    store.minimizeWindow("win-1");
    layer.reconcile();
    expect(isParked(wrapperOf("win-1"))).toBe(true);
    store.restoreWindow("win-1");
    layer.reconcile();
    expect(frameOf("win-1")).toBe(frame);
    await store.closeWindow("win-1");
    layer.reconcile();
    expect(layer.hasPage("win-1")).toBe(false);
    expect(host.querySelectorAll("iframe")).toHaveLength(0);
  });

  it("keeps pages alive and hidden across a desktop switch", async () => {
    const frame = frameOf("win-1");
    await store.switchDesktop("work");
    layer.reconcile();
    expect(isParked(wrapperOf("win-1"))).toBe(true);
    await store.switchDesktop("home");
    layer.reconcile();
    expect(frameOf("win-1")).toBe(frame);
    expect(isParked(wrapperOf("win-1"))).toBe(false);
  });

  it("hides the page of a stopped app nothing brings back, and reloads it at the window's stored path once the app runs again", async () => {
    await navigateInPage("win-1", "/?doc=2");
    const reloads = spyOnSrc("win-1");
    socket.deliver().onAppsUpdated([{ ...docs, is_running: false, program: "" }, notes]);
    layer.reconcile();
    expect(isParked(wrapperOf("win-1"))).toBe(true);
    expect(reloads).toEqual([]);
    socket.deliver().onAppsUpdated([{ ...docs, program: "" }, notes]);
    layer.reconcile();
    expect(isParked(wrapperOf("win-1"))).toBe(false);
    expect(reloads).toEqual(["http://127.0.0.1:7001/?doc=2"]);
  });

  it("keeps a stoppable app's page while it is stopped: the shell's parker answers it", async () => {
    await navigateInPage("win-1", "/?doc=2");
    const reloads = spyOnSrc("win-1");
    socket.deliver().onAppsUpdated([{ ...docs, is_running: false }, notes]);
    layer.reconcile();
    expect(isParked(wrapperOf("win-1"))).toBe(false);
    socket.deliver().onAppsUpdated([docs, notes]);
    layer.reconcile();
    expect(reloads).toEqual([]);
  });
});

describe("covered pages", () => {
  // Both windows sit at the first cascade frame of the 1000x800 backdrop, so win-2 in front covers win-1's page.
  const CASCADE_RECT = { left: 50, top: 48, width: 600, height: 560 };

  beforeEach(() => {
    store.restoreWindow("win-2");
    layer.reconcile();
    drawChromeAt("win-1", CASCADE_RECT);
  });

  it("parks the page the window in front covers, still shown to the page, and unparks it once uncovered", () => {
    const spy = spyOnFrame("win-1");
    load("win-1");
    drawChromeAt("win-2", CASCADE_RECT);
    layer.reconcile();
    expect(isParked(wrapperOf("win-1"))).toBe(true);
    expect(isParked(wrapperOf("win-2"))).toBe(false);
    // Reloaded while covered, it is greeted as shown like any shown page.
    load("win-1");
    expect(spy.mock.calls.map((call) => call[0].type)).toEqual([
      SHELL_HANDSHAKE,
      SHELL_THEME,
      SHELL_SHOWN,
      SHELL_HANDSHAKE,
      SHELL_THEME,
      SHELL_SHOWN,
    ]);
    store.minimizeWindow("win-2");
    layer.reconcile();
    expect(isParked(wrapperOf("win-1"))).toBe(false);
    expect(spy.mock.calls.map((call) => call[0].type)).not.toContain(SHELL_HIDDEN);
  });

  it("counts the stopped placeholder in front as covering, but not a window with neither page nor placeholder", () => {
    drawChromeAt("win-2", CASCADE_RECT);
    socket.deliver().onAppsUpdated([docs, { ...notes, is_running: false, program: "" }]);
    layer.reconcile();
    expect(isParked(wrapperOf("win-2"))).toBe(true);
    expect(isParked(wrapperOf("win-1"))).toBe(true);
    // With its app unknown the window in front paints only its title bar, and the page behind shows through.
    socket.deliver().onAppsUpdated([docs]);
    layer.reconcile();
    expect(isParked(wrapperOf("win-1"))).toBe(false);
  });

  it("unparks a page the moved window covers, which moves with no reconcile, but not for a press", () => {
    drawChromeAt("win-2", CASCADE_RECT);
    layer.reconcile();
    // A press that may still become a drag moves nothing yet.
    layer.setGestureActive(true);
    expect(isParked(wrapperOf("win-1"))).toBe(true);
    store.beginWindowMove("win-2", { x: 300, y: 60 });
    layer.reconcile();
    expect(isParked(wrapperOf("win-1"))).toBe(false);
    store.endWindowMove({ x: 300, y: 60 });
    layer.setGestureActive(false);
    expect(isParked(wrapperOf("win-1"))).toBe(true);
  });

  it("keeps parked, while another window is moved, a page that a window staying put covers", () => {
    // win-2 covers win-1 and stays put; win-3, raised in front of both and clear of them, is the one dragged.
    store.restoreWindow("win-3");
    store.raiseWindow("win-3");
    drawChromeAt("win-2", CASCADE_RECT);
    drawChromeAt("win-3", { left: 700, top: 48, width: 280, height: 300 });
    layer.reconcile();
    expect(isParked(wrapperOf("win-1"))).toBe(true);
    layer.setGestureActive(true);
    store.beginWindowMove("win-3", { x: 800, y: 60 });
    layer.reconcile();
    expect(isParked(wrapperOf("win-1"))).toBe(true);
    expect(isParked(wrapperOf("win-2"))).toBe(false);
    expect(isParked(wrapperOf("win-3"))).toBe(false);
    store.endWindowMove({ x: 800, y: 60 });
    layer.setGestureActive(false);
    expect(isParked(wrapperOf("win-1"))).toBe(true);
  });

  it("gives the moved window's page a compositor layer of its own until the move ends, and no other page one", () => {
    store.restoreWindow("win-3");
    layer.reconcile();
    const promoted = (): string[] =>
      ["win-1", "win-2", "win-3"].filter((windowId) => wrapperOf(windowId).style.willChange === "transform");
    layer.setGestureActive(true);
    expect(promoted()).toEqual([]);
    store.beginWindowMove("win-2", { x: 300, y: 60 });
    layer.reconcile();
    expect(promoted()).toEqual(["win-2"]);
    store.endWindowMove({ x: 300, y: 60 });
    layer.setGestureActive(false);
    expect(promoted()).toEqual([]);
    // A resize changes the page's size on every move, which a layer of its own would not spare it repainting.
    store.beginWindowResize("win-2", "se");
    layer.reconcile();
    expect(promoted()).toEqual([]);
  });

  it("parks a page whose content is below the backdrop, but not the focused window's, which keeps the keyboard", () => {
    // Both dragged down until only their title bars show, win-2 last, so it is on top and focused.
    const belowBackdrop = { x: 0.05, y: 1, width: 0.6, height: 0.7 };
    store.setWindowFrame("win-1", belowBackdrop);
    store.setWindowFrame("win-2", belowBackdrop);
    for (const windowId of ["win-1", "win-2"]) drawChromeAt(windowId, { left: 50, top: 764, width: 600, height: 560 });
    layer.reconcile();
    expect(activeFocusedWindowId(store.getState())).toBe("win-2");
    frameOf("win-2").focus();
    layer.reconcile();
    expect(isParked(wrapperOf("win-1"))).toBe(true);
    expect(isParked(wrapperOf("win-2"))).toBe(false);
    expect(document.activeElement).toBe(frameOf("win-2"));
  });

  it("keeps live a page below the backdrop while a fullscreen window behind it opens the taskbar's band", () => {
    // win-3 fullscreen at the back, win-1 dragged down until only its title bar is in the backdrop, win-2 on top.
    store.restoreWindow("win-3");
    store.toggleFullscreen("win-3");
    store.setWindowFrame("win-1", { x: 0.05, y: 1, width: 0.6, height: 0.7 });
    store.raiseWindow("win-2");
    drawChromeAt("win-3", { left: 0, top: 0, width: 1000, height: 848 });
    drawChromeAt("win-1", { left: 50, top: 764, width: 600, height: 560 });
    drawChromeAt("win-2", CASCADE_RECT);
    layer.reconcile();
    expect(activePlacements(store.getState()).map((placement) => placement.window_id)).toEqual([
      "win-3",
      "win-1",
      "win-2",
    ]);
    expect(isParked(wrapperOf("win-1"))).toBe(false);
    // Out of fullscreen the backdrop clips the band again, and the page there is out of sight.
    store.toggleFullscreen("win-3");
    store.raiseWindow("win-2");
    layer.reconcile();
    expect(isParked(wrapperOf("win-1"))).toBe(true);
  });

  it("tests a viewed desktop's pages against the owner's backdrop the letterbox draws, not this one", async () => {
    await viewAlicesDesktopAtHalfScale([
      placementRecord("win-a", { frame: BOTTOM_RIGHT_QUARTER_FRAME }),
      placementRecord("win-b"),
    ]);
    // win-a lies in the bottom right of Alice's 2000x1600 backdrop: beyond this 1000x800 one, inside the letterbox.
    renderChrome("win-a", "win-b");
    drawChromeAt("win-a", { left: 500, top: 400, width: 500, height: 400 });
    layer.reconcile();
    expect(activeFocusedWindowId(store.getState())).toBe("win-b");
    expect(isParked(wrapperOf("win-a"))).toBe(false);
  });

  it("covers a page once the window travelling over it lands, and uncovers it as soon as the window leaves", () => {
    // Placed over win-1, but still travelling there from the bottom right.
    drawChromeAt("win-2", { left: 600, top: 450, width: 400, height: 350 });
    layer.reconcile();
    expect(isParked(wrapperOf("win-1"))).toBe(false);
    drawChromeAt("win-2", CASCADE_RECT);
    layer.reconcile();
    expect(isParked(wrapperOf("win-1"))).toBe(true);
    // Sent back to the bottom right: drawn over win-1 still, but no longer going to be.
    store.setWindowFrame("win-2", BOTTOM_RIGHT_QUARTER_FRAME);
    layer.reconcile();
    expect(isParked(wrapperOf("win-1"))).toBe(false);
  });
});

describe("the contract", () => {
  it("tells every greeted page a theme the shell wears, from the newest layer only", () => {
    const spy = spyOnFrame("win-1");
    load("win-1");
    spy.mockClear();
    void wearTheme({ id: "mac-classic", revision: "r1" });
    expect(spy.mock.calls.map((call) => call[0])).toEqual([
      { type: SHELL_THEME, theme: "mac-classic", revision: "r1", isPreview: false },
    ]);

    spy.mockClear();
    new LivePagesLayer(document.createElement("div"), store, PAGE_OPTIONS).start();
    void wearTheme({ id: "windows-2000", revision: "r2" });
    expect(spy).not.toHaveBeenCalled();
  });

  it("tells the pages no theme once the layer is stopped", () => {
    const spy = spyOnFrame("win-1");
    load("win-1");
    spy.mockClear();
    layer.stop();
    void wearTheme({ id: "mac-classic", revision: "r1" });
    expect(spy).not.toHaveBeenCalled();
  });

  it("tells the pages a previewed theme is a preview, on a change and in the greeting, and when it is saved", () => {
    const spy = spyOnFrame("win-1");
    load("win-1");
    spy.mockClear();
    void wearTheme({ id: "mac-classic", revision: "r1" }, document, { isRemembered: false });
    load("win-1");
    void wearTheme({ id: "mac-classic", revision: "r1" });
    const themeMessages = spy.mock.calls.map((call) => call[0]).filter((message) => message.type === SHELL_THEME);
    expect(themeMessages).toEqual([
      { type: SHELL_THEME, theme: "mac-classic", revision: "r1", isPreview: true },
      { type: SHELL_THEME, theme: "mac-classic", revision: "r1", isPreview: true },
      { type: SHELL_THEME, theme: "mac-classic", revision: "r1", isPreview: false },
    ]);
  });

  it("greets a page after every load with the window, desktop, and path, then says shown", () => {
    const spy = spyOnFrame("win-1");
    load("win-1");
    expect(spy.mock.calls.map((call) => call[0])).toEqual([
      {
        type: SHELL_HANDSHAKE,
        clientId: CLIENT,
        windowId: "win-1",
        desktopId: "home",
        app: "docs",
        path: "/?doc=1",
        canReportBug: false,
        opensLocalFiles: false,
        opensWorkspaceSettings: false,
        showsCoachMarks: false,
        isViewing: false,
      },
      // The theme the page wears follows every handshake (workspace-themes plan section 5.3).
      { type: SHELL_THEME, theme: "standard", revision: "", isPreview: false },
      { type: SHELL_SHOWN },
    ]);
    store.minimizeWindow("win-1");
    layer.reconcile();
    expect(spy.mock.calls[spy.mock.calls.length - 1][0]).toEqual({ type: SHELL_HIDDEN });
    spy.mockClear();
    load("win-1");
    expect(spy.mock.calls.map((call) => call[0])).toEqual([
      {
        type: SHELL_HANDSHAKE,
        clientId: CLIENT,
        windowId: "win-1",
        desktopId: "home",
        app: "docs",
        path: "/?doc=1",
        canReportBug: false,
        opensLocalFiles: false,
        opensWorkspaceSettings: false,
        showsCoachMarks: false,
        isViewing: false,
      },
      { type: SHELL_THEME, theme: "standard", revision: "", isPreview: false },
      { type: SHELL_HIDDEN },
    ]);
  });

  it("tells a hidden page again that it is hidden once its contract connects, after the load that first told it", () => {
    const spy = spyOnFrame("win-1");
    load("win-1");
    messageFromPage("win-1", { type: SHELL_CAPABILITIES, navigation: false });
    // A shown page is greeted again and told nothing of its visibility: shown is what a page takes itself to be.
    expect(spy.mock.calls.map((call) => call[0].type)).toEqual([
      SHELL_HANDSHAKE,
      SHELL_THEME,
      SHELL_SHOWN,
      SHELL_HANDSHAKE,
      SHELL_THEME,
    ]);
    store.minimizeWindow("win-1");
    layer.reconcile();
    load("win-1");
    spy.mockClear();
    messageFromPage("win-1", { type: SHELL_CAPABILITIES, navigation: false });
    expect(spy.mock.calls.map((call) => call[0].type)).toEqual([SHELL_HANDSHAKE, SHELL_THEME, SHELL_HIDDEN]);
  });

  it("tells a hidden page whose contract connects before its load that it is hidden once, not again on a reconcile", () => {
    store.minimizeWindow("win-1");
    layer.reconcile();
    const spy = spyOnFrame("win-1");
    messageFromPage("win-1", { type: SHELL_CAPABILITIES, navigation: false });
    layer.reconcile();
    expect(spy.mock.calls.map((call) => call[0].type)).toEqual([SHELL_HANDSHAKE, SHELL_THEME, SHELL_HIDDEN]);
  });

  it("hands every loaded page a fresh handshake when the embedder's word on Report a bug changes", () => {
    const spy = spyOnFrame("win-1");
    load("win-1");
    spy.mockClear();

    store.setCanReportBug(true);
    expect(spy.mock.calls.map((call) => call[0])).toEqual([
      {
        type: SHELL_HANDSHAKE,
        clientId: CLIENT,
        windowId: "win-1",
        desktopId: "home",
        app: "docs",
        path: "/?doc=1",
        canReportBug: true,
        opensLocalFiles: false,
        opensWorkspaceSettings: false,
        showsCoachMarks: false,
        isViewing: false,
      },
      { type: SHELL_THEME, theme: "standard", revision: "", isPreview: false },
    ]);
    store.setCanReportBug(true);
    expect(spy).toHaveBeenCalledTimes(2);
    store.setCanReportBug(false);
    expect(spy.mock.calls[2][0]).toMatchObject({ type: SHELL_HANDSHAKE, windowId: "win-1", canReportBug: false });
  });

  it("greets a page that loads after the embedder spoke with its word on Report a bug, and none before it loads", () => {
    const spy = spyOnFrame("win-1");
    store.setCanReportBug(true);
    expect(spy).not.toHaveBeenCalled();
    load("win-1");
    expect(spy.mock.calls[0][0]).toMatchObject({ type: SHELL_HANDSHAKE, windowId: "win-1", canReportBug: true });
  });

  it("opens the bug report about a page's shell:report-bug failure only while the embedder opens one", () => {
    const failure = { message: "Docs couldn't save. Try again.", details: "HTTP 500" };
    messageFromPage("win-1", { type: SHELL_REPORT_BUG, ...failure });
    expect(bugReportsOpened).toEqual([]);
    store.setCanReportBug(true);
    messageFromPage("win-1", { type: SHELL_REPORT_BUG, ...failure });
    messageFromPage("win-1", { type: SHELL_REPORT_BUG, message: "Docs couldn't save.", details: null });
    expect(bugReportsOpened).toEqual([failure, { message: "Docs couldn't save.", details: null }]);
  });

  it("tells a user's own app's page it cannot report a bug, and drops a shell:report-bug it sends anyway", () => {
    store.setCanReportBug(true);
    store.restoreWindow("win-2");
    layer.reconcile();
    const spy = spyOnFrame("win-2");
    load("win-2");
    expect(spy.mock.calls[0][0]).toMatchObject({ type: SHELL_HANDSHAKE, app: "notes", canReportBug: false });
    messageFromPage("win-2", { type: SHELL_REPORT_BUG, message: "Notes couldn't save.", details: "HTTP 500" });
    expect(bugReportsOpened).toEqual([]);
    messageFromPage("win-1", { type: SHELL_REPORT_BUG, message: "Docs couldn't save.", details: "HTTP 500" });
    expect(bugReportsOpened).toEqual([{ message: "Docs couldn't save.", details: "HTTP 500" }]);
  });

  it("opens the bug report with nothing attached for a shell:report-bug whose payload holds no failure", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    store.setCanReportBug(true);
    messageFromPage("win-1", { type: SHELL_REPORT_BUG });
    messageFromPage("win-1", { type: SHELL_REPORT_BUG, message: "Docs couldn't save.", details: 42 });
    messageFromPage("win-1", { type: SHELL_REPORT_BUG, message: "  ", details: "HTTP 500" });
    expect(bugReportsOpened).toEqual([null, null, null]);
    warn.mockRestore();
  });

  it("tells a page in its handshake whether the embedder opens its settings for the files shared from the computer", () => {
    store.setOpensLocalFiles(true);
    const spy = spyOnFrame("win-1");
    load("win-1");
    expect(spy.mock.calls[0][0]).toMatchObject({ type: SHELL_HANDSHAKE, opensLocalFiles: true });
  });

  it("tells a page in its handshake whether the embedder opens its workspace settings", () => {
    store.setOpensWorkspaceSettings(true);
    const spy = spyOnFrame("win-1");
    load("win-1");
    expect(spy.mock.calls[0][0]).toMatchObject({ type: SHELL_HANDSHAKE, opensWorkspaceSettings: true });
  });

  it("tells a page in its handshake whether the embedder draws coach marks on its own controls", () => {
    store.setShowsCoachMarks(true);
    const spy = spyOnFrame("win-1");
    load("win-1");
    expect(spy.mock.calls[0][0]).toMatchObject({ type: SHELL_HANDSHAKE, showsCoachMarks: true });
  });

  it("greets a page reloaded while hidden with its own desktop, not the active one", async () => {
    load("win-1");
    await store.switchDesktop("work");
    layer.reconcile();
    const spy = spyOnFrame("win-1");
    load("win-1");
    expect(spy.mock.calls[0][0]).toMatchObject({ type: SHELL_HANDSHAKE, windowId: "win-1", desktopId: "home" });
  });

  it("posts a page's location to the shell, remembering it first so the update never bounces back", async () => {
    const spy = spyOnFrame("win-1");
    load("win-1");
    messageFromPage("win-1", { type: SHELL_CAPABILITIES, navigation: true });
    spy.mockClear();
    messageFromPage("win-1", { type: SHELL_LOCATION, path: "/?doc=2", title: "Second" });
    // A redraw while the report is on its way (the stored path is still the old one): no navigation.
    layer.reconcile();
    expect(spy).not.toHaveBeenCalled();
    await settle();
    expect(api.calls).toContain("reportWindowLocation:home:win-1:client-1:/?doc=2:Second");
    // The broadcast that follows carries the path the page already reported: no navigation either.
    socket.deliver().onDesktopsUpdated(api.desktops);
    layer.reconcile();
    expect(spy).not.toHaveBeenCalled();
    expect(frameOf("win-1").getAttribute("src")).toBe("http://127.0.0.1:7001/?doc=1");
  });

  it("a desktops update from another cause while the report is on its way does not send the page back", async () => {
    const urls = spyOnSrc("win-1");
    const spy = spyOnFrame("win-1");
    load("win-1");
    messageFromPage("win-1", { type: SHELL_CAPABILITIES, navigation: true });
    spy.mockClear();
    const before = api.desktops;
    messageFromPage("win-1", { type: SHELL_LOCATION, path: "/?doc=2", title: "" });
    // Another page's report elsewhere broadcast the desktops as they stood: the old path is still stored.
    socket.deliver().onDesktopsUpdated(before);
    layer.reconcile();
    expect(spy).not.toHaveBeenCalled();
    await settle();
    // The route's answer is taken at once, before the broadcast, so a reload meanwhile lands at the new path.
    expect(store.getState().desktops[0].windows.find((window) => window.id === "win-1")?.path).toBe("/?doc=2");
    // A snapshot from before the report still arriving after the answer moves nothing either.
    socket.deliver().onDesktopsUpdated(before);
    layer.reconcile();
    socket.deliver().onDesktopsUpdated(api.desktops);
    layer.reconcile();
    expect(spy).not.toHaveBeenCalled();
    expect(urls).toEqual([]);
    // Once the shell shows the reported path, a later move elsewhere is followed again.
    const [home, work] = api.desktops;
    socket.deliver().onDesktopsUpdated([
      {
        ...home,
        windows: home.windows.map((window) => (window.id === "win-1" ? { ...window, path: "/?doc=9" } : window)),
      },
      work,
    ]);
    layer.reconcile();
    expect(spy.mock.calls.map((call) => call[0])).toEqual([{ type: SHELL_NAVIGATE, path: "/?doc=9" }]);
  });

  it("follows this client's own navigation even back to a path the page just reported leaving", async () => {
    const spy = spyOnFrame("win-1");
    load("win-1");
    messageFromPage("win-1", { type: SHELL_CAPABILITIES, navigation: true });
    spy.mockClear();
    // The chooser's draft: this client points its own page at the draft path.
    expect(await store.navigateOwnWindow("win-1", "/?draft=a")).toBe(true);
    layer.reconcile();
    expect(spy.mock.calls.map((call) => call[0])).toEqual([{ type: SHELL_NAVIGATE, path: "/?draft=a" }]);
    // The page takes the draft and reports its selection alone, leaving the draft path behind.
    messageFromPage("win-1", { type: SHELL_LOCATION, path: "/?doc=1", title: "" });
    await settle();
    layer.reconcile();
    spy.mockClear();
    // The same draft again: a stale-snapshot guard would call this "the path the page reported leaving" and skip
    // it; a navigation this client asked for is followed.
    expect(await store.navigateOwnWindow("win-1", "/?draft=a")).toBe(true);
    layer.reconcile();
    expect(spy.mock.calls.map((call) => call[0])).toEqual([{ type: SHELL_NAVIGATE, path: "/?draft=a" }]);
  });

  it("a report the shell refuses leaves the page to follow the stored path again", async () => {
    const spy = spyOnFrame("win-1");
    load("win-1");
    messageFromPage("win-1", { type: SHELL_CAPABILITIES, navigation: true });
    spy.mockClear();
    api.refusal = "no such window";
    messageFromPage("win-1", { type: SHELL_LOCATION, path: "/?doc=2", title: "" });
    await settle();
    api.refusal = null;
    socket.deliver().onDesktopsUpdated(api.desktops);
    layer.reconcile();
    expect(spy.mock.calls.map((call) => call[0])).toEqual([{ type: SHELL_NAVIGATE, path: "/?doc=1" }]);
  });

  it("cuts a reported title to the shell's limit rather than having the whole report refused", async () => {
    load("win-1");
    messageFromPage("win-1", { type: SHELL_LOCATION, path: "/?doc=2", title: ` ${"t".repeat(300)} ` });
    await settle();
    expect(api.calls).toContain(`reportWindowLocation:home:win-1:client-1:/?doc=2:${"t".repeat(256)}`);
  });

  it("a local edit between a page's report and the broadcast does not point the page back", async () => {
    const urls = spyOnSrc("win-1");
    load("win-1");
    // A page with no in-place navigation: being sent back would mean a reload.
    messageFromPage("win-1", { type: SHELL_LOCATION, path: "/?doc=2", title: "" });
    await settle();
    // Another window opens (the desktops record changes locally) while the broadcast is still on its way.
    await store.openWindowAt("notes", "/c", "new");
    layer.reconcile();
    expect(urls).toEqual([]);
    socket.deliver().onDesktopsUpdated(api.desktops);
    layer.reconcile();
    expect(urls).toEqual([]);
  });

  it("follows a path changed elsewhere: navigate for a capable page, reload for the rest", () => {
    store.restoreWindow("win-2");
    layer.reconcile();
    const docsSpy = spyOnFrame("win-1");
    load("win-1");
    messageFromPage("win-1", { type: SHELL_CAPABILITIES, navigation: true });
    load("win-2");
    docsSpy.mockClear();
    const [home, work] = api.desktops;
    socket.deliver().onDesktopsUpdated([
      {
        ...home,
        windows: home.windows.map((window) =>
          window.id === "win-1"
            ? { ...window, path: "/?doc=9" }
            : window.id === "win-2"
              ? { ...window, path: "/elsewhere" }
              : window,
        ),
      },
      work,
    ]);
    layer.reconcile();
    expect(docsSpy.mock.calls.map((call) => call[0])).toEqual([{ type: SHELL_NAVIGATE, path: "/?doc=9" }]);
    expect(frameOf("win-1").getAttribute("src")).toBe("http://127.0.0.1:7001/?doc=1");
    expect(frameOf("win-2").getAttribute("src")).toBe("http://127.0.0.1:7002/elsewhere");
    // Told once: the same broadcast again moves nothing.
    docsSpy.mockClear();
    layer.reconcile();
    expect(docsSpy).not.toHaveBeenCalled();
  });

  it("an independent window's page opens at this client's own path, and follows only that path", async () => {
    const frame = await showIndependentWindow([
      placementRecord("win-2", { is_minimized: true }),
      placementRecord("win-1"),
      placementRecord("win-4"),
    ]);
    expect(frame.getAttribute("src")).toBe("http://127.0.0.1:7001/?doc=7");
    expect(frame.title).toBe("Seven");
    const spy = spyOnFrame("win-4");
    load("win-4");
    expect(spy.mock.calls[0][0]).toMatchObject({ type: SHELL_HANDSHAKE, windowId: "win-4", path: "/?doc=7" });
    messageFromPage("win-4", { type: SHELL_CAPABILITIES, navigation: true });
    spy.mockClear();
    // Its own report is stored for this client and never bounces, whatever the shared record says.
    messageFromPage("win-4", { type: SHELL_LOCATION, path: "/?doc=8", title: "Eight" });
    await settle();
    expect(api.calls).toContain("reportWindowLocation:home:win-4:client-1:/?doc=8:Eight");
    socket.deliver().onDesktopsUpdated(api.desktops);
    socket.deliver().onPlacementsUpdated({ desktopId: "home", clientId: CLIENT, saveId: "save-shell-2" });
    await settle();
    layer.reconcile();
    expect(spy).not.toHaveBeenCalled();
    expect(store.getState().desktops[0].windows.find((window) => window.id === "win-4")?.path).toBe("/");
    // An agent's navigate for this client arrives as a stored path with the layout, and the page follows it.
    api.windowPaths.set(`${CLIENT}/win-4`, { path: "/?doc=9", title: "Eight" });
    socket.deliver().onPlacementsUpdated({ desktopId: "home", clientId: CLIENT, saveId: "save-shell-3" });
    await settle();
    layer.reconcile();
    expect(spy.mock.calls.map((call) => call[0])).toEqual([{ type: SHELL_NAVIGATE, path: "/?doc=9" }]);
  });

  it("follows an independent window's stored path back to the one its page reported leaving", async () => {
    await showIndependentWindow([placementRecord("win-4")]);
    const spy = spyOnFrame("win-4");
    load("win-4");
    messageFromPage("win-4", { type: SHELL_CAPABILITIES, navigation: true });
    spy.mockClear();
    // From here the layer reconciles only when the store says something changed, as the app's redraw does.
    store.subscribe(() => layer.reconcile());
    // The page moves on by itself, and the shell's broadcast confirms the path it reported.
    messageFromPage("win-4", { type: SHELL_LOCATION, path: "/?doc=8", title: "Eight" });
    await settle();
    socket.deliver().onPlacementsUpdated({ desktopId: "home", clientId: CLIENT, saveId: "save-shell-2" });
    await settle();
    expect(spy).not.toHaveBeenCalled();
    // An agent's show then points this client's page back where it was (opening a notification's chat).
    api.windowPaths.set(`${CLIENT}/win-4`, { path: "/?doc=7", title: "Seven" });
    socket.deliver().onPlacementsUpdated({ desktopId: "home", clientId: CLIENT, saveId: "save-shell-3" });
    await settle();
    expect(spy.mock.calls.map((call) => call[0])).toEqual([{ type: SHELL_NAVIGATE, path: "/?doc=7" }]);
  });

  it("leaves an independent window's hidden page alone while another desktop is active", async () => {
    const frame = await showIndependentWindow([placementRecord("win-4")]);
    expect(frame.getAttribute("src")).toBe("http://127.0.0.1:7001/?doc=7");
    const spy = spyOnFrame("win-4");
    load("win-4");
    messageFromPage("win-4", { type: SHELL_CAPABILITIES, navigation: true });
    spy.mockClear();
    // The work desktop's layout knows nothing of this client's path for the window; neither it nor a desktops
    // update while it is active sends the hidden page to the home path.
    await store.switchDesktop("work");
    layer.reconcile();
    socket.deliver().onDesktopsUpdated(api.desktops);
    layer.reconcile();
    expect(isParked(wrapperOf("win-4"))).toBe(true);
    expect(spy.mock.calls.map((call) => call[0])).toEqual([{ type: SHELL_HIDDEN }]);
    expect(frame.getAttribute("src")).toBe("http://127.0.0.1:7001/?doc=7");
    // Back on its desktop, the stored path is the one the page is at, so nothing moves it.
    await store.switchDesktop("home");
    renderChrome("win-4");
    layer.reconcile();
    expect(spy.mock.calls.map((call) => call[0].type)).not.toContain(SHELL_NAVIGATE);
    expect(frame.getAttribute("src")).toBe("http://127.0.0.1:7001/?doc=7");
  });

  it("does not move an independent window's page while its desktop's layout is still being read", async () => {
    const frame = await showIndependentWindow([placementRecord("win-4")]);
    const spy = spyOnFrame("win-4");
    load("win-4");
    messageFromPage("win-4", { type: SHELL_CAPABILITIES, navigation: true });
    await store.switchDesktop("work");
    layer.reconcile();
    spy.mockClear();
    // Back to home with its layout held open: a desktops update meanwhile knows no stored path for the page.
    const answerLayout = api.holdReads();
    const switching = store.switchDesktop("home");
    await settle();
    expect(store.getState().activeDesktopId).toBe("home");
    expect(store.getState().isLayoutLoaded).toBe(false);
    socket.deliver().onDesktopsUpdated(api.desktops);
    layer.reconcile();
    expect(spy.mock.calls.map((call) => call[0].type)).not.toContain(SHELL_NAVIGATE);
    expect(frame.getAttribute("src")).toBe("http://127.0.0.1:7001/?doc=7");
    answerLayout();
    await switching;
    renderChrome("win-4");
    layer.reconcile();
    expect(spy.mock.calls.map((call) => call[0].type)).not.toContain(SHELL_NAVIGATE);
    expect(frame.getAttribute("src")).toBe("http://127.0.0.1:7001/?doc=7");
  });

  it("reloads and greets an independent window's hidden page at this client's own path", async () => {
    await showIndependentWindow([placementRecord("win-4")]);
    load("win-4");
    await store.switchDesktop("work");
    layer.reconcile();
    // The work desktop's layout knows nothing of this client's path for the window; an agent's refresh of the
    // app still reloads the hidden page where it is, and the handshake after the load names that path.
    const reloads = spyOnSrc("win-4");
    socket.deliver().onLayoutOp({ op: "refresh", args: { app: "docs" }, requester: "" });
    expect(reloads).toEqual(["http://127.0.0.1:7001/?doc=7"]);
    const spy = spyOnFrame("win-4");
    load("win-4");
    expect(spy.mock.calls[0][0]).toMatchObject({ type: SHELL_HANDSHAKE, desktopId: "home", path: "/?doc=7" });
  });

  it("raises the window of a page that says it took focus", () => {
    store.restoreWindow("win-2");
    layer.reconcile();
    expect(activeFocusedWindowId(store.getState())).toBe("win-2");
    messageFromPage("win-1", { type: SHELL_FOCUSED });
    expect(activeFocusedWindowId(store.getState())).toBe("win-1");
  });

  it("ignores a focus report from a hidden page, so a pulled-out or minimized window is not brought back by it", async () => {
    // Minimized: its page is hidden, and the browser can still restore focus to it.
    store.minimizeWindow("win-1");
    layer.reconcile();
    expect(isParked(wrapperOf("win-1"))).toBe(true);
    messageFromPage("win-1", { type: SHELL_FOCUSED });
    expect(activePlacements(store.getState()).find((placement) => placement.window_id === "win-1")).toMatchObject({
      is_minimized: true,
    });
    // Pulled out: the same, and the window stays out.
    api.writeLayout("home", CLIENT, {
      updated_at: null,
      placements: [placementRecord("win-2", { is_minimized: true }), placementRecord("win-1", { is_detached: true })],
    });
    socket.deliver().onPlacementsUpdated({ desktopId: "home", clientId: CLIENT, saveId: "save-elsewhere" });
    await settle();
    layer.reconcile();
    messageFromPage("win-1", { type: SHELL_FOCUSED });
    expect(activePlacements(store.getState()).find((placement) => placement.window_id === "win-1")).toMatchObject({
      is_detached: true,
    });
  });

  it("takes the document's focus off a page as it hides it", () => {
    frameOf("win-1").focus();
    expect(document.activeElement).toBe(frameOf("win-1"));
    store.minimizeWindow("win-1");
    layer.reconcile();
    expect(document.activeElement).toBe(host);
    store.restoreWindow("win-1");
    layer.reconcile();
    expect(document.activeElement).toBe(host);
  });

  it("takes the document's focus off a page whose window another window is raised over, however it was raised", () => {
    frameOf("win-1").focus();
    // A raise that no press on a handle made.
    store.restoreWindow("win-2");
    layer.reconcile();
    expect(activeFocusedWindowId(store.getState())).toBe("win-2");
    expect(document.activeElement).toBe(host);
  });

  it("leaves the document's focus in a page that reported taking it", () => {
    store.restoreWindow("win-2");
    layer.reconcile();
    frameOf("win-1").focus();
    messageFromPage("win-1", { type: SHELL_FOCUSED });
    layer.reconcile();
    expect(activeFocusedWindowId(store.getState())).toBe("win-1");
    expect(document.activeElement).toBe(frameOf("win-1"));
  });

  it("leaves the dragged window on top when another page says it took focus mid-drag", () => {
    store.restoreWindow("win-2");
    layer.reconcile();
    store.beginWindowMove("win-1", { x: 100, y: 60 });
    expect(activeFocusedWindowId(store.getState())).toBe("win-1");
    messageFromPage("win-2", { type: SHELL_FOCUSED });
    expect(activeFocusedWindowId(store.getState())).toBe("win-1");
    store.endWindowMove({ x: 150, y: 90 });
    expect(activeFocusedWindowId(store.getState())).toBe("win-1");
  });

  it("a press on a window's handle takes the document's focus back from another window's page, not its own", () => {
    store.restoreWindow("win-2");
    layer.reconcile();
    frameOf("win-2").focus();
    expect(document.activeElement).toBe(frameOf("win-2"));
    layer.takeFocusFromOtherPages("win-2");
    expect(document.activeElement).toBe(frameOf("win-2"));
    layer.takeFocusFromOtherPages("win-1");
    expect(document.activeElement).toBe(host);
    // A handle of no window (a shortcut, an entry) takes it from any page.
    frameOf("win-2").focus();
    layer.takeFocusFromOtherPages(null);
    expect(document.activeElement).toBe(host);
    // An iframe that is no page of the shell's is left alone.
    const other = document.createElement("iframe");
    other.tabIndex = 0;
    document.body.appendChild(other);
    other.focus();
    layer.takeFocusFromOtherPages("win-1");
    expect(document.activeElement).toBe(other);
    other.remove();
  });

  it("opens a page's shell:open on its own app, and warns about the address form", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    messageFromPage("win-1", { type: SHELL_OPEN, path: "/?doc=3", ifPresent: "new" });
    await settle();
    expect(api.calls).toContain("openWindow:home:docs:/?doc=3:new");
    messageFromPage("win-1", { type: SHELL_OPEN, address: "app:docs?instance=x" });
    await settle();
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("shell:open ignored"));
    warn.mockRestore();
  });

  it("relays a page's shell:message with its app as the sender, and warns about one with no typed message", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    messageFromPage("win-1", { type: SHELL_MESSAGE, message: { type: "open:file", path: "/home/user/plan.md" } });
    await settle();
    expect(api.relayedMessages).toEqual([
      { type: "open:file", clientId: "client-1", payload: { path: "/home/user/plan.md" }, sender: "docs" },
    ]);
    messageFromPage("win-1", { type: SHELL_MESSAGE, message: { path: "/home/user/plan.md" } });
    await settle();
    expect(api.relayedMessages).toHaveLength(1);
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("shell:message ignored"));
    warn.mockRestore();
  });

  it("opens a page's shell:open-link where the store opens a link, as sent by its app, and warns about one with no url", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    messageFromPage("win-1", { type: SHELL_OPEN_LINK, url: "http://localhost:3000/preview" });
    await settle();
    expect(api.relayedMessages).toEqual([
      { type: "open:url", clientId: "client-1", payload: { url: "http://localhost:3000/preview" }, sender: "docs" },
    ]);
    messageFromPage("win-1", { type: SHELL_OPEN_LINK });
    await settle();
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("shell:open-link ignored"));
    warn.mockRestore();
  });

  it("runs a page's shell:start-with-text as the primary text action, and warns when it carries no text", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const textApps = [
      appRecord("docs", {
        url: "http://127.0.0.1:7001",
        launch_paths: [launchPathRecord({ id: "new", path: "/new", params: ["message"], text_param: "message" })],
      }),
      notes,
    ];
    offerApps(api, socket, textApps);
    messageFromPage("win-1", { type: SHELL_START_WITH_TEXT, text: "hello there" });
    await settle();
    expect(api.calls).toContain(`launch:home:docs:new:{"message":"hello there"}:new`);
    expect(api.calls).toContain("openWindow:home:docs:/new?message=hello+there:new");
    messageFromPage("win-1", { type: SHELL_START_WITH_TEXT });
    await settle();
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("shell:start-with-text ignored"));
    warn.mockRestore();
  });

  it("drafts a page's shell:draft-text into the pinned window that takes a draft, and warns when it carries no text", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const buddy = appRecord("buddy", {
      url: "http://127.0.0.1:7003",
      pin: { path: "/", style: "plain", scope: "linked", default_mode: "bar" },
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
    });
    offerApps(api, socket, [docs, notes, buddy]);
    api.postLaunchAnswer = "/?chat=agent-1";
    const [home, work] = api.desktops;
    api.desktops = [
      { ...home, windows: [...home.windows, windowRecord("win-9", "buddy", "/", { is_pinned: true })] },
      work,
    ];
    socket.deliver().onDesktopsUpdated(api.desktops);
    messageFromPage("win-1", { type: SHELL_DRAFT_TEXT, text: "Explain this element:" });
    await settle();
    expect(api.calls).toContain(`launch:home:buddy:draft:{"message":"Explain this element:"}:window:win-9`);
    messageFromPage("win-1", { type: SHELL_DRAFT_TEXT });
    await settle();
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("shell:draft-text ignored"));
    warn.mockRestore();
  });

  it("shows a page's shell:show-coach-card as the page's words under the chrome control it names", () => {
    messageFromPage("win-1", {
      type: SHELL_SHOW_COACH_CARD,
      text: "Your connections live here.",
      chromeAnchor: "permissions",
    });
    expect(coachCards).toEqual([["Your connections live here.", { chromeAnchor: "permissions" }]]);
  });

  it("shows a page's shell:show-coach-card as the page's words at the agent's avatar when it names that target", () => {
    messageFromPage("win-1", { type: SHELL_SHOW_COACH_CARD, text: "Tell me what to build.", target: "avatar" });
    expect(coachCards).toEqual([["Tell me what to build.", { target: "avatar" }]]);
  });

  it("drops a shell:show-coach-card that names both places, or a target that is not the avatar", () => {
    messageFromPage("win-1", {
      type: SHELL_SHOW_COACH_CARD,
      text: "Look here.",
      chromeAnchor: "permissions",
      target: "avatar",
    });
    messageFromPage("win-1", { type: SHELL_SHOW_COACH_CARD, text: "Look here.", target: "launcher" });
    messageFromPage("win-1", { type: SHELL_SHOW_COACH_CARD, text: "Look here.", target: 3 });
    messageFromPage("win-1", { type: SHELL_SHOW_COACH_CARD, text: "", target: "avatar" });
    expect(coachCards).toEqual([]);
  });

  it("drops a shell:show-coach-card whose anchor is not one of the chrome's, or whose text is missing, empty or too long", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    messageFromPage("win-1", { type: SHELL_SHOW_COACH_CARD, text: "Look here.", chromeAnchor: "taskbar" });
    messageFromPage("win-1", { type: SHELL_SHOW_COACH_CARD, text: "Look here." });
    messageFromPage("win-1", { type: SHELL_SHOW_COACH_CARD, text: "Look here.", chromeAnchor: 3 });
    messageFromPage("win-1", { type: SHELL_SHOW_COACH_CARD, chromeAnchor: "share" });
    messageFromPage("win-1", { type: SHELL_SHOW_COACH_CARD, text: "", chromeAnchor: "share" });
    messageFromPage("win-1", { type: SHELL_SHOW_COACH_CARD, text: ["Look here."], chromeAnchor: "share" });
    messageFromPage("win-1", { type: SHELL_SHOW_COACH_CARD, text: "x".repeat(501), chromeAnchor: "share" });
    expect(coachCards).toEqual([]);
    // Dropped without a word: a page newer than this shell may name an anchor it does not know.
    expect(warn).not.toHaveBeenCalled();

    messageFromPage("win-1", { type: SHELL_SHOW_COACH_CARD, text: "x".repeat(500), chromeAnchor: "share" });
    expect(coachCards).toEqual([["x".repeat(500), { chromeAnchor: "share" }]]);
    warn.mockRestore();
  });

  it("takes a page's shell:agent-working as that page's last word, and counts an agent as working while any page says so", () => {
    store.restoreWindow("win-2");
    layer.reconcile();
    // A layer starts from no page having said anything.
    expect(pagesAgentWorking).toEqual([false]);

    messageFromPage("win-1", { type: SHELL_AGENT_WORKING, isWorking: true });
    expect(lastToldOfPages()).toBe(true);
    const toldAfterFirst = pagesAgentWorking.length;
    // The same word again changes nothing, and is not passed on.
    messageFromPage("win-1", { type: SHELL_AGENT_WORKING, isWorking: true });
    expect(pagesAgentWorking).toHaveLength(toldAfterFirst);

    messageFromPage("win-2", { type: SHELL_AGENT_WORKING, isWorking: true });
    messageFromPage("win-1", { type: SHELL_AGENT_WORKING, isWorking: false });
    expect(lastToldOfPages()).toBe(true);

    messageFromPage("win-2", { type: SHELL_AGENT_WORKING, isWorking: false });
    expect(lastToldOfPages()).toBe(false);
  });

  it("drops a shell:agent-working that carries no boolean", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    messageFromPage("win-1", { type: SHELL_AGENT_WORKING });
    messageFromPage("win-1", { type: SHELL_AGENT_WORKING, isWorking: "true" });
    messageFromPage("win-1", { type: SHELL_AGENT_WORKING, isWorking: 1 });
    messageFromPage("win-1", { type: SHELL_AGENT_WORKING, isWorking: null });

    expect(pagesAgentWorking).toEqual([false]);
    expect(warn).not.toHaveBeenCalled();
    warn.mockRestore();
  });

  it("forgets what a page said of an agent working when its window closes", async () => {
    messageFromPage("win-1", { type: SHELL_AGENT_WORKING, isWorking: true });
    expect(lastToldOfPages()).toBe(true);

    await store.closeWindow("win-1");
    layer.reconcile();

    expect(lastToldOfPages()).toBe(false);
  });

  it("forgets it when the page's frame loads again, before greeting the fresh page", () => {
    messageFromPage("win-1", { type: SHELL_AGENT_WORKING, isWorking: true });
    const spy = spyOnFrame("win-1");
    const toldBeforeGreeting: boolean[] = [];
    spy.mockImplementation((message: { type?: string }) => {
      if (message.type === SHELL_HANDSHAKE) toldBeforeGreeting.push(lastToldOfPages() ?? true);
    });

    load("win-1");

    expect(lastToldOfPages()).toBe(false);
    // A page that still has an agent working answers the greeting by saying so again, which must come after.
    expect(toldBeforeGreeting).toEqual([false]);
    messageFromPage("win-1", { type: SHELL_AGENT_WORKING, isWorking: true });
    expect(lastToldOfPages()).toBe(true);
  });

  it("forgets it when the layer stops, and a layer that replaces another starts from nobody working", () => {
    messageFromPage("win-1", { type: SHELL_AGENT_WORKING, isWorking: true });
    expect(lastToldOfPages()).toBe(true);

    new LivePagesLayer(document.createElement("div"), store, PAGE_OPTIONS).start();
    expect(lastToldOfPages()).toBe(false);

    pagesAgentWorking = [];
    messageFromPage("win-1", { type: SHELL_AGENT_WORKING, isWorking: false });
    // The replaced layer no longer hears its pages.
    expect(pagesAgentWorking).toEqual([]);
    layer.stop();
    expect(pagesAgentWorking).toEqual([false]);
  });

  it("takes a page's shell:first-conversation-step as that page's last word, and holds while any page says pending", () => {
    store.restoreWindow("win-2");
    layer.reconcile();
    // A layer starts from no page having said anything, which is no page's word.
    expect(pagesFirstConversation).toEqual([{ isPending: false, isSaidByPage: false }]);

    messageFromPage("win-1", { type: SHELL_FIRST_CONVERSATION_STEP, isPending: true });
    // The same word again changes nothing, and is not passed on.
    messageFromPage("win-1", { type: SHELL_FIRST_CONVERSATION_STEP, isPending: true });
    messageFromPage("win-2", { type: SHELL_FIRST_CONVERSATION_STEP, isPending: true });
    messageFromPage("win-1", { type: SHELL_FIRST_CONVERSATION_STEP, isPending: false });
    messageFromPage("win-2", { type: SHELL_FIRST_CONVERSATION_STEP, isPending: false });

    expect(pagesFirstConversation.slice(1)).toEqual([
      { isPending: true, isSaidByPage: true },
      { isPending: true, isSaidByPage: true },
      { isPending: true, isSaidByPage: true },
      { isPending: false, isSaidByPage: true },
    ]);
  });

  it("drops a shell:first-conversation-step that carries no boolean", () => {
    messageFromPage("win-1", { type: SHELL_FIRST_CONVERSATION_STEP });
    messageFromPage("win-1", { type: SHELL_FIRST_CONVERSATION_STEP, isPending: "true" });
    messageFromPage("win-1", { type: SHELL_FIRST_CONVERSATION_STEP, isPending: 1 });
    messageFromPage("win-1", { type: SHELL_FIRST_CONVERSATION_STEP, isPending: null });

    expect(pagesFirstConversation).toHaveLength(1);
  });

  it("stops holding on a page's word when its window closes, as a word dropped rather than said", async () => {
    messageFromPage("win-1", { type: SHELL_FIRST_CONVERSATION_STEP, isPending: true });

    await store.closeWindow("win-1");
    layer.reconcile();

    expect(lastToldOfFirstConversation()).toEqual({ isPending: false, isSaidByPage: false });
  });

  it("drops it when the page's frame loads again, before greeting the fresh page, which says it again", () => {
    messageFromPage("win-1", { type: SHELL_FIRST_CONVERSATION_STEP, isPending: true });
    const spy = spyOnFrame("win-1");
    const toldBeforeGreeting: (boolean | undefined)[] = [];
    spy.mockImplementation((message: { type?: string }) => {
      if (message.type === SHELL_HANDSHAKE) toldBeforeGreeting.push(lastToldOfFirstConversation()?.isPending);
    });

    load("win-1");

    expect(lastToldOfFirstConversation()).toEqual({ isPending: false, isSaidByPage: false });
    expect(toldBeforeGreeting).toEqual([false]);
    messageFromPage("win-1", { type: SHELL_FIRST_CONVERSATION_STEP, isPending: true });
    expect(lastToldOfFirstConversation()).toEqual({ isPending: true, isSaidByPage: true });
  });

  it("drops it when the layer stops, and a layer that replaces another starts from no page holding", () => {
    messageFromPage("win-1", { type: SHELL_FIRST_CONVERSATION_STEP, isPending: true });

    new LivePagesLayer(document.createElement("div"), store, PAGE_OPTIONS).start();
    expect(lastToldOfFirstConversation()).toEqual({ isPending: false, isSaidByPage: false });

    pagesFirstConversation = [];
    messageFromPage("win-1", { type: SHELL_FIRST_CONVERSATION_STEP, isPending: false });
    // The replaced layer no longer hears its pages.
    expect(pagesFirstConversation).toEqual([]);
    layer.stop();
    expect(pagesFirstConversation).toEqual([{ isPending: false, isSaidByPage: false }]);
  });

  it("reloads pages for the agent's refresh op at their windows' stored paths", async () => {
    await navigateInPage("win-1", "/?doc=2");
    const reloads = spyOnSrc("win-1");
    socket.deliver().onLayoutOp({ op: "refresh", args: { window: "win-1" }, requester: "" });
    socket.deliver().onLayoutOp({ op: "refresh", args: { app: "docs" }, requester: "" });
    expect(reloads).toEqual(["http://127.0.0.1:7001/?doc=2", "http://127.0.0.1:7001/?doc=2"]);
  });

  it("tells the focused page about the close chord before closing its window", async () => {
    const spy = spyOnFrame("win-1");
    load("win-1");
    spy.mockClear();
    await store.closeFocusedWindow();
    expect(spy.mock.calls[0][0]).toEqual({ type: SHELL_CLOSE_REQUEST });
    expect(api.calls).toContain("closeWindow:home:win-1");
  });

  it("only tells a page that owns the close chord, and forgets that on its next load", async () => {
    const spy = spyOnFrame("win-1");
    load("win-1");
    messageFromPage("win-1", { type: SHELL_CAPABILITIES, navigation: false, closeChord: true });
    spy.mockClear();
    await store.closeFocusedWindow();
    expect(spy.mock.calls[0][0]).toEqual({ type: SHELL_CLOSE_REQUEST });
    expect(api.calls.filter((call) => call.startsWith("closeWindow"))).toEqual([]);

    // A reload is a fresh page that has declared nothing yet.
    load("win-1");
    await store.closeFocusedWindow();
    expect(api.calls).toContain("closeWindow:home:win-1");
  });
});

describe("raising the window the document's focus lands in", () => {
  const CASCADE_RECT = { left: 50, top: 48, width: 600, height: 560 };

  /** A frame in the document that is no page of the shell's. */
  let strayFrame: HTMLIFrameElement | null = null;

  /** The window the active desktop's stack ends with. */
  function topWindowId(): string | undefined {
    const placements = activePlacements(store.getState());
    return placements[placements.length - 1]?.window_id;
  }

  /** Focus ``windowId``'s frame and fire the window's blur. */
  function blurIntoFrameOf(windowId: string): void {
    frameOf(windowId).focus();
    window.dispatchEvent(new Event("blur"));
  }

  beforeEach(() => {
    store.restoreWindow("win-2");
    layer.reconcile();
  });

  afterEach(() => {
    strayFrame?.remove();
    strayFrame = null;
  });

  it("raises the window of an unfocused page the focus landed in, once, from the window's blur", () => {
    const raise = vi.spyOn(store, "raiseWindow");
    frameOf("win-1").focus();
    expect(activeFocusedWindowId(store.getState())).toBe("win-2");
    window.dispatchEvent(new Event("blur"));
    expect(activeFocusedWindowId(store.getState())).toBe("win-1");
    expect(topWindowId()).toBe("win-1");
    tick(2);
    expect(raise).toHaveBeenCalledTimes(1);
  });

  it("raises the window of a frame the focus moved into from another frame, on the next tick", () => {
    frameOf("win-2").focus();
    tick();
    expect(activeFocusedWindowId(store.getState())).toBe("win-2");
    // Fires nothing on the shell's document.
    frameOf("win-1").focus();
    expect(activeFocusedWindowId(store.getState())).toBe("win-2");
    tick();
    expect(activeFocusedWindowId(store.getState())).toBe("win-1");
    expect(topWindowId()).toBe("win-1");
  });

  it("raises nothing for the focused window's frame, nor for a frame that is no page of the shell's", () => {
    frameOf("win-2").focus();
    window.dispatchEvent(new Event("blur"));
    tick();
    expect(activeFocusedWindowId(store.getState())).toBe("win-2");
    strayFrame = document.createElement("iframe");
    document.body.appendChild(strayFrame);
    strayFrame.focus();
    window.dispatchEvent(new Event("blur"));
    tick();
    expect(activeFocusedWindowId(store.getState())).toBe("win-2");
  });

  it("does not raise the window the shell just moved the focus away from", () => {
    frameOf("win-2").focus();
    tick();
    // The focus stays in win-2's page until the next reconcile.
    store.raiseWindow("win-1");
    tick();
    expect(activeFocusedWindowId(store.getState())).toBe("win-1");
    layer.reconcile();
    expect(document.activeElement).toBe(host);
    tick();
    expect(activeFocusedWindowId(store.getState())).toBe("win-1");
  });

  it("raises nothing during a gesture, nor while another window is being moved", () => {
    layer.setGestureActive(true);
    blurIntoFrameOf("win-1");
    tick();
    expect(activeFocusedWindowId(store.getState())).toBe("win-2");
    layer.setGestureActive(false);
    host.focus();
    store.beginWindowMove("win-2", { x: 100, y: 60 });
    blurIntoFrameOf("win-1");
    tick();
    expect(activeFocusedWindowId(store.getState())).toBe("win-2");
  });

  it("raises nothing on a viewed desktop, and says nothing about it", async () => {
    await viewAlicesDesktopAtHalfScale([placementRecord("win-a"), placementRecord("win-b")]);
    renderChrome("win-a", "win-b");
    layer.reconcile();
    expect(activeFocusedWindowId(store.getState())).toBe("win-b");
    blurIntoFrameOf("win-a");
    tick();
    expect(activeFocusedWindowId(store.getState())).toBe("win-b");
    expect(store.toasts.current()).toEqual([]);
  });

  it("raises nothing while the overview is up", () => {
    layer.setOverview({ scaleOf: () => 0.5 });
    layer.reconcile();
    blurIntoFrameOf("win-1");
    tick();
    expect(activeFocusedWindowId(store.getState())).toBe("win-2");
  });

  it("raises nothing for a page that is hidden, being pulled out, or covered", () => {
    store.minimizeWindow("win-1");
    layer.reconcile();
    blurIntoFrameOf("win-1");
    tick();
    expect(activeFocusedWindowId(store.getState())).toBe("win-2");
    store.restoreWindow("win-1");
    store.raiseWindow("win-2");
    layer.reconcile();

    layer.setTornOutWindow("win-1");
    blurIntoFrameOf("win-1");
    tick();
    expect(activeFocusedWindowId(store.getState())).toBe("win-2");
    layer.setTornOutWindow(null);

    drawChromeAt("win-1", CASCADE_RECT);
    drawChromeAt("win-2", CASCADE_RECT);
    layer.reconcile();
    expect(isParked(wrapperOf("win-1"))).toBe(true);
    blurIntoFrameOf("win-1");
    tick();
    expect(activeFocusedWindowId(store.getState())).toBe("win-2");
  });

  it("raises nothing on a touch client, whose unfocused pages take no press", () => {
    store.setThemeMetrics(METRICS, { isPhone: false, isTouch: true });
    layer.reconcile();
    blurIntoFrameOf("win-1");
    tick();
    expect(activeFocusedWindowId(store.getState())).toBe("win-2");
  });

  it("raises nothing for the one page the phone shows", () => {
    layer.setMountPolicy({ kind: "shown", windowId: "win-1", alsoKeep: [] });
    layer.reconcile();
    blurIntoFrameOf("win-1");
    tick();
    expect(activeFocusedWindowId(store.getState())).toBe("win-2");
  });

  it("takes a landing that answers the shell:shown just posted to the page for no press: no raise, the focus handed back by the next tick", () => {
    const raise = vi.spyOn(store, "raiseWindow");
    load("win-1");
    blurIntoFrameOf("win-1");
    expect(raise).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(frameOf("win-1"));
    tick();
    expect(raise).not.toHaveBeenCalled();
    expect(activeFocusedWindowId(store.getState())).toBe("win-2");
    expect(document.activeElement).toBe(host);
  });

  it("hands the focus back on the next tick for a landing the window's blur took for an answer at the last settle tick", () => {
    const raise = vi.spyOn(store, "raiseWindow");
    load("win-1");
    tick(SHELL_PROMPT_SETTLE_TICKS);
    blurIntoFrameOf("win-1");
    expect(raise).not.toHaveBeenCalled();
    tick();
    expect(raise).not.toHaveBeenCalled();
    expect(activeFocusedWindowId(store.getState())).toBe("win-2");
    expect(document.activeElement).toBe(host);
  });

  it("takes a press that comes after a landing the blur took for an answer, once that landing is gone", () => {
    load("win-1");
    tick(SHELL_PROMPT_SETTLE_TICKS);
    blurIntoFrameOf("win-1");
    host.focus();
    tick();
    blurIntoFrameOf("win-1");
    expect(topWindowId()).toBe("win-1");
  });

  it("takes a landing that answers the latest ttyd-focus posted to the page for no press, however late the post", () => {
    const grants = spyOnFrame("win-2");
    // A later grant to win-2 goes out just before the user chooses win-1.
    const scheduleTicks = FOCUS_GRANT_SCHEDULE_MS / FOCUS_WATCH_INTERVAL_MS;
    for (let count = 0; grantsSeen(grants) < 2 && count < scheduleTicks; count += 1) tick();
    expect(grantsSeen(grants)).toBe(2);
    store.raiseWindow("win-1");
    layer.reconcile();
    const raise = vi.spyOn(store, "raiseWindow");
    frameOf("win-2").focus();
    tick();
    expect(raise).not.toHaveBeenCalled();
    expect(activeFocusedWindowId(store.getState())).toBe("win-1");
    expect(document.activeElement).toBe(host);
  });

  it("takes a landing that answers a ttyd-focus posted while the page held the focus from an earlier one for no press", () => {
    const grants = spyOnFrame("win-2");
    vi.advanceTimersByTime(0);
    expect(grantsSeen(grants)).toBe(1);
    frameOf("win-2").focus();
    const scheduleTicks = FOCUS_GRANT_SCHEDULE_MS / FOCUS_WATCH_INTERVAL_MS;
    for (let count = 0; grantsSeen(grants) < 2 && count < scheduleTicks; count += 1) tick();
    expect(grantsSeen(grants)).toBe(2);
    store.raiseWindow("win-1");
    layer.reconcile();
    expect(document.activeElement).toBe(host);
    const raise = vi.spyOn(store, "raiseWindow");
    frameOf("win-2").focus();
    tick();
    expect(raise).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(host);
  });

  it("raises for a landing that comes well after the shell's last message to the page", () => {
    load("win-1");
    pastThePromptSettle();
    blurIntoFrameOf("win-1");
    expect(topWindowId()).toBe("win-1");
  });

  it("raises for a press whose page loads again before the next tick, and leaves the focus in it", () => {
    load("win-1");
    pastThePromptSettle();
    frameOf("win-2").focus();
    tick();
    // The press moves the focus into win-1's page, which goes to another page of its own.
    frameOf("win-1").focus();
    load("win-1");
    tick();
    expect(topWindowId()).toBe("win-1");
    expect(document.activeElement).toBe(frameOf("win-1"));
  });

  it("raises for a press the hold set aside whose page loads again meanwhile, once the hold is over", () => {
    load("win-2");
    pastThePromptSettle();
    blurIntoFrameOf("win-1");
    layer.reconcile();
    expect(topWindowId()).toBe("win-1");
    frameOf("win-2").focus();
    load("win-2");
    tick(WATCHER_RAISE_HOLD_TICKS);
    expect(topWindowId()).toBe("win-2");
    expect(document.activeElement).toBe(frameOf("win-2"));
  });

  it("takes a landing that answers a shell:shown posted to a page while its window was the focused one for no press", () => {
    frameOf("win-2").focus();
    pastThePromptSettle();
    load("win-2");
    store.raiseWindow("win-1");
    layer.reconcile();
    expect(document.activeElement).toBe(host);
    const raise = vi.spyOn(store, "raiseWindow");
    frameOf("win-2").focus();
    tick();
    expect(raise).not.toHaveBeenCalled();
    expect(topWindowId()).toBe("win-1");
    expect(document.activeElement).toBe(host);
  });

  describe.each([
    ["just after a tick", 1],
    ["halfway between two ticks", FOCUS_WATCH_INTERVAL_MS / 2],
    ["just before a tick", FOCUS_WATCH_INTERVAL_MS - 1],
  ])("with the shell's message to a page posted %s", (_phase, postPhaseMs) => {
    /** How long the page takes to answer the message by taking the focus. */
    const ANSWER_DELAY_MS = 5;

    /** Tell win-1's page it is shown, ``postPhaseMs`` after a tick. */
    function postThePrompt(): void {
      tick();
      vi.advanceTimersByTime(postPhaseMs);
      load("win-1");
    }

    /** Let the time from the post to just after the watcher's tick ``ticks`` past it go by. */
    function pastTheTick(ticks: number): void {
      vi.advanceTimersByTime(ticks * FOCUS_WATCH_INTERVAL_MS - postPhaseMs + 1);
    }

    it("takes the page's answer for no press when the focus comes from another frame", () => {
      const raise = vi.spyOn(store, "raiseWindow");
      frameOf("win-2").focus();
      postThePrompt();
      vi.advanceTimersByTime(ANSWER_DELAY_MS);
      frameOf("win-1").focus();
      tick();
      expect(raise).not.toHaveBeenCalled();
      expect(document.activeElement).toBe(host);
    });

    it("takes the page's answer for no press when the focus comes from the shell's own document", () => {
      const raise = vi.spyOn(store, "raiseWindow");
      postThePrompt();
      vi.advanceTimersByTime(ANSWER_DELAY_MS);
      blurIntoFrameOf("win-1");
      tick();
      expect(raise).not.toHaveBeenCalled();
      expect(document.activeElement).toBe(host);
    });

    it("raises for a press from another frame that the watcher reads one tick past the settle", () => {
      frameOf("win-2").focus();
      postThePrompt();
      pastTheTick(SHELL_PROMPT_SETTLE_TICKS);
      frameOf("win-1").focus();
      expect(topWindowId()).toBe("win-2");
      tick();
      expect(topWindowId()).toBe("win-1");
    });

    it("raises for a press from the shell's own document one tick past the settle", () => {
      postThePrompt();
      pastTheTick(SHELL_PROMPT_SETTLE_TICKS + 1);
      blurIntoFrameOf("win-1");
      expect(topWindowId()).toBe("win-1");
    });
  });

  describe("by the clock, in milliseconds and counted ticks", () => {
    it("reads which frame holds the focus once every 100 ms", () => {
      // The store refuses the raise while win-2 is moved, so every read of the landing asks for it again.
      store.beginWindowMove("win-2", { x: 100, y: 60 });
      frameOf("win-1").focus();
      const raise = vi.spyOn(store, "raiseWindow");
      vi.advanceTimersByTime(99);
      expect(raise).toHaveBeenCalledTimes(0);
      vi.advanceTimersByTime(1);
      expect(raise).toHaveBeenCalledTimes(1);
      vi.advanceTimersByTime(900);
      expect(raise).toHaveBeenCalledTimes(10);
    });

    it("takes a landing read at the second tick after the shell's message to the page for the page's answer", () => {
      const raise = vi.spyOn(store, "raiseWindow");
      frameOf("win-2").focus();
      load("win-1");
      vi.advanceTimersByTime(150);
      frameOf("win-1").focus();
      vi.advanceTimersByTime(50);
      expect(raise).not.toHaveBeenCalled();
      expect(document.activeElement).toBe(host);
    });

    it("raises for a landing read at the third tick after the shell's message to the page", () => {
      frameOf("win-2").focus();
      load("win-1");
      vi.advanceTimersByTime(250);
      frameOf("win-1").focus();
      expect(topWindowId()).toBe("win-2");
      vi.advanceTimersByTime(50);
      expect(topWindowId()).toBe("win-1");
    });

    it("raises the next window at the third tick after a raise of the watcher's, and at neither tick before it", () => {
      frameOf("win-2").focus();
      frameOf("win-1").focus();
      vi.advanceTimersByTime(100);
      expect(topWindowId()).toBe("win-1");
      layer.reconcile();
      frameOf("win-2").focus();
      vi.advanceTimersByTime(299);
      expect(topWindowId()).toBe("win-1");
      vi.advanceTimersByTime(1);
      expect(topWindowId()).toBe("win-2");
    });
  });

  it("detects a press in a page the shell handed the focus back from", () => {
    load("win-1");
    blurIntoFrameOf("win-1");
    tick();
    expect(document.activeElement).toBe(host);
    pastThePromptSettle();
    blurIntoFrameOf("win-1");
    expect(topWindowId()).toBe("win-1");
  });

  it("holds a second raise off for a while after one, and applies the landing that came meanwhile once the hold is over", () => {
    blurIntoFrameOf("win-1");
    layer.reconcile();
    expect(topWindowId()).toBe("win-1");
    frameOf("win-2").focus();
    tick(WATCHER_RAISE_HOLD_TICKS - 1);
    expect(topWindowId()).toBe("win-1");
    tick();
    expect(topWindowId()).toBe("win-2");
  });

  it("starts no hold for a raise the store refused, and applies the landing as soon as the store takes it", () => {
    store.beginWindowMove("win-2", { x: 100, y: 60 });
    blurIntoFrameOf("win-1");
    tick();
    expect(topWindowId()).toBe("win-2");
    store.endWindowMove({ x: 100, y: 60 });
    tick();
    expect(topWindowId()).toBe("win-1");
  });

  it("posts the focused window's page no focus grant while a press whose raise the store refuses waits in another window's page", () => {
    blurIntoFrameOf("win-1");
    layer.reconcile();
    const grants = spyOnFrame("win-1");
    vi.advanceTimersByTime(0);
    expect(grantsSeen(grants)).toBe(1);
    frameOf("win-2").focus();
    // The store refuses the raise the hold put off, so win-1 stays the window the grants are for.
    store.beginWindowMove("win-1", { x: 100, y: 60 });
    vi.advanceTimersByTime(FOCUS_GRANT_SCHEDULE_MS);
    expect(grantsSeen(grants)).toBe(1);
    expect(topWindowId()).toBe("win-1");
    store.endWindowMove({ x: 100, y: 60 });
    tick();
    expect(topWindowId()).toBe("win-2");
  });

  it("posts no focus grant that falls due between a press into another window's page and the tick that reads it", () => {
    store.raiseWindow("win-1");
    layer.reconcile();
    const grants = spyOnFrame("win-1");
    frameOf("win-2").focus();
    vi.advanceTimersByTime(0);
    expect(grantsSeen(grants)).toBe(0);
    expect(document.activeElement).toBe(frameOf("win-2"));
    tick();
    expect(topWindowId()).toBe("win-2");
    vi.advanceTimersByTime(FOCUS_GRANT_SCHEDULE_MS);
    expect(grantsSeen(grants)).toBe(0);
  });

  it("posts the grants all the same while the focus rests in a page the shell raises nothing for", () => {
    // A page that answers the shell's own message.
    store.raiseWindow("win-1");
    layer.reconcile();
    const grants = spyOnFrame("win-1");
    load("win-2");
    frameOf("win-2").focus();
    vi.advanceTimersByTime(0);
    expect(grantsSeen(grants)).toBe(1);

    // A page the window in front covers.
    drawChromeAt("win-1", CASCADE_RECT);
    drawChromeAt("win-2", CASCADE_RECT);
    store.raiseWindow("win-2");
    layer.reconcile();
    expect(isParked(wrapperOf("win-1"))).toBe(true);
    const laterGrants = spyOnFrame("win-2");
    frameOf("win-1").focus();
    vi.advanceTimersByTime(0);
    expect(grantsSeen(laterGrants)).toBe(1);
  });

  it("raises a few times, not on every tick, for two pages that each take the focus back when they lose it", () => {
    const raise = vi.spyOn(store, "raiseWindow");
    const ticks = 6 * WATCHER_RAISE_HOLD_TICKS;
    for (let count = 0; count < ticks; count += 1) {
      const unfocused = topWindowId() === "win-1" ? "win-2" : "win-1";
      frameOf(unfocused).focus();
      tick();
      layer.reconcile();
    }
    expect(raise.mock.calls.length).toBeGreaterThan(1);
    expect(raise.mock.calls.length).toBeLessThanOrEqual(ticks / WATCHER_RAISE_HOLD_TICKS + 1);
  });

  it("reads nothing from the store in a tick while the focus rests in the focused window's page or outside every frame", () => {
    vi.advanceTimersByTime(FOCUS_GRANT_SCHEDULE_MS);
    const getState = vi.spyOn(store, "getState");
    host.focus();
    tick();
    frameOf("win-2").focus();
    tick();
    expect(getState).not.toHaveBeenCalled();
    tick();
    expect(getState).not.toHaveBeenCalled();
  });

  it("keeps one watcher timer, and stops the watcher of a layer that a newer one replaces", () => {
    vi.advanceTimersByTime(FOCUS_GRANT_SCHEDULE_MS);
    const timers = vi.getTimerCount();
    tick(2);
    expect(vi.getTimerCount()).toBe(timers);
    new LivePagesLayer(host, store, PAGE_OPTIONS).start();
    expect(vi.getTimerCount()).toBe(timers);
    // The newer layer has no pages yet, so only the replaced layer's watcher could raise for this landing.
    frameOf("win-1").focus();
    tick();
    expect(activeFocusedWindowId(store.getState())).toBe("win-2");
  });

  it("stops its watcher when the layer is stopped, and raises for no landing after", () => {
    vi.advanceTimersByTime(FOCUS_GRANT_SCHEDULE_MS);
    const timers = vi.getTimerCount();
    layer.stop();
    expect(vi.getTimerCount()).toBe(timers - 1);
    blurIntoFrameOf("win-1");
    tick();
    expect(activeFocusedWindowId(store.getState())).toBe("win-2");
  });

  it("leaves the watcher of the layer that replaced it running when a replaced layer is stopped", () => {
    vi.advanceTimersByTime(FOCUS_GRANT_SCHEDULE_MS);
    const timers = vi.getTimerCount();
    const newer = new LivePagesLayer(host, store, PAGE_OPTIONS);
    newer.start();
    layer.stop();
    expect(vi.getTimerCount()).toBe(timers);
    newer.stop();
    expect(vi.getTimerCount()).toBe(timers - 1);
  });
});

describe("the focus grants a raised window's page is sent", () => {
  beforeEach(() => {
    store.restoreWindow("win-2");
    layer.reconcile();
  });

  it("posts the focused window's page the grant three times over the retry schedule", () => {
    const grants = spyOnFrame("win-2");
    vi.advanceTimersByTime(0);
    expect(grantsSeen(grants)).toBe(1);
    vi.advanceTimersByTime(1000);
    expect(grantsSeen(grants)).toBe(3);
  });

  it("drops the grants still pending for a window when another window becomes the focused one", () => {
    const grants = spyOnFrame("win-2");
    vi.advanceTimersByTime(100);
    expect(grantsSeen(grants)).toBe(1);
    store.raiseWindow("win-1");
    layer.reconcile();
    const nextGrants = spyOnFrame("win-1");
    vi.advanceTimersByTime(FOCUS_GRANT_SCHEDULE_MS);
    expect(grantsSeen(grants)).toBe(1);
    expect(grantsSeen(nextGrants)).toBe(3);
  });

  it("posts nothing for a window the store has raised another over, before the layer has reconciled", () => {
    const grants = spyOnFrame("win-2");
    vi.advanceTimersByTime(100);
    store.raiseWindow("win-1");
    vi.advanceTimersByTime(FOCUS_GRANT_SCHEDULE_MS);
    expect(grantsSeen(grants)).toBe(1);
  });

  it("drops the pending grants of a layer that a newer one replaces", () => {
    const grants = spyOnFrame("win-2");
    vi.advanceTimersByTime(100);
    new LivePagesLayer(host, store, PAGE_OPTIONS).start();
    vi.advanceTimersByTime(FOCUS_GRANT_SCHEDULE_MS);
    expect(grantsSeen(grants)).toBe(1);
  });
});

describe("the phone's mount policy", () => {
  it("lays the shown page over the whole host, keeps the kept one hidden, and destroys every other", () => {
    expect(layer.hasPage("win-1")).toBe(true);
    // win-2 is minimized for this client, which a phone does not read: shown is shown.
    layer.setMountPolicy({ kind: "shown", windowId: "win-2", alsoKeep: ["win-3"] });
    layer.reconcile();
    expect(layer.hasPage("win-1")).toBe(false);
    const shown = wrapperOf("win-2");
    expect(isParked(shown)).toBe(false);
    expect([shown.style.left, shown.style.top, shown.style.width, shown.style.height]).toEqual([
      "0px",
      "0px",
      "1000px",
      "800px",
    ]);
    expect(shown.style.pointerEvents).toBe("auto");
    // The kept page is created even though it has never been shown, and stays out of sight.
    expect(isParked(wrapperOf("win-3"))).toBe(true);
  });

  it("gives a window shown again a fresh page, while the kept page lives on across the switch", () => {
    layer.setMountPolicy({ kind: "shown", windowId: "win-1", alsoKeep: ["win-3"] });
    layer.reconcile();
    const firstFrame = frameOf("win-1");
    const keptFrame = frameOf("win-3");
    layer.setMountPolicy({ kind: "shown", windowId: "win-2", alsoKeep: ["win-3"] });
    layer.reconcile();
    layer.setMountPolicy({ kind: "shown", windowId: "win-1", alsoKeep: ["win-3"] });
    layer.reconcile();
    expect(frameOf("win-1")).not.toBe(firstFrame);
    expect(frameOf("win-3")).toBe(keptFrame);
    // Home: no page shown, the kept one still mounted.
    layer.setMountPolicy({ kind: "shown", windowId: null, alsoKeep: ["win-3"] });
    layer.reconcile();
    expect(layer.hasPage("win-1")).toBe(false);
    expect(frameOf("win-3")).toBe(keptFrame);
  });
});

describe("the overview (the multiplayer spec, section 8.2)", () => {
  /** Stand in for the overview's tiles: one content box per window of the desktop, minimized ones included. */
  function renderTiles(): void {
    // The windows layer is gone while the overview is up; its tiles stand in, outside the stage.
    windows.innerHTML = "";
    overviewTiles.innerHTML = "";
    for (const [windowId, left] of [
      ["win-1", 40],
      ["win-2", 340],
      ["win-3", 640],
    ] as const) {
      const tile = document.createElement("div");
      tile.setAttribute("data-window-id", windowId);
      const content = document.createElement("div");
      content.setAttribute("data-window-content", "");
      content.getBoundingClientRect = () => ({ left, top: 100, width: 250, height: 200 }) as DOMRect;
      tile.appendChild(content);
      overviewTiles.appendChild(tile);
    }
  }

  it("lays every page of the active desktop over its tile at the tile's scale, inert and above the scrim, the minimized ones too", () => {
    renderTiles();
    layer.setOverview({ scaleOf: (windowId) => (windowId === "win-3" ? null : 0.5) });
    layer.reconcile();
    // win-2 is minimized and had no page; the overview gives it one.
    for (const windowId of ["win-1", "win-2"]) {
      const wrapper = wrapperOf(windowId);
      expect(wrapper.style.visibility).toBe("");
      expect(wrapper.style.width).toBe("500px");
      expect(wrapper.style.height).toBe("400px");
      expect(wrapper.style.transform).toBe("scale(0.5)");
      expect(wrapper.style.transformOrigin).toBe("top left");
      expect(wrapper.style.pointerEvents).toBe("none");
      expect(wrapper.style.zIndex).toBe("calc(var(--z-sticky) + 1)");
    }
    expect(wrapperOf("win-1").style.left).toBe("40px");
    expect(wrapperOf("win-2").style.left).toBe("340px");
    // A window the overview has no tile for stays hidden, and nothing holds the frame focus.
    expect(host.querySelector('iframe[data-live-page="win-3"]')).toBeNull();
    expect(document.activeElement).not.toBe(frameOf("win-1"));

    // The overview goes: the pages lie over their windows again, at their own size and stacking.
    layer.setOverview(null);
    renderChrome();
    layer.reconcile();
    const wrapper = wrapperOf("win-1");
    expect(wrapper.style.transform).toBe("");
    expect(wrapper.style.width).toBe("500px");
    expect(wrapper.style.left).toBe("100px");
    expect(wrapper.style.pointerEvents).toBe("auto");
    expect(wrapper.style.zIndex).toBe("5");
    expect(isParked(wrapperOf("win-2"))).toBe(true);
  });

  it("keeps a page at its tile's scale when a tile's resize re-places it", () => {
    renderTiles();
    layer.setOverview({ scaleOf: () => 0.5 });
    layer.reconcile();
    const content = overviewTiles.querySelector('[data-window-id="win-1"] [data-window-content]') as HTMLElement;
    content.getBoundingClientRect = () => ({ left: 40, top: 100, width: 300, height: 240 }) as DOMRect;
    layer.placePage("win-1");
    const wrapper = wrapperOf("win-1");
    expect([wrapper.style.width, wrapper.style.height, wrapper.style.transform]).toEqual([
      "600px",
      "480px",
      "scale(0.5)",
    ]);
  });

  it("takes the moved window's compositor layer away when the move ends with the overview up", () => {
    layer.reconcile();
    store.beginWindowMove("win-1", { x: 300, y: 60 });
    layer.reconcile();
    expect(wrapperOf("win-1").style.willChange).toBe("transform");
    renderTiles();
    layer.setOverview({ scaleOf: () => 0.5 });
    layer.reconcile();
    store.endWindowMove({ x: 300, y: 60 });
    layer.reconcile();
    expect(wrapperOf("win-1").style.willChange).toBe("");
  });

  it("hides a page whose tile scale is not positive rather than sizing it by a zero scale", () => {
    renderTiles();
    layer.setOverview({ scaleOf: (windowId) => (windowId === "win-1" ? 0 : 0.5) });
    layer.reconcile();
    expect(isParked(wrapperOf("win-1"))).toBe(true);
    expect(wrapperOf("win-2").style.visibility).toBe("");
  });
});

describe("input reports for the heartbeat (the app-usage-signals spec, sections 6.3 and 6.4)", () => {
  /** Focus landing in one page is reported at most once per throttle interval: move the clock past it. */
  function pastTheFocusThrottle(): void {
    vi.setSystemTime(Date.now() + 21_000);
  }

  it("notes a page's shell:input with its window and app", () => {
    store.restoreWindow("win-2");
    layer.reconcile();
    messageFromPage("win-1", { type: SHELL_INPUT });
    messageFromPage("win-2", { type: SHELL_INPUT });
    messageFromPage("win-1", { type: SHELL_INPUT });
    expect(pendingInputReportsForTesting()).toEqual([
      { window_id: "win-1", app: "docs", kind: "input" },
      { window_id: "win-2", app: "notes", kind: "input" },
    ]);
  });

  it("notes focus landing in a page that binds no contract, from a raise and from the window's blur, and not in one that does", () => {
    // The setup reconciled with win-1 focused before its page had loaded: a page that may still declare the
    // contract, so the shell's own raise at boot says nothing.
    expect(pendingInputReportsForTesting()).toEqual([]);

    // Restoring win-2 raises a page that has not loaded either; once loaded and declaring nothing, a later raise
    // of it is a landing the shell made.
    store.restoreWindow("win-2");
    layer.reconcile();
    expect(pendingInputReportsForTesting()).toEqual([]);
    load("win-2");
    store.raiseWindow("win-1");
    layer.reconcile();
    store.raiseWindow("win-2");
    layer.reconcile();
    expect(activeFocusedWindowId(store.getState())).toBe("win-2");
    expect(pendingInputReportsForTesting()).toEqual([{ window_id: "win-2", app: "notes", kind: "focus" }]);

    // The person clicks into win-1's frame: the window loses focus to it, loaded or not. A second click within
    // the throttle is not reported again; past it, it is.
    frameOf("win-1").focus();
    expect(document.activeElement).toBe(frameOf("win-1"));
    window.dispatchEvent(new Event("blur"));
    expect(pendingInputReportsForTesting()).toEqual([
      { window_id: "win-2", app: "notes", kind: "focus" },
      { window_id: "win-1", app: "docs", kind: "focus" },
    ]);
    resetPresenceForTesting();
    window.dispatchEvent(new Event("blur"));
    expect(pendingInputReportsForTesting()).toEqual([]);
    pastTheFocusThrottle();
    window.dispatchEvent(new Event("blur"));
    expect(pendingInputReportsForTesting()).toEqual([{ window_id: "win-1", app: "docs", kind: "focus" }]);

    // A page that has declared its capabilities reports its own input: neither the window losing focus to it nor
    // a raise of it says anything more.
    resetPresenceForTesting();
    pastTheFocusThrottle();
    load("win-1");
    messageFromPage("win-1", { type: SHELL_CAPABILITIES, navigation: false });
    window.dispatchEvent(new Event("blur"));
    store.raiseWindow("win-1");
    layer.reconcile();
    expect(activeFocusedWindowId(store.getState())).toBe("win-1");
    expect(pendingInputReportsForTesting()).toEqual([]);
    messageFromPage("win-1", { type: SHELL_INPUT });
    expect(pendingInputReportsForTesting()).toEqual([{ window_id: "win-1", app: "docs", kind: "input" }]);

    // The window losing focus to anything but a page's frame (the launcher's field) is nobody's landing.
    resetPresenceForTesting();
    host.focus();
    window.dispatchEvent(new Event("blur"));
    expect(pendingInputReportsForTesting()).toEqual([]);
  });

  it("notes no use for a landing that answers a message the shell just posted to the page, and notes a landing past it", () => {
    load("win-1");
    frameOf("win-1").focus();
    window.dispatchEvent(new Event("blur"));
    expect(pendingInputReportsForTesting()).toEqual([]);

    pastThePromptSettle();
    window.dispatchEvent(new Event("blur"));
    expect(pendingInputReportsForTesting()).toEqual([{ window_id: "win-1", app: "docs", kind: "focus" }]);
  });

  it("forgets a page's capabilities on its reload, since a fresh page has declared nothing yet", () => {
    messageFromPage("win-1", { type: SHELL_CAPABILITIES, navigation: false });
    resetPresenceForTesting();
    pastTheFocusThrottle();
    frameOf("win-1").focus();
    window.dispatchEvent(new Event("blur"));
    expect(pendingInputReportsForTesting()).toEqual([]);

    load("win-1");
    pastThePromptSettle();
    window.dispatchEvent(new Event("blur"));
    expect(pendingInputReportsForTesting()).toEqual([{ window_id: "win-1", app: "docs", kind: "focus" }]);
  });
});
