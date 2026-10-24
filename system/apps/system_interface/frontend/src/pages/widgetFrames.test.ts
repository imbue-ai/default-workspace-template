// @vitest-environment jsdom
/**
 * The widget frames layer against a real store over the fake shell: a frame per enabled widget at its app's widget
 * path, laid over its chrome's content box (or kept loaded out of sight until there is one), in the backdrop's host
 * or the taskbar's; greeted after every load and told its mode; and its page's own size, hidden, press, dismiss,
 * menu, tooltip, and drag messages reaching the store or the layer's callbacks.
 */
import "../testing/dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  SHELL_HANDSHAKE,
  SHELL_WIDGET_DISMISS,
  SHELL_WIDGET_DRAG,
  SHELL_WIDGET_HIDDEN,
  SHELL_WIDGET_MENU,
  SHELL_WIDGET_MODE,
  SHELL_WIDGET_PRESS,
  SHELL_WIDGET_SIZE,
  SHELL_WIDGET_TOOLTIP,
} from "@imbue/workspace-ui/src/app_contract";
import { hideTooltipAtRect, showTooltipAtRect } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { initEmbedderRelay, resetEmbedderRelayForTesting } from "../relay";
import { DesktopStore } from "../store/DesktopStore";
import { FakeDesktopApi, FakeDesktopSocket, offerApps, settle } from "../testing/fakeShell";
import { appRecord, clientRecord, desktopRecord, themeMetricsRecord } from "../testing/records";
import { WidgetFramesLayer } from "./widgetFrames";
import type { WidgetDragStep } from "./widgetFrames";

vi.mock("@imbue/workspace-ui/src/components/hoverTooltip", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@imbue/workspace-ui/src/components/hoverTooltip")>()),
  showTooltipAtRect: vi.fn(),
  hideTooltipAtRect: vi.fn(),
}));

const METRICS = themeMetricsRecord();
const CLIENT = "client-1";
const NO_LINK = { desktopId: null, open: null, launch: null };
const WIDGET = { path: "/widget", default_enabled: true, tray: true, toolbar: true };
const gauge = appRecord("gauge", { url: "http://127.0.0.1:7005", widget: WIDGET });
const meter = appRecord("meter", { url: "http://127.0.0.1:7006", widget: { ...WIDGET, path: "/meter" } });

let api: FakeDesktopApi;
let socket: FakeDesktopSocket;
let store: DesktopStore;
let backdrop: HTMLElement;
let host: HTMLElement;
let taskbar: HTMLElement;
let trayHost: HTMLElement;
let layer: WidgetFramesLayer;
let drags: [string, WidgetDragStep][];
let menus: [string, { x: number; y: number }, HTMLIFrameElement][];

type Box = { left: number; top: number; width: number; height: number };

/** Stand in for a widget's chrome: an element carrying ``attribute="app"`` with a content box at ``box``. */
function drawChrome(parent: HTMLElement, attribute: string, app: string, box: Box): HTMLElement {
  const chrome = document.createElement("div");
  chrome.setAttribute(attribute, app);
  const content = document.createElement("div");
  content.setAttribute("data-widget-content", "");
  content.getBoundingClientRect = () => box as DOMRect;
  chrome.appendChild(content);
  parent.appendChild(chrome);
  return chrome;
}

function frameIn(container: HTMLElement, app: string): HTMLIFrameElement | null {
  return container.querySelector<HTMLIFrameElement>(`iframe[data-widget-frame="${app}"]`);
}

function frameOf(app: string): HTMLIFrameElement {
  const frame = frameIn(host, app) ?? frameIn(trayHost, app);
  if (frame === null) throw new Error(`no frame for ${app}`);
  return frame;
}

function wrapperOf(app: string): HTMLElement {
  return frameOf(app).parentElement as HTMLElement;
}

function boxOf(element: HTMLElement): string[] {
  const style = element.style;
  return [style.left, style.top, style.width, style.height, style.visibility, style.pointerEvents];
}

/** The messages a widget's page received. */
function spyOnFrame(app: string): ReturnType<typeof vi.fn> {
  const spy = vi.fn();
  (frameOf(app).contentWindow as Window).postMessage = spy as unknown as Window["postMessage"];
  return spy;
}

function posted(spy: ReturnType<typeof vi.fn>): Record<string, unknown>[] {
  return spy.mock.calls.map((call) => call[0] as Record<string, unknown>);
}

function load(app: string): void {
  frameOf(app).dispatchEvent(new Event("load"));
}

/** A message from the page inside ``frame``, as the relay sees it. */
function messageFrom(frame: HTMLIFrameElement, data: Record<string, unknown>): void {
  window.dispatchEvent(
    new MessageEvent("message", { data, source: frame.contentWindow, origin: window.location.origin }),
  );
}

function messageFromWidget(app: string, data: Record<string, unknown>): void {
  messageFrom(frameOf(app), data);
}

/** Where the frame sits in the shell's viewport, for the points its page reports. */
function placeFrameAt(app: string, left: number, top: number): void {
  frameOf(app).getBoundingClientRect = () => ({ left, top, width: 200, height: 80 }) as DOMRect;
}

beforeEach(async () => {
  vi.mocked(showTooltipAtRect).mockClear();
  vi.mocked(hideTooltipAtRect).mockClear();
  api = new FakeDesktopApi();
  socket = new FakeDesktopSocket();
  api.apps = [gauge, meter];
  api.widgetSwitches = [
    { app: "gauge", is_enabled: true },
    { app: "meter", is_enabled: false },
  ];
  api.desktops = [desktopRecord("home"), desktopRecord("work")];
  api.clients = [
    clientRecord(CLIENT, {
      active_desktop: "home",
      widgets: { gauge: { position: null, mode: "collapsed", placement: "desktop" } },
    }),
  ];
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
  backdrop = document.createElement("div");
  host = document.createElement("div");
  host.getBoundingClientRect = () => ({ left: 0, top: 0, width: 1000, height: 800 }) as DOMRect;
  backdrop.appendChild(host);
  taskbar = document.createElement("div");
  trayHost = document.createElement("div");
  trayHost.getBoundingClientRect = () => ({ left: 0, top: 800, width: 1000, height: 48 }) as DOMRect;
  taskbar.appendChild(trayHost);
  document.body.append(backdrop, taskbar);
  resetEmbedderRelayForTesting();
  initEmbedderRelay();
  drags = [];
  menus = [];
  layer = new WidgetFramesLayer(host, store, {
    host: "127.0.0.1:8000",
    protocol: "http:",
    onDrag: (app, step) => void drags.push([app, step]),
    onMenu: (app, point, frame) => void menus.push([app, point, frame]),
  });
  layer.start();
  await store.start(NO_LINK);
  layer.reconcile();
});

afterEach(() => {
  layer.stop();
  resetEmbedderRelayForTesting();
  backdrop.remove();
  taskbar.remove();
});

describe("creating and placing frames", () => {
  it("frames each enabled widget at its app's widget path, sandboxed like a page, and no disabled one", () => {
    const frame = frameOf("gauge");
    expect(frame.getAttribute("src")).toBe("http://127.0.0.1:7005/widget");
    expect(frame.getAttribute("sandbox")).toContain("allow-same-origin");
    expect(frame.title).toBe("Gauge widget");
    expect(frameIn(host, "meter")).toBeNull();
    expect(layer.isOver(host)).toBe(true);
    expect(layer.isOver(trayHost)).toBe(false);
  });

  it("keeps a frame loaded out of sight at the largest widget size until its chrome draws a content box", () => {
    expect(boxOf(wrapperOf("gauge"))).toEqual(["0px", "0px", "320px", "240px", "hidden", "none"]);
    // A content box with no area yet is no place to lay the page either.
    const chrome = drawChrome(backdrop, "data-widget", "gauge", { left: 40, top: 30, width: 0, height: 60 });
    layer.reconcile();
    expect(wrapperOf("gauge").style.visibility).toBe("hidden");
    chrome.remove();
    drawChrome(backdrop, "data-widget", "gauge", { left: 40, top: 30, width: 180, height: 60 });
    layer.reconcile();
    expect(boxOf(wrapperOf("gauge"))).toEqual(["40px", "30px", "180px", "60px", "", "auto"]);
  });

  it("lays one frame over its content box again as it moves, for a drag's step, and ignores an app it does not frame", () => {
    const box = { left: 40, top: 30, width: 180, height: 60 };
    drawChrome(backdrop, "data-widget", "gauge", box);
    layer.reconcile();
    box.left = 300;
    box.top = 200;
    layer.placeFrame("gauge");
    layer.placeFrame("meter");
    expect(boxOf(wrapperOf("gauge"))).toEqual(["300px", "200px", "180px", "60px", "", "auto"]);
  });

  it("makes every frame inert while a press on a handle lasts", () => {
    drawChrome(backdrop, "data-widget", "gauge", { left: 40, top: 30, width: 180, height: 60 });
    layer.reconcile();
    layer.setGestureActive(true);
    expect(wrapperOf("gauge").style.pointerEvents).toBe("none");
    layer.setGestureActive(true);
    expect(wrapperOf("gauge").style.pointerEvents).toBe("none");
    layer.setGestureActive(false);
    expect(wrapperOf("gauge").style.pointerEvents).toBe("auto");
  });

  it("removes a widget's frame and forgets its report when it is disabled, and frames it anew when re-enabled", () => {
    const first = frameOf("gauge");
    store.reportWidgetSize("gauge", { width: 180, height: 60 });
    socket.deliver().onWidgetsUpdated([{ app: "gauge", is_enabled: false }]);
    layer.reconcile();
    expect(frameIn(host, "gauge")).toBeNull();
    expect(store.getState().widgetReports.gauge?.size ?? null).toBeNull();
    socket.deliver().onWidgetsUpdated([{ app: "gauge", is_enabled: true }]);
    layer.reconcile();
    expect(frameOf("gauge")).not.toBe(first);
  });

  it("frames a widget anew at its new path when its app changes it", () => {
    const first = frameOf("gauge");
    offerApps(api, socket, [appRecord("gauge", { url: "http://127.0.0.1:7005", widget: { ...WIDGET, path: "/v2" } })]);
    layer.reconcile();
    expect(first.isConnected).toBe(false);
    expect(frameOf("gauge").getAttribute("src")).toBe("http://127.0.0.1:7005/v2");
  });

  it("removes every frame on stop, and forgets what each page said", () => {
    store.reportWidgetSize("gauge", { width: 180, height: 60 });
    layer.stop();
    expect(host.querySelector("iframe")).toBeNull();
    expect(store.getState().widgetReports.gauge?.size ?? null).toBeNull();
  });
});

describe("the handshake and the mode", () => {
  it("tells a page nothing before it loads, then greets it as a widget on the active desktop and tells it its mode", () => {
    const spy = spyOnFrame("gauge");
    layer.reconcile();
    expect(spy).not.toHaveBeenCalled();
    load("gauge");
    expect(posted(spy)).toEqual([
      {
        type: SHELL_HANDSHAKE,
        clientId: CLIENT,
        windowId: null,
        desktopId: "home",
        app: "gauge",
        path: "/widget",
        surface: "widget",
      },
      { type: SHELL_WIDGET_MODE, mode: "collapsed" },
    ]);
    // Nothing changed: a redraw says nothing more.
    layer.reconcile();
    expect(spy).toHaveBeenCalledTimes(2);
  });

  it("forgets what a page said when it reloads, and greets the fresh page again", () => {
    load("gauge");
    store.reportWidgetSize("gauge", { width: 180, height: 60 });
    const spy = spyOnFrame("gauge");
    load("gauge");
    expect(store.getState().widgetReports.gauge?.size ?? null).toBeNull();
    expect(posted(spy).map((message) => message.type)).toEqual([SHELL_HANDSHAKE, SHELL_WIDGET_MODE]);
  });

  it("tells the page its new mode alone when the client changes it", async () => {
    load("gauge");
    const spy = spyOnFrame("gauge");
    await store.setWidgetMode("gauge", "expanded");
    layer.reconcile();
    expect(posted(spy)).toEqual([{ type: SHELL_WIDGET_MODE, mode: "expanded" }]);
  });

  it("greets the page again, naming the new desktop, when the client switches desktops", async () => {
    load("gauge");
    const spy = spyOnFrame("gauge");
    await store.switchDesktop("work");
    layer.reconcile();
    expect(posted(spy)).toEqual([
      expect.objectContaining({ type: SHELL_HANDSHAKE, desktopId: "work", surface: "widget" }),
      { type: SHELL_WIDGET_MODE, mode: "collapsed" },
    ]);
  });

  it("tells every widget it is collapsed on a phone, framed in the backdrop's host even when placed in the tray", async () => {
    await store.setWidgetPlacement("gauge", "tray");
    layer.setTrayHost(trayHost);
    expect(frameIn(trayHost, "gauge")).not.toBeNull();
    store.setThemeMetrics(METRICS, { isPhone: true, isTouch: false });
    layer.reconcile();
    expect(frameIn(trayHost, "gauge")).toBeNull();
    const spy = spyOnFrame("gauge");
    load("gauge");
    expect(posted(spy)[1]).toEqual({ type: SHELL_WIDGET_MODE, mode: "collapsed" });
  });
});

describe("widgets in the taskbar", () => {
  beforeEach(async () => {
    await store.setWidgetPlacement("gauge", "tray");
    drawChrome(backdrop, "data-widget", "gauge", { left: 40, top: 30, width: 180, height: 60 });
    drawChrome(taskbar, "data-widget", "gauge", { left: 900, top: 810, width: 64, height: 24 });
    drawChrome(taskbar, "data-widget-popover", "gauge", { left: 780, top: 600, width: 200, height: 180 });
  });

  it("frames a tray widget in the backdrop's host until the taskbar gives the layer its own, then moves it there", () => {
    expect(frameIn(host, "gauge")).not.toBeNull();
    layer.setTrayHost(trayHost);
    expect(frameIn(host, "gauge")).toBeNull();
    // Laid over its slot, in the tray host's own pixels.
    expect(boxOf(wrapperOf("gauge"))).toEqual(["900px", "10px", "64px", "24px", "", "auto"]);
    // The same host again changes nothing; the taskbar going takes the frame back to the backdrop.
    const frame = frameOf("gauge");
    layer.setTrayHost(trayHost);
    expect(frameOf("gauge")).toBe(frame);
    layer.setTrayHost(null);
    expect(frameIn(host, "gauge")).not.toBeNull();
    expect(frameIn(trayHost, "gauge")).toBeNull();
  });

  it("tells a tray page its form, and while its popover is open lays the same frame over the popover, expanded", () => {
    layer.setTrayHost(trayHost);
    const spy = spyOnFrame("gauge");
    load("gauge");
    expect(posted(spy)[1]).toEqual({ type: SHELL_WIDGET_MODE, mode: "tray" });
    const frame = frameOf("gauge");
    store.toggleTrayPopover("gauge");
    layer.reconcile();
    expect(frameOf("gauge")).toBe(frame);
    expect(posted(spy)[2]).toEqual({ type: SHELL_WIDGET_MODE, mode: "expanded" });
    expect(boxOf(wrapperOf("gauge"))).toEqual(["780px", "-200px", "200px", "180px", "", "auto"]);
  });

  it("tells a page in the bar's row it is a toolbar widget", async () => {
    layer.setTrayHost(trayHost);
    load("gauge");
    const spy = spyOnFrame("gauge");
    await store.setWidgetPlacement("gauge", "toolbar");
    layer.reconcile();
    expect(posted(spy)).toEqual([{ type: SHELL_WIDGET_MODE, mode: "toolbar" }]);
  });

  it("opens or closes the popover on a click on a taskbar page, taking down its tooltip", () => {
    layer.setTrayHost(trayHost);
    messageFromWidget("gauge", { type: SHELL_WIDGET_PRESS });
    expect(store.getState().trayPopoverApp).toBe("gauge");
    expect(hideTooltipAtRect).toHaveBeenCalledWith("widget:gauge");
    messageFromWidget("gauge", { type: SHELL_WIDGET_PRESS });
    expect(store.getState().trayPopoverApp).toBeNull();
  });

  it("closes the open popover on Escape in its page, and no other", () => {
    layer.setTrayHost(trayHost);
    messageFromWidget("gauge", { type: SHELL_WIDGET_DISMISS });
    expect(store.getState().trayPopoverApp).toBeNull();
    store.toggleTrayPopover("gauge");
    messageFromWidget("gauge", { type: SHELL_WIDGET_DISMISS });
    expect(store.getState().trayPopoverApp).toBeNull();
  });

  it("keeps a tray page's size apart from its expanded size", () => {
    layer.setTrayHost(trayHost);
    load("gauge");
    messageFromWidget("gauge", { type: SHELL_WIDGET_SIZE, width: 64, height: 24 });
    store.toggleTrayPopover("gauge");
    layer.reconcile();
    messageFromWidget("gauge", { type: SHELL_WIDGET_SIZE, width: 200, height: 180 });
    expect(store.getState().widgetReports.gauge).toMatchObject({
      size: { width: 200, height: 180 },
      traySize: { width: 64, height: 24 },
    });
  });
});

describe("what a widget's page says", () => {
  it("reports a page's size to the store, and ignores one that is not a finite pair of numbers", () => {
    load("gauge");
    messageFromWidget("gauge", { type: SHELL_WIDGET_SIZE, width: "180", height: 60 });
    messageFromWidget("gauge", { type: SHELL_WIDGET_SIZE, width: Infinity, height: 60 });
    expect(store.getState().widgetReports.gauge?.size ?? null).toBeNull();
    messageFromWidget("gauge", { type: SHELL_WIDGET_SIZE, width: 180, height: 60 });
    expect(store.getState().widgetReports.gauge).toMatchObject({ size: { width: 180, height: 60 }, traySize: null });
  });

  it("reports a page's hidden flag to the store, and ignores one that is not a boolean", () => {
    messageFromWidget("gauge", { type: SHELL_WIDGET_HIDDEN, isHidden: "yes" });
    expect(store.getState().widgetReports.gauge?.isHidden ?? false).toBe(false);
    messageFromWidget("gauge", { type: SHELL_WIDGET_HIDDEN, isHidden: true });
    expect(store.getState().widgetReports.gauge?.isHidden).toBe(true);
  });

  it("ignores a press from a widget on the desktop, which has no popover", () => {
    messageFromWidget("gauge", { type: SHELL_WIDGET_PRESS });
    expect(store.getState().trayPopoverApp).toBeNull();
  });

  it("asks for a widget's menu at its right-click, in the shell's viewport, and ignores one with no point", () => {
    placeFrameAt("gauge", 40, 30);
    messageFromWidget("gauge", { type: SHELL_WIDGET_MENU, x: 12, y: "5" });
    expect(menus).toEqual([]);
    messageFromWidget("gauge", { type: SHELL_WIDGET_MENU, x: 12, y: 5 });
    expect(menus).toEqual([["gauge", { x: 52, y: 35 }, frameOf("gauge")]]);
    expect(hideTooltipAtRect).toHaveBeenCalledWith("widget:gauge");
  });

  it("shows a page's tooltip under the element's place in the frame, and takes it down when the page says so", () => {
    placeFrameAt("gauge", 40, 30);
    messageFromWidget("gauge", { type: SHELL_WIDGET_TOOLTIP, text: "42%", left: 10, top: 4, width: 30, height: 16 });
    expect(showTooltipAtRect).toHaveBeenCalledWith(
      "widget:gauge",
      { left: 50, top: 34, bottom: 50, width: 30 },
      "42%",
    );
    messageFromWidget("gauge", { type: SHELL_WIDGET_TOOLTIP, text: null });
    messageFromWidget("gauge", { type: SHELL_WIDGET_TOOLTIP, text: "42%", left: 10, top: 4, width: 30 });
    expect(hideTooltipAtRect).toHaveBeenCalledTimes(2);
    expect(showTooltipAtRect).toHaveBeenCalledTimes(1);
  });

  it("ignores every widget message from a frame that is no widget's", () => {
    const stranger = document.createElement("iframe");
    document.body.appendChild(stranger);
    // A frame claiming a widget's app is still not the frame the layer made for it.
    const impostor = document.createElement("iframe");
    impostor.setAttribute("data-widget-frame", "gauge");
    document.body.appendChild(impostor);
    expect(layer.appOfFrame(stranger)).toBeNull();
    expect(layer.appOfFrame(impostor)).toBeNull();
    expect(layer.appOfFrame(frameOf("gauge"))).toBe("gauge");
    store.toggleTrayPopover("gauge");
    for (const frame of [stranger, impostor]) {
      messageFrom(frame, { type: SHELL_WIDGET_SIZE, width: 180, height: 60 });
      messageFrom(frame, { type: SHELL_WIDGET_HIDDEN, isHidden: true });
      messageFrom(frame, { type: SHELL_WIDGET_DISMISS });
      messageFrom(frame, { type: SHELL_WIDGET_PRESS });
      messageFrom(frame, { type: SHELL_WIDGET_MENU, x: 1, y: 1 });
      messageFrom(frame, { type: SHELL_WIDGET_TOOLTIP, text: "hi", left: 0, top: 0, width: 1, height: 1 });
      messageFrom(frame, { type: SHELL_WIDGET_DRAG, phase: "start", screenX: 1, screenY: 1 });
    }
    expect(store.getState().widgetReports.gauge).toBeUndefined();
    expect(store.getState().trayPopoverApp).toBe("gauge");
    expect(menus).toEqual([]);
    expect(drags).toEqual([]);
    expect(showTooltipAtRect).not.toHaveBeenCalled();
    stranger.remove();
    impostor.remove();
  });
});

describe("a drag a widget's page reports", () => {
  /** The frame sits at (40, 30) in the viewport, and the screen is offset from the viewport by (100, 50). */
  function startDrag(): void {
    placeFrameAt("gauge", 40, 30);
    messageFromWidget("gauge", {
      type: SHELL_WIDGET_DRAG,
      phase: "start",
      screenX: 160,
      screenY: 90,
      pressX: 4,
      pressY: 3,
      pressScreenX: 144,
      pressScreenY: 83,
    });
  }

  it("turns each step's screen point into the shell's viewport by where the press was in the frame", () => {
    startDrag();
    // The frame moves under the pointer; the screen points stay true.
    placeFrameAt("gauge", 300, 300);
    messageFromWidget("gauge", { type: SHELL_WIDGET_DRAG, phase: "move", screenX: 200, screenY: 120 });
    messageFromWidget("gauge", { type: SHELL_WIDGET_DRAG, phase: "end", screenX: 210, screenY: 130 });
    // After the end, a stray move is no drag's.
    messageFromWidget("gauge", { type: SHELL_WIDGET_DRAG, phase: "move", screenX: 220, screenY: 140 });
    expect(drags).toEqual([
      ["gauge", { phase: "start", point: { x: 60, y: 40 }, press: { x: 44, y: 33 } }],
      ["gauge", { phase: "move", point: { x: 100, y: 70 } }],
      ["gauge", { phase: "end", point: { x: 110, y: 80 } }],
    ]);
    expect(hideTooltipAtRect).toHaveBeenCalledWith("widget:gauge");
  });

  it("passes on a cancel only for a drag under way", () => {
    messageFromWidget("gauge", { type: SHELL_WIDGET_DRAG, phase: "cancel" });
    expect(drags).toEqual([]);
    startDrag();
    messageFromWidget("gauge", { type: SHELL_WIDGET_DRAG, phase: "cancel" });
    messageFromWidget("gauge", { type: SHELL_WIDGET_DRAG, phase: "move", screenX: 200, screenY: 120 });
    expect(drags.map(([, step]) => step.phase)).toEqual(["start", "cancel"]);
  });

  it("ignores a step with no screen point, a start with no press, and a phase it does not know", () => {
    placeFrameAt("gauge", 40, 30);
    messageFromWidget("gauge", { type: SHELL_WIDGET_DRAG, phase: "start", screenX: 160 });
    messageFromWidget("gauge", { type: SHELL_WIDGET_DRAG, phase: "start", screenX: 160, screenY: 90, pressX: 4 });
    messageFromWidget("gauge", { type: SHELL_WIDGET_DRAG, phase: "move", screenX: 200, screenY: 120 });
    expect(drags).toEqual([]);
    startDrag();
    messageFromWidget("gauge", { type: SHELL_WIDGET_DRAG, phase: "hover", screenX: 200, screenY: 120 });
    expect(drags.map(([, step]) => step.phase)).toEqual(["start"]);
  });

  it("cancels a drag under way when its widget's frame goes", async () => {
    startDrag();
    socket.deliver().onWidgetsUpdated([{ app: "gauge", is_enabled: false }]);
    layer.reconcile();
    await settle();
    expect(drags.map(([, step]) => step.phase)).toEqual(["start", "cancel"]);
    expect(hideTooltipAtRect).toHaveBeenCalledWith("widget:gauge");
  });
});
