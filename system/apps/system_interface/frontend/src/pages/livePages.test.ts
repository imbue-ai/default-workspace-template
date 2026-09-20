// @vitest-environment jsdom
/**
 * The live-page layer against a real store over the fake shell: pages are created for the shown
 * windows of the active desktop (never for a window settling on another client's open), laid
 * over their windows' content boxes in the interleaved stacking order, inert unless focused,
 * hidden when minimized, destroyed when closed; they are greeted after every load, told shown
 * and hidden, and follow their windows' stored paths in place or by reload; and their own
 * ``shell:location``, ``shell:focused``, and ``shell:open`` reach the store.
 */
import "../testing/dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  SHELL_CAPABILITIES,
  SHELL_CLOSE_REQUEST,
  SHELL_FOCUSED,
  SHELL_HANDSHAKE,
  SHELL_HIDDEN,
  SHELL_LOCATION,
  SHELL_NAVIGATE,
  SHELL_OPEN,
  SHELL_SHOWN,
} from "@imbue/workspace-ui/src/app_contract";
import { initEmbedderRelay, resetEmbedderRelayForTesting } from "../relay";
import { activeFocusedWindowId } from "../reducers/desktopState";
import { DesktopStore } from "../store/DesktopStore";
import { FakeDesktopApi, FakeDesktopSocket, settle } from "../testing/fakeShell";
import { appRecord, desktopRecord, placementRecord, themeMetricsRecord, windowRecord } from "../testing/records";
import { LivePagesLayer } from "./livePages";

const METRICS = themeMetricsRecord();
const CLIENT = "client-1";
const NO_LINK = { desktopId: null, open: null, launch: null };
const docs = appRecord("docs", { url: "http://127.0.0.1:7001" });
const notes = appRecord("notes", { url: "http://127.0.0.1:7002" });

let api: FakeDesktopApi;
let socket: FakeDesktopSocket;
let store: DesktopStore;
let backdrop: HTMLElement;
let host: HTMLElement;
let windows: HTMLElement;
let layer: LivePagesLayer;

/** Stand in for the window chrome the views render: one content box per shown window at a fixed spot. */
function renderChrome(): void {
  windows.innerHTML = "";
  for (const windowId of ["win-1", "win-2", "win-3"]) {
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

/** The messages a page's window received, by type. */
function spyOnFrame(windowId: string): ReturnType<typeof vi.fn> {
  const frame = frameOf(windowId);
  const spy = vi.fn();
  const target = frame.contentWindow as Window;
  target.postMessage = spy as unknown as Window["postMessage"];
  return spy;
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

beforeEach(async () => {
  api = new FakeDesktopApi();
  socket = new FakeDesktopSocket();
  api.desktops = [
    desktopRecord("home", {
      windows: [
        windowRecord("win-1", "docs", "/?doc=1"),
        windowRecord("win-2", "notes", "/b"),
        windowRecord("win-3", "docs", "/new", { is_settling: true }),
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
    modes: { isCompact: false, isTouch: false },
    redraw: () => undefined,
    notify: () => undefined,
    reloadInterface: () => undefined,
  });
  store.setBackdropSize({ width: 1000, height: 800 });
  backdrop = document.createElement("div");
  backdrop.getBoundingClientRect = () => ({ left: 0, top: 0, width: 1000, height: 800 }) as DOMRect;
  host = document.createElement("div");
  host.getBoundingClientRect = () => ({ left: 0, top: 0, width: 1000, height: 800 }) as DOMRect;
  windows = document.createElement("div");
  backdrop.append(host, windows);
  document.body.appendChild(backdrop);
  resetEmbedderRelayForTesting();
  initEmbedderRelay();
  layer = new LivePagesLayer(host, store, { host: "127.0.0.1:8000", protocol: "http:" });
  layer.start();
  await store.start(NO_LINK);
  socket.deliver().onAppsUpdated([docs, notes]);
  renderChrome();
  layer.reconcile();
});

afterEach(() => {
  resetEmbedderRelayForTesting();
  backdrop.remove();
});

describe("creating and positioning", () => {
  it("creates a page for each shown window at its app's origin plus its path, over its content box", () => {
    const frame = frameOf("win-1");
    expect(frame.getAttribute("src")).toBe("http://127.0.0.1:7001/?doc=1");
    expect(frame.getAttribute("sandbox")).toContain("allow-same-origin");
    const wrapper = wrapperOf("win-1");
    expect(wrapper.style.left).toBe("100px");
    expect(wrapper.style.top).toBe("60px");
    expect(wrapper.style.width).toBe("500px");
    expect(wrapper.style.display).toBe("");
    // Placements: win-3 (missing, minimized) at 0, win-2 minimized at 1, win-1 focused at 2.
    expect(wrapper.style.zIndex).toBe("5");
    expect(wrapper.style.pointerEvents).toBe("auto");
  });

  it("creates no page for a minimized window, nor for one settling on another client's open", () => {
    expect(layer.hasPage("win-2")).toBe(false);
    expect(layer.hasPage("win-3")).toBe(false);
    store.restoreWindow("win-2");
    layer.reconcile();
    expect(layer.hasPage("win-2")).toBe(true);
    // The settling window's restore waits; still no page.
    store.restoreWindow("win-3");
    layer.reconcile();
    expect(layer.hasPage("win-3")).toBe(false);
  });

  it("creates the page of a settling window the shell placed in this client's layout (an agent's open)", async () => {
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

  it("makes every page but the focused one inert, and all of them during a gesture", () => {
    store.restoreWindow("win-2");
    layer.reconcile();
    expect(wrapperOf("win-2").style.pointerEvents).toBe("auto");
    expect(wrapperOf("win-1").style.pointerEvents).toBe("none");
    layer.setGestureActive(true);
    expect(wrapperOf("win-2").style.pointerEvents).toBe("none");
    layer.setGestureActive(false);
    expect(wrapperOf("win-2").style.pointerEvents).toBe("auto");
  });

  it("hides a page when its window is minimized, keeping the same frame, and destroys it on close", async () => {
    const frame = frameOf("win-1");
    store.minimizeWindow("win-1");
    layer.reconcile();
    expect(wrapperOf("win-1").style.display).toBe("none");
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
    expect(wrapperOf("win-1").style.display).toBe("none");
    await store.switchDesktop("home");
    layer.reconcile();
    expect(frameOf("win-1")).toBe(frame);
    expect(wrapperOf("win-1").style.display).toBe("");
  });

  it("hides a stopped app's page and reloads it at the window's stored path once the app runs again", async () => {
    await navigateInPage("win-1", "/?doc=2");
    const reloads = spyOnSrc("win-1");
    socket.deliver().onAppsUpdated([{ ...docs, is_running: false }, notes]);
    layer.reconcile();
    expect(wrapperOf("win-1").style.display).toBe("none");
    expect(reloads).toEqual([]);
    socket.deliver().onAppsUpdated([docs, notes]);
    layer.reconcile();
    expect(wrapperOf("win-1").style.display).toBe("");
    expect(reloads).toEqual(["http://127.0.0.1:7001/?doc=2"]);
  });
});

describe("the contract", () => {
  it("greets a page after every load with the window, desktop, and path, then says shown", () => {
    const spy = spyOnFrame("win-1");
    load("win-1");
    expect(spy.mock.calls.map((call) => call[0])).toEqual([
      {
        type: SHELL_HANDSHAKE,
        clientId: CLIENT,
        windowId: "win-1",
        desktopId: "home",
        path: "/?doc=1",
      },
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
        path: "/?doc=1",
      },
      { type: SHELL_HIDDEN },
    ]);
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
    await store.openWindowAt("notes", "/c", null, "new");
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
    const [home, work] = api.desktops;
    const independent = windowRecord("win-4", "docs", "/", { is_pinned: true, scope: "independent" });
    api.desktops = [{ ...home, windows: [...home.windows, independent] }, work];
    api.windowPaths.set(`${CLIENT}/win-4`, { path: "/?doc=7", title: "Seven" });
    api.writeLayout("home", CLIENT, {
      updated_at: null,
      placements: [
        placementRecord("win-2", { is_minimized: true }),
        placementRecord("win-1"),
        placementRecord("win-4"),
      ],
    });
    socket.deliver().onDesktopsUpdated(api.desktops);
    socket.deliver().onPlacementsUpdated({ desktopId: "home", clientId: CLIENT, saveId: "save-shell" });
    await settle();
    renderChrome();
    windows.appendChild(windows.lastElementChild!.cloneNode(true));
    (windows.lastElementChild as HTMLElement).setAttribute("data-window-id", "win-4");
    (windows.lastElementChild!.firstElementChild as HTMLElement).getBoundingClientRect = () =>
      ({ left: 100, top: 60, width: 500, height: 400 }) as DOMRect;
    layer.reconcile();
    expect(frameOf("win-4").getAttribute("src")).toBe("http://127.0.0.1:7001/?doc=7");
    expect(frameOf("win-4").title).toBe("Seven");
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

  it("raises the window of a page that says it took focus", () => {
    store.restoreWindow("win-2");
    layer.reconcile();
    expect(activeFocusedWindowId(store.getState())).toBe("win-2");
    messageFromPage("win-1", { type: SHELL_FOCUSED });
    expect(activeFocusedWindowId(store.getState())).toBe("win-1");
  });

  it("opens a page's shell:open on its own app, and warns about the address form", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    messageFromPage("win-1", { type: SHELL_OPEN, path: "/?doc=3", ifPresent: "new" });
    await settle();
    expect(api.calls).toContain("openWindow:home:docs:/?doc=3:new:-");
    messageFromPage("win-1", { type: SHELL_OPEN, address: "app:docs?instance=x" });
    await settle();
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("shell:open ignored"));
    warn.mockRestore();
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
});
