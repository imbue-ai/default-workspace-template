// @vitest-environment jsdom
/**
 * The desktop's root over the fake shell: what the App owns beyond the views it composes, the
 * document-level keyboard and pointer handling around the launcher and the menus.
 */
import "../testing/dom";
import { mountView, unmountViews } from "../testing/mount";
import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { GestureListener, GestureSource } from "../gestures/pointerGestures";
import { DesktopStore } from "../store/DesktopStore";
import { FakeDesktopApi, FakeDesktopSocket } from "../testing/fakeShell";
import { appRecord, desktopRecord, placementRecord, themeMetricsRecord, windowRecord } from "../testing/records";
import { App } from "./App";

const CLIENT = "client-1";
const NO_LINK = { desktopId: null, open: null, launch: null };
let gestureListener: GestureListener | null = null;
const gestures: GestureSource = {
  attach: (_root, listener) => {
    gestureListener = listener;
    return () => undefined;
  },
};

let store: DesktopStore;

function pressEscape(): void {
  document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  m.redraw.sync();
}

beforeEach(async () => {
  // The template catalog request the App fires on mount: a shell with no catalog configured.
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: true, json: async () => ({ catalog: null }) })),
  );
  const api = new FakeDesktopApi();
  const socket = new FakeDesktopSocket();
  api.desktops = [desktopRecord("home", { windows: [windowRecord("win-1", "docs", "/a")] })];
  api.writeLayout("home", CLIENT, { updated_at: null, placements: [placementRecord("win-1")] });
  store = new DesktopStore({
    clientId: CLIENT,
    api,
    socket,
    metrics: themeMetricsRecord(),
    modes: { isCompact: false, isTouch: false },
    redraw: () => m.redraw(),
    notify: () => undefined,
    reloadInterface: () => undefined,
  });
  await store.start(NO_LINK);
  socket.deliver().onAppsUpdated([appRecord("docs")]);
  mountView(() => m(App, { store, gestures, host: "127.0.0.1:8000", protocol: "http:" }));
});

afterEach(() => {
  unmountViews();
  vi.unstubAllGlobals();
  gestureListener = null;
});

describe("a window drag", () => {
  const binding = { kind: "window-move", windowId: "win-1" } as const;

  /** Render win-1 on a 1000x800 backdrop and begin dragging it at (100, 60), with no redraw after the begin. */
  function beginDrag(): { listener: GestureListener; element: HTMLElement; preview: HTMLElement } {
    store.setBackdropSize({ width: 1000, height: 800 });
    m.redraw.sync();
    const listener = gestureListener as GestureListener;
    const element = document.querySelector('[data-window-id="win-1"]') as HTMLElement;
    const preview = document.querySelector("[data-snap-preview]") as HTMLElement;
    listener.onBegin(binding, { x: 100, y: 60 }, { x: 100, y: 60 });
    return { listener, element, preview };
  }

  it("paints the window, and the snap preview, per move with no redraw, and saves on release", async () => {
    const { listener, element, preview } = beginDrag();
    expect(element.style.left).toBe("50px");
    expect(preview.style.display).toBe("none");
    // Straight onto the element, before any redraw could run (mithril's are asynchronous).
    listener.onMove(binding, { x: 150, y: 90 }, { x: 50, y: 30 });
    expect(element.style.left).toBe("100px");
    expect(element.style.top).toBe("78px");
    expect(preview.style.display).toBe("none");
    listener.onMove(binding, { x: 5, y: 400 }, { x: -95, y: 340 });
    expect(preview.style.display).toBe("");
    expect(preview.style.width).toBe("500px");
    // A redraw from any other cause renders the same thing the drag painted.
    m.redraw.sync();
    expect(document.querySelector('[data-window-id="win-1"]')).toBe(element);
    expect(preview.style.display).toBe("");
    expect(preview.style.width).toBe("500px");
    listener.onEnd(binding, { x: 5, y: 400 }, { x: -95, y: 340 });
    m.redraw.sync();
    expect(element.getAttribute("data-window-state")).toBe("SNAPPED_LEFT");
    expect(preview.style.display).toBe("none");
  });

  // Mithril diffs a render against the last render, not the DOM: with no redraw between the begin and
  // the end, the end render finds the same rectangle (a cancel) or the same hidden preview (a snap
  // release) it rendered at the begin and writes nothing, so the paint itself must have put them right.
  it("puts the window back and hides the preview when cancelled, with no redraw in between", () => {
    const { listener, element, preview } = beginDrag();
    m.redraw.sync();
    listener.onMove(binding, { x: 5, y: 400 }, { x: -95, y: 340 });
    expect(element.style.left).not.toBe("50px");
    expect(preview.style.display).toBe("");
    listener.onCancel(binding);
    expect(element.style.left).toBe("50px");
    expect(preview.style.display).toBe("none");
    m.redraw.sync();
    expect(element.style.left).toBe("50px");
    expect(preview.style.display).toBe("none");
  });

  it("hides the preview on a snap release, with no redraw in between", () => {
    const { listener, preview } = beginDrag();
    m.redraw.sync();
    listener.onMove(binding, { x: 5, y: 400 }, { x: -95, y: 340 });
    expect(preview.style.display).toBe("");
    listener.onEnd(binding, { x: 5, y: 400 }, { x: -95, y: 340 });
    m.redraw.sync();
    expect(document.querySelector('[data-window-id="win-1"]')?.getAttribute("data-window-state")).toBe("SNAPPED_LEFT");
    expect(preview.style.display).toBe("none");
  });
});

describe("Escape", () => {
  it("closes the launcher, unless a dialog is up over it, which takes the Escape itself", () => {
    store.openLauncher();
    m.redraw.sync();
    expect(document.querySelector("[data-launcher-overlay]")).not.toBeNull();

    const modal = document.createElement("div");
    modal.className = "modal-overlay";
    document.body.appendChild(modal);
    pressEscape();
    expect(store.isLauncherOpen()).toBe(true);

    modal.remove();
    pressEscape();
    expect(store.isLauncherOpen()).toBe(false);
    expect(document.querySelector("[data-launcher-overlay]")).toBeNull();
  });

  it("closes a menu opened over the launcher (by keyboard, so the launcher stayed) before the launcher", () => {
    store.openLauncher();
    m.redraw.sync();
    (document.querySelector("[data-desktops-menu]") as HTMLElement).click();
    m.redraw.sync();
    expect(document.querySelector('[data-floating="desktops-menu"]')).not.toBeNull();
    pressEscape();
    expect(document.querySelector('[data-floating="desktops-menu"]')).toBeNull();
    expect(store.isLauncherOpen()).toBe(true);
    pressEscape();
    expect(store.isLauncherOpen()).toBe(false);
  });
});

/** The shield over the focused window's content, which is there only while a menu or the launcher is open. */
function focusedShield(): HTMLElement | null {
  return document.querySelector('[data-window-id="win-1"][data-focused="true"] [data-window-shield]');
}

function pressOn(element: HTMLElement): void {
  element.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
  m.redraw.sync();
}

describe("a press into the focused page", () => {
  it("closes an open menu: the page is shielded while the menu is up, so the press reaches the shell", () => {
    expect(focusedShield()).toBeNull();
    (document.querySelector('[data-window-control="menu"]') as HTMLElement).click();
    m.redraw.sync();
    expect(document.querySelector('[data-floating="window-menu"]')).not.toBeNull();
    const shield = focusedShield();
    expect(shield).not.toBeNull();
    pressOn(shield as HTMLElement);
    expect(document.querySelector('[data-floating="window-menu"]')).toBeNull();
    expect(focusedShield()).toBeNull();
  });

  it("closes the launcher the same way", () => {
    store.openLauncher();
    m.redraw.sync();
    const shield = focusedShield();
    expect(shield).not.toBeNull();
    pressOn(shield as HTMLElement);
    expect(store.isLauncherOpen()).toBe(false);
    expect(focusedShield()).toBeNull();
  });
});
