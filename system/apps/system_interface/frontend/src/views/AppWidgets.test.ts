// @vitest-environment jsdom
/**
 * The desktop's root over the fake shell, for the app widgets it wires in: the widget's menu on the desktop, in the
 * taskbar's tray and in its row, the taskbar's slots and popover, the popover closing on a press outside it or on
 * Escape, a drag the widget's own page reports, and where the widget frames live as the layout changes.
 */
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import { SHELL_WIDGET_DRAG, SHELL_WIDGET_MENU } from "@imbue/workspace-ui/src/app_contract";
import m from "mithril";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { GestureListener, GestureSource } from "../gestures/pointerGestures";
import type { DesktopShortcut, WidgetPresentation } from "../model/records";
import { initEmbedderRelay, resetEmbedderRelayForTesting } from "../relay";
import { TourController } from "../model/tour";
import { DesktopStore } from "../store/DesktopStore";
import { FakeDesktopApi, FakeDesktopSocket, settle } from "../testing/fakeShell";
import { FakeTourApi } from "../testing/fakeTour";
import { appRecord, desktopRecord, placementRecord, themeMetricsRecord, windowRecord } from "../testing/records";
import { App } from "./App";

const CLIENT = "client-1";
const NO_LINK = { desktopId: null, open: null, launch: null };
const REFERENCE_ROWS = ["copy-reference", "explain-element", "modify-element"];
const CLOCK = appRecord("clock", { widget: { path: "/widget", default_enabled: true, tray: true, toolbar: true } });
/** A widget whose page draws no form for the taskbar, so it lives on the desktop alone. */
const NOTE = appRecord("note", { widget: { path: "/widget", default_enabled: true, tray: false, toolbar: false } });

let gestureListener: GestureListener | null = null;
const gestures: GestureSource = {
  attach: (_root, listener) => {
    gestureListener = listener;
    return () => undefined;
  },
};

let api: FakeDesktopApi;
let socket: FakeDesktopSocket;
let store: DesktopStore;

/** A fresh fake shell whose home desktop holds win-1 of docs, whose workspace shows the clock's and the note's
 *  widgets, a store started over it on a 1000x800 backdrop, and the App mounted over the store. */
async function mountApp(): Promise<void> {
  api = new FakeDesktopApi();
  socket = new FakeDesktopSocket();
  api.desktops = [desktopRecord("home", { windows: [windowRecord("win-1", "docs", "/a")] })];
  api.writeLayout("home", CLIENT, { updated_at: null, placements: [placementRecord("win-1")] });
  api.widgetSwitches = [
    { app: "clock", is_enabled: true },
    { app: "note", is_enabled: true },
  ];
  store = new DesktopStore({
    clientId: CLIENT,
    api,
    socket,
    metrics: themeMetricsRecord(),
    modes: { isPhone: false, isTouch: false },
    redraw: () => m.redraw(),
    reloadInterface: () => undefined,
    soloWindowId: null,
    outsideLinks: {
      openInEmbedder: () => undefined,
      openInBrowser: () => undefined,
      launchInSystemApp: () => undefined,
    },
  });
  await store.start(NO_LINK);
  api.apps = [appRecord("docs"), CLOCK, NOTE];
  socket.deliver().onAppsUpdated(api.apps);
  const tour = new TourController({
    api: new FakeTourApi({ isSeen: true }),
    highlightChrome: () => undefined,
    isAutoStartAllowed: () => false,
    redraw: () => m.redraw(),
  });
  mountView(() => m(App, { store, gestures, tour, host: "127.0.0.1:8000", protocol: "http:" }));
  store.setBackdropSize({ width: 1000, height: 800 });
  m.redraw.sync();
}

beforeEach(async () => {
  resetEmbedderRelayForTesting();
  initEmbedderRelay();
  await mountApp();
});

afterEach(() => {
  unmountViews();
  gestureListener = null;
});

/** This client now shows each widget in ``widgets`` so, as the shell's broadcast says. */
function presentWidgets(widgets: Record<string, Partial<WidgetPresentation>>): void {
  const full = Object.fromEntries(
    Object.entries(widgets).map(([app, presentation]) => [
      app,
      { position: null, mode: "expanded", placement: "desktop", ...presentation } as WidgetPresentation,
    ]),
  );
  socket.deliver().onClientWidgetsChanged({ clientId: CLIENT, widgets: full });
  // A widget moved into or out of the taskbar is framed anew there, whose page has said nothing yet.
  m.redraw.sync();
}

/** The widget's page reports its size: on the desktop (or expanded, in its popover), or in its taskbar form. */
function reportSize(app: string, width: number, height: number, isTray = false): void {
  store.reportWidgetSize(app, { width, height }, isTray);
  m.redraw.sync();
}

function rightClick(element: Element, x = 10, y = 10): MouseEvent {
  const event = new MouseEvent("contextmenu", { bubbles: true, cancelable: true, clientX: x, clientY: y });
  element.dispatchEvent(event);
  m.redraw.sync();
  return event;
}

function menuRowKeys(): string[] {
  return Array.from(document.body.querySelectorAll('[data-menu-part="menu"] [data-menu-row]')).map(
    (row) => row.getAttribute("data-menu-row") ?? "",
  );
}

function menuRowLabel(key: string): string {
  return document.querySelector(`[data-menu-part="menu"] [data-menu-row="${key}"]`)?.textContent?.trim() ?? "";
}

function chooseRow(key: string): void {
  const row = document.querySelector(`[data-menu-part="menu"] [data-menu-row="${key}"]`) as HTMLElement;
  row.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
  row.click();
  m.redraw.sync();
}

function pressEscape(): void {
  document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  m.redraw.sync();
}

function pressOn(element: Element): void {
  element.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
  m.redraw.sync();
}

function desktopWidget(app: string): HTMLElement | null {
  return document.querySelector(`[data-widgets] [data-widget="${app}"]`);
}

function frameOf(app: string): HTMLIFrameElement {
  return document.querySelector(`iframe[data-widget-frame="${app}"]`) as HTMLIFrameElement;
}

/** A message from the page inside ``app``'s widget frame, as the relay sees it. */
function messageFromWidget(app: string, data: Record<string, unknown>): void {
  const frame = frameOf(app);
  window.dispatchEvent(
    new MessageEvent("message", { data, source: frame.contentWindow, origin: new URL(frame.src).origin }),
  );
  m.redraw.sync();
}

function widgetCalls(): string[] {
  return api.calls.filter((call) => call.startsWith("setWidget"));
}

describe("a widget's menu on the desktop", () => {
  it("collapses it, moves it to each place its page draws a form for, opens its app, and hides it", async () => {
    reportSize("clock", 200, 100);
    expect(rightClick(desktopWidget("clock") as HTMLElement).defaultPrevented).toBe(true);
    expect(menuRowKeys()).toEqual([
      "widget-collapse",
      "widget-to-toolbar",
      "widget-to-tray",
      "widget-open",
      "widget-hide",
      ...REFERENCE_ROWS,
    ]);
    expect(menuRowLabel("widget-open")).toBe("Open Clock");
    // The open menu holds the widget up, out of its resting see-through look.
    expect(desktopWidget("clock")?.className).toContain("ring-accent");
    chooseRow("widget-collapse");
    await settle();
    expect(widgetCalls()).toEqual([`setWidgetPresentation:${CLIENT}:clock:default:collapsed:desktop`]);

    rightClick(desktopWidget("clock") as HTMLElement);
    expect(menuRowKeys()[0]).toBe("widget-expand");
    expect(menuRowLabel("widget-expand")).toBe("Expand");
    chooseRow("widget-to-tray");
    await settle();
    expect(widgetCalls()).toEqual([
      `setWidgetPresentation:${CLIENT}:clock:default:collapsed:desktop`,
      `setWidgetPresentation:${CLIENT}:clock:default:collapsed:tray`,
    ]);
    expect(desktopWidget("clock")).toBeNull();
  });

  it("offers no move for a widget whose page draws no taskbar form", () => {
    reportSize("note", 200, 100);
    rightClick(desktopWidget("note") as HTMLElement);
    expect(menuRowKeys()).toEqual(["widget-collapse", "widget-open", "widget-hide", ...REFERENCE_ROWS]);
  });

  it("opens the widget's app from Open, and hides the widget for the workspace from Hide widget", async () => {
    reportSize("clock", 200, 100);
    rightClick(desktopWidget("clock") as HTMLElement);
    chooseRow("widget-open");
    await settle();
    expect(store.getState().desktops[0].windows.map((window) => window.app)).toEqual(["docs", "clock"]);

    rightClick(desktopWidget("clock") as HTMLElement);
    expect(menuRowLabel("widget-hide")).toBe("Hide widget");
    chooseRow("widget-hide");
    await settle();
    expect(api.calls).toContain("setWidgetEnabled:clock:false");
    m.redraw.sync();
    expect(desktopWidget("clock")).toBeNull();
  });

  it("collapses and expands from a double-click", async () => {
    reportSize("clock", 200, 100);
    (desktopWidget("clock") as HTMLElement).dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
    await settle();
    expect(store.getState().widgets.clock?.mode).toBe("collapsed");
  });

  it("opens from a long press on its rim, as from a right-click", () => {
    reportSize("clock", 200, 100);
    const element = desktopWidget("clock") as HTMLElement;
    // jsdom does no hit testing: the point under the press is the widget's box.
    document.elementFromPoint = (): Element | null => element;
    try {
      (gestureListener as GestureListener).onLongPress({ kind: "widget", app: "clock", element }, { x: 20, y: 20 });
      m.redraw.sync();
    } finally {
      Reflect.deleteProperty(document, "elementFromPoint");
    }
    expect(menuRowKeys()).toEqual([
      "widget-collapse",
      "widget-to-toolbar",
      "widget-to-tray",
      "widget-open",
      "widget-hide",
      ...REFERENCE_ROWS,
    ]);
  });

  it("opens from a right-click in the widget's own page, at the point it names", () => {
    reportSize("clock", 200, 100);
    messageFromWidget("clock", { type: SHELL_WIDGET_MENU, x: 30, y: 40 });
    expect(menuRowKeys()).toEqual([
      "widget-collapse",
      "widget-to-toolbar",
      "widget-to-tray",
      "widget-open",
      "widget-hide",
      ...REFERENCE_ROWS,
    ]);
  });

  it("is gone, rows and all, once the widget is no longer shown", () => {
    reportSize("clock", 200, 100);
    rightClick(desktopWidget("clock") as HTMLElement);
    socket.deliver().onWidgetsUpdated([
      { app: "clock", is_enabled: false },
      { app: "note", is_enabled: true },
    ]);
    m.redraw.sync();
    expect(menuRowKeys().filter((key) => key.startsWith("widget-"))).toEqual([]);
  });
});

describe("a widget in the taskbar", () => {
  it("draws a slot in the tray at its page's tray size, once that size is reported and while it is not hidden", () => {
    presentWidgets({ clock: { placement: "tray" } });
    reportSize("clock", 200, 100);
    expect(document.querySelector('[data-tray-widget="clock"]')).toBeNull();
    reportSize("clock", 40, 20, true);
    const slot = document.querySelector('[data-system-tray] [data-tray-widget="clock"]') as HTMLElement;
    expect(slot).not.toBeNull();
    expect([slot.style.width, slot.style.height]).toEqual(["40px", "20px"]);
    expect(desktopWidget("clock")).toBeNull();
    expect(document.querySelector("[data-toolbar-widgets]")).toBeNull();
    store.reportWidgetHidden("clock", true);
    m.redraw.sync();
    expect(document.querySelector('[data-tray-widget="clock"]')).toBeNull();
  });

  it("draws a slot in the bar's row for a widget put in the toolbar", () => {
    presentWidgets({ clock: { placement: "toolbar" } });
    reportSize("clock", 60, 24, true);
    const slot = document.querySelector('[data-toolbar-widgets] [data-toolbar-widget="clock"]') as HTMLElement;
    expect(slot).not.toBeNull();
    expect(slot.style.width).toBe("60px");
    expect(document.querySelector('[data-tray-widget="clock"]')).toBeNull();
  });

  it("lays the page of a widget in the taskbar in the taskbar's frames host, and back on the desktop's when moved", async () => {
    expect(frameOf("clock").closest("[data-widget-frames]")).not.toBeNull();
    presentWidgets({ clock: { placement: "tray" } });
    m.redraw.sync();
    expect(frameOf("clock").closest("[data-tray-widget-frames]")).not.toBeNull();
    expect(frameOf("note").closest("[data-widget-frames]")).not.toBeNull();
  });

  it("offers a tray widget a move to the desktop or the toolbar, and no collapse", async () => {
    presentWidgets({ clock: { placement: "tray" } });
    reportSize("clock", 40, 20, true);
    rightClick(document.querySelector('[data-tray-widget="clock"]') as HTMLElement);
    expect(menuRowKeys()).toEqual([
      "widget-to-desktop",
      "widget-to-toolbar",
      "widget-open",
      "widget-hide",
      ...REFERENCE_ROWS,
    ]);
    chooseRow("widget-to-desktop");
    await settle();
    expect(widgetCalls()).toEqual([`setWidgetPresentation:${CLIENT}:clock:default:expanded:desktop`]);
  });

  it("offers a toolbar widget a move to the desktop or the tray", async () => {
    presentWidgets({ clock: { placement: "toolbar" } });
    reportSize("clock", 60, 24, true);
    rightClick(document.querySelector('[data-toolbar-widget="clock"]') as HTMLElement);
    expect(menuRowKeys()).toEqual([
      "widget-to-desktop",
      "widget-to-tray",
      "widget-open",
      "widget-hide",
      ...REFERENCE_ROWS,
    ]);
    chooseRow("widget-to-tray");
    await settle();
    expect(widgetCalls()).toEqual([`setWidgetPresentation:${CLIENT}:clock:default:expanded:tray`]);
  });

  it("opens its popover over the tray at its expanded size, held to the widget maximum, once that size is reported", () => {
    presentWidgets({ clock: { placement: "tray" } });
    reportSize("clock", 40, 20, true);
    const slot = document.querySelector('[data-tray-widget="clock"]') as HTMLElement;
    slot.click();
    m.redraw.sync();
    expect(store.getState().trayPopoverApp).toBe("clock");
    // Its page has not said how big it is expanded yet.
    expect(document.querySelector("[data-widget-popover]")).toBeNull();
    reportSize("clock", 500, 100);
    const popover = document.querySelector('[data-widget-popover="clock"]') as HTMLElement;
    expect(popover).not.toBeNull();
    expect(popover.closest("[data-toolbar-widgets]")).toBeNull();
    const content = popover.querySelector("[data-widget-content]") as HTMLElement;
    expect([content.style.width, content.style.height]).toEqual(["320px", "100px"]);
    expect(slot.getAttribute("data-popover-open")).toBe("true");
  });

  it("opens a toolbar widget's popover in the bar's row", () => {
    presentWidgets({ clock: { placement: "toolbar" } });
    reportSize("clock", 60, 24, true);
    reportSize("clock", 200, 100);
    store.toggleTrayPopover("clock");
    m.redraw.sync();
    expect(document.querySelector('[data-toolbar-widgets] [data-widget-popover="clock"]')).not.toBeNull();
  });
});

describe("an open popover", () => {
  function openPopover(): void {
    presentWidgets({ clock: { placement: "tray" } });
    reportSize("clock", 40, 20, true);
    reportSize("clock", 200, 100);
    store.toggleTrayPopover("clock");
    m.redraw.sync();
    expect(document.querySelector('[data-widget-popover="clock"]')).not.toBeNull();
  }

  it("stays open on a press on itself or on its slot, and closes on a press anywhere else", () => {
    openPopover();
    pressOn(document.querySelector('[data-widget-popover="clock"] [data-widget-content]') as HTMLElement);
    pressOn(document.querySelector('[data-tray-widget="clock"]') as HTMLElement);
    expect(store.getState().trayPopoverApp).toBe("clock");
    pressOn(document.querySelector("[data-backdrop-area]") as HTMLElement);
    expect(store.getState().trayPopoverApp).toBeNull();
    expect(document.querySelector("[data-widget-popover]")).toBeNull();
  });

  it("closes on a press on the tray's own items, which are not its slot", () => {
    openPopover();
    pressOn(document.querySelector('[data-tray-widget="desktops"] [data-desktop-switch]') as HTMLElement);
    expect(store.getState().trayPopoverApp).toBeNull();
  });

  it("closes on Escape before the launcher does", () => {
    openPopover();
    store.openLauncher();
    m.redraw.sync();
    pressEscape();
    expect(store.getState().trayPopoverApp).toBeNull();
    expect(store.isLauncherOpen()).toBe(true);
    pressEscape();
    expect(store.isLauncherOpen()).toBe(false);
  });
});

describe("a widget's drag", () => {
  /** The clock's box on the desktop: where its chrome is drawn, in backdrop pixels. */
  function clockBox(): { left: number; top: number } {
    const element = desktopWidget("clock") as HTMLElement;
    return { left: parseFloat(element.style.left), top: parseFloat(element.style.top) };
  }

  it("follows its rim's drag per move with no redraw, and keeps it where it is let go", async () => {
    reportSize("clock", 200, 100);
    const start = clockBox();
    const element = desktopWidget("clock") as HTMLElement;
    const binding = { kind: "widget", app: "clock", element } as const;
    const listener = gestureListener as GestureListener;
    // jsdom measures every box as empty, so the grab offset is the press point itself: a move to (x, y) puts the
    // box's corner at (x - 10, y - 10).
    listener.onBegin(binding, { x: 10, y: 10 }, { x: 10, y: 10 });
    const to = { x: start.left - 300 + 10, y: start.top + 200 + 10 };
    listener.onMove(binding, to, { x: to.x - 10, y: to.y - 10 });
    expect(clockBox()).toEqual({ left: start.left - 300, top: start.top + 200 });
    listener.onEnd(binding, to, { x: to.x - 10, y: to.y - 10 });
    expect(clockBox()).toEqual({ left: start.left - 300, top: start.top + 200 });
    await settle();
    m.redraw.sync();
    expect(clockBox()).toEqual({ left: start.left - 300, top: start.top + 200 });
    expect(store.getState().widgets.clock?.position).not.toBeNull();
  });

  it("puts the widget back where it was when its rim's drag is cancelled, before any redraw", () => {
    reportSize("clock", 200, 100);
    const start = clockBox();
    const element = desktopWidget("clock") as HTMLElement;
    const binding = { kind: "widget", app: "clock", element } as const;
    const listener = gestureListener as GestureListener;
    listener.onBegin(binding, { x: 10, y: 10 }, { x: 10, y: 10 });
    listener.onMove(binding, { x: start.left - 290, y: start.top + 10 }, { x: start.left - 300, y: start.top });
    expect(clockBox()).toEqual({ left: start.left - 300, top: start.top });
    listener.onCancel(binding);
    expect(clockBox()).toEqual(start);
    expect(widgetCalls()).toEqual([]);
  });

  /** A drag the clock's own page reports: pressed at (10, 10) in the page, a screen point being that point. */
  function dragFromPage(phase: string, screenX = 0, screenY = 0): void {
    const press = phase === "start" ? { pressX: 10, pressY: 10, pressScreenX: 10, pressScreenY: 10 } : {};
    messageFromWidget("clock", { type: SHELL_WIDGET_DRAG, phase, screenX, screenY, ...press });
  }

  it("follows a drag its own page reports, and keeps the widget where it is let go", async () => {
    reportSize("clock", 200, 100);
    const start = clockBox();
    // jsdom lays every frame at the viewport's corner, so a page point is a backdrop point.
    dragFromPage("start", 10, 10);
    expect(store.getGesture()?.kind).toBe("widget");
    dragFromPage("move", 10 - 300, 10 + 200);
    expect(clockBox()).toEqual({ left: start.left - 300, top: start.top + 200 });
    dragFromPage("end", 10 - 300, 10 + 200);
    expect(store.getGesture()).toBeNull();
    await settle();
    m.redraw.sync();
    expect(clockBox()).toEqual({ left: start.left - 300, top: start.top + 200 });
    expect(widgetCalls()).toHaveLength(1);
  });

  it("puts the widget back when its page cancels the drag", () => {
    reportSize("clock", 200, 100);
    const start = clockBox();
    dragFromPage("start", 10, 10);
    dragFromPage("move", 10 - 300, 10);
    dragFromPage("cancel");
    expect(store.getGesture()).toBeNull();
    expect(clockBox()).toEqual(start);
    expect(widgetCalls()).toEqual([]);
  });

  it("starts no drag from the page while another gesture runs, and leaves that gesture its own", () => {
    reportSize("clock", 200, 100);
    reportSize("note", 200, 100);
    const note = desktopWidget("note") as HTMLElement;
    const listener = gestureListener as GestureListener;
    const binding = { kind: "widget", app: "note", element: note } as const;
    listener.onBegin(binding, { x: 500, y: 500 }, { x: 500, y: 500 });
    const start = clockBox();
    dragFromPage("start", 10, 10);
    dragFromPage("move", 10 - 300, 10);
    dragFromPage("end", 10 - 300, 10);
    expect(clockBox()).toEqual(start);
    expect(store.getGesture()).toMatchObject({ kind: "widget", app: "note" });
  });

  it("opens no menu from the page on a phone, whose layout draws none, nor later back on the desktop", () => {
    reportSize("clock", 200, 100);
    store.setThemeMetrics(themeMetricsRecord(), { isPhone: true, isTouch: true });
    m.redraw.sync();
    messageFromWidget("clock", { type: SHELL_WIDGET_MENU, x: 30, y: 40 });
    store.setThemeMetrics(themeMetricsRecord(), { isPhone: false, isTouch: false });
    m.redraw.sync();
    expect(menuRowKeys()).toEqual([]);
  });

  it("starts no drag from the page on a phone, where a widget does not move", () => {
    reportSize("clock", 200, 100);
    store.setThemeMetrics(themeMetricsRecord(), { isPhone: true, isTouch: true });
    m.redraw.sync();
    dragFromPage("start", 10, 10);
    dragFromPage("move", 10 - 300, 10);
    expect(store.getGesture()).toBeNull();
  });
});

describe("the widget switch rows", () => {
  const CLOCK_SHORTCUT: DesktopShortcut = {
    target: { kind: "launch", app: "clock", launch: "new" },
    mode: "focus",
    cell: { column: 0, row: 0 },
  };

  function showShortcut(): void {
    api.desktops = [
      desktopRecord("home", { windows: [windowRecord("win-1", "docs", "/a")], shortcuts: [CLOCK_SHORTCUT] }),
    ];
    socket.deliver().onDesktopsUpdated(api.desktops);
    m.redraw.sync();
  }

  it("hides a shown widget from its app's desktop shortcut, and shows a hidden one", async () => {
    showShortcut();
    rightClick(document.querySelector('[data-shortcut="clock:new"]') as HTMLElement);
    expect(menuRowKeys()).toEqual([
      "open",
      "open-new",
      "change-mode",
      "widget-hide",
      "remove-from-desktop",
      ...REFERENCE_ROWS,
    ]);
    expect(menuRowLabel("widget-hide")).toBe("Hide widget");
    chooseRow("widget-hide");
    await settle();
    expect(api.calls).toContain("setWidgetEnabled:clock:false");

    rightClick(document.querySelector('[data-shortcut="clock:new"]') as HTMLElement);
    expect(menuRowKeys()).not.toContain("widget-hide");
    expect(menuRowLabel("widget-show")).toBe("Show widget");
    chooseRow("widget-show");
    await settle();
    expect(api.calls).toContain("setWidgetEnabled:clock:true");
  });

  it("offers the switch on the app's launcher menu beside its desktop shortcut, and none for an app with no widget", () => {
    store.openLauncher();
    m.redraw.sync();
    rightClick(document.querySelector('[data-launcher-overlay] [data-launch="clock:new"]') as HTMLElement);
    expect(menuRowKeys().slice(0, 2)).toEqual(["add-to-desktop", "widget-hide"]);
    pressEscape();
    rightClick(document.querySelector('[data-launcher-overlay] [data-launch="docs:new"]') as HTMLElement);
    expect(menuRowKeys()).not.toContain("widget-hide");
  });
});

describe("the widget frames across layouts", () => {
  it("moves every widget's frame to the phone's host and back, and drops them all when the App goes", () => {
    presentWidgets({ clock: { placement: "tray" } });
    m.redraw.sync();
    store.setThemeMetrics(themeMetricsRecord(), { isPhone: true, isTouch: true });
    m.redraw.sync();
    expect(document.querySelector("[data-phone-layout]")).not.toBeNull();
    expect(frameOf("clock").closest("[data-widget-frames]")).not.toBeNull();
    expect(frameOf("note").closest("[data-widget-frames]")).not.toBeNull();

    store.setThemeMetrics(themeMetricsRecord(), { isPhone: false, isTouch: false });
    m.redraw.sync();
    expect(frameOf("clock").closest("[data-tray-widget-frames]")).not.toBeNull();
    expect(frameOf("note").closest("[data-backdrop-area] [data-widget-frames]")).not.toBeNull();

    reportSize("note", 200, 100);
    unmountViews();
    expect(store.getState().widgetReports).toEqual({});
  });
});
