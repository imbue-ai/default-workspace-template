// @vitest-environment jsdom
/**
 * The desktop's root over the fake shell: what the App owns beyond the views it composes, the
 * document-level keyboard and pointer handling around the launcher and the menus.
 */
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { GestureListener, GestureSource } from "../gestures/pointerGestures";
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
import { AVATAR_DESIGN_PROMPT } from "./AvatarChooserDialog";
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

let api: FakeDesktopApi;
let socket: FakeDesktopSocket;
let store: DesktopStore;

function pressEscape(): void {
  document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  m.redraw.sync();
}

beforeEach(async () => {
  api = new FakeDesktopApi();
  socket = new FakeDesktopSocket();
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
  gestureListener = null;
});

describe("a floating entry drag", () => {
  const buddy = appRecord("buddy", { pin: { path: "/", style: "plain", scope: "linked", default_mode: "floating" } });

  /** A floating buddy entry on a 1000x800 backdrop, lifted at (10, 10): under jsdom every box measures as empty,
   *  so the grab offset is the press point itself and a move to (x, y) puts the box's corner at (x - 10, y - 10). */
  function beginDrag(): { listener: GestureListener; element: HTMLElement } {
    socket.deliver().onAppsUpdated([appRecord("docs"), buddy]);
    socket.deliver().onDesktopsUpdated([
      desktopRecord("home", {
        windows: [windowRecord("win-1", "docs", "/a"), windowRecord("win-9", "buddy", "/", { is_pinned: true })],
      }),
    ]);
    store.setBackdropSize({ width: 1000, height: 800 });
    m.redraw.sync();
    const element = document.querySelector('[data-pinned-entry="buddy"][data-entry-mode="floating"]') as HTMLElement;
    const listener = gestureListener as GestureListener;
    listener.onBegin({ kind: "floating-entry", app: "buddy", element }, { x: 10, y: 10 }, { x: 10, y: 10 });
    return { listener, element };
  }

  it("paints the entry per move with no redraw, and a redraw then draws the same box", () => {
    const { listener, element } = beginDrag();
    expect(element.style.left).toBe("928px");
    listener.onMove({ kind: "floating-entry", app: "buddy", element }, { x: 110, y: 210 }, { x: 100, y: 200 });
    expect(element.style.left).toBe("100px");
    expect(element.style.top).toBe("200px");
    m.redraw.sync();
    expect(document.querySelector('[data-pinned-entry="buddy"]')).toBe(element);
    expect(element.style.left).toBe("100px");
    listener.onEnd({ kind: "floating-entry", app: "buddy", element }, { x: 110, y: 210 }, { x: 100, y: 200 });
    expect(element.style.left).toBe("100px");
    m.redraw.sync();
    expect(element.style.left).toBe("100px");
  });

  it("puts the entry back where it was when cancelled, before any redraw", () => {
    const { listener, element } = beginDrag();
    listener.onMove({ kind: "floating-entry", app: "buddy", element }, { x: 110, y: 210 }, { x: 100, y: 200 });
    expect(element.style.left).toBe("100px");
    listener.onCancel({ kind: "floating-entry", app: "buddy", element });
    expect(element.style.left).toBe("928px");
    m.redraw.sync();
    expect(element.style.left).toBe("928px");
  });
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

  // A render diffs against the last render, not the DOM, so the paint at the end or the cancel must put the
  // window and the preview right itself.
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

  // The press, not the begin: the pixels a press spends reaching the drag threshold are spent beside the
  // handle, and a page still live there takes the moves that would have crossed it.
  it("makes the pages inert from the press, and gives them back when the press ends", () => {
    store.setBackdropSize({ width: 1000, height: 800 });
    m.redraw.sync();
    // Every box measures as empty under jsdom, and a page over a content box with no area is hidden rather
    // than laid out, which is the only step that writes the page's pointer events.
    const content = document.querySelector('[data-window-id="win-1"] [data-window-content]') as HTMLElement;
    content.getBoundingClientRect = () => ({ left: 100, top: 60, width: 500, height: 400 }) as DOMRect;
    m.redraw.sync();
    const page = document.querySelector('iframe[data-live-page="win-1"]')?.parentElement as HTMLElement;
    expect(page.style.pointerEvents).toBe("auto");
    const listener = gestureListener as GestureListener;
    listener.onPressStart(binding);
    expect(page.style.pointerEvents).toBe("none");
    listener.onPressEnd(binding);
    expect(page.style.pointerEvents).toBe("auto");
  });

  // jsdom raises no transition events of its own, so the travel is driven by hand here.
  it("re-places a travelling window's page every frame, and once more where the window landed", async () => {
    store.setBackdropSize({ width: 1000, height: 800 });
    const root = document.querySelector('[data-window-id="win-1"]') as HTMLElement;
    const content = root.querySelector("[data-window-content]") as HTMLElement;
    let travelled = { left: 100, top: 60, width: 500, height: 400 };
    content.getBoundingClientRect = () => travelled as DOMRect;
    m.redraw.sync();
    const page = document.querySelector('iframe[data-live-page="win-1"]')?.parentElement as HTMLElement;
    expect(page.style.left).toBe("100px");

    // One transition per property the move changes; the travel is over when the last of them ends.
    root.dispatchEvent(new Event("transitionrun", { bubbles: true }));
    root.dispatchEvent(new Event("transitionrun", { bubbles: true }));
    travelled = { ...travelled, left: 300 };
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    expect(page.style.left).toBe("300px");

    root.dispatchEvent(new Event("transitionend", { bubbles: true }));
    travelled = { ...travelled, left: 500 };
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    expect(page.style.left).toBe("500px");

    travelled = { ...travelled, left: 640 };
    root.dispatchEvent(new Event("transitionend", { bubbles: true }));
    expect(page.style.left).toBe("640px");
    travelled = { ...travelled, left: 900 };
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    expect(page.style.left).toBe("640px");
  });

  it("turns window motion off for the press and back on when it ends", () => {
    store.setBackdropSize({ width: 1000, height: 800 });
    m.redraw.sync();
    const listener = gestureListener as GestureListener;
    expect(document.querySelector("[data-window-motion]")).toBeNull();
    listener.onPressStart(binding);
    expect(document.querySelector('[data-window-motion="off"]')).not.toBeNull();
    listener.onPressEnd(binding);
    expect(document.querySelector("[data-window-motion]")).toBeNull();
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
    expect(document.querySelector(".desktops-menu")).not.toBeNull();
    pressEscape();
    expect(document.querySelector(".desktops-menu")).toBeNull();
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

describe("the avatar chooser", () => {
  // The pinned app declares a launch path taking a draft, so the chooser's prompt is launched into this client's
  // view of its window.
  const buddy = appRecord("buddy", {
    pin: { path: "/", style: "avatar", scope: "linked", default_mode: "bar" },
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

  /** The desktop with buddy's pinned window, its entry in the bar in the avatar style; answers the entry. */
  function pinnedEntry(...apps: readonly ReturnType<typeof appRecord>[]): HTMLElement {
    offerApps(api, socket, [appRecord("docs"), buddy, ...apps]);
    api.postLaunchAnswer = "/?chat=agent-1";
    api.desktops = [
      desktopRecord("home", {
        windows: [windowRecord("win-1", "docs", "/a"), windowRecord("win-9", "buddy", "/", { is_pinned: true })],
      }),
    ];
    socket.deliver().onDesktopsUpdated(api.desktops);
    m.redraw.sync();
    return document.querySelector('[data-taskbar-entry="win-9"]') as HTMLElement;
  }

  function openEntryMenuRow(entry: HTMLElement, key: string): void {
    entry.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, clientX: 10, clientY: 10 }));
    m.redraw.sync();
    (document.querySelector(`[data-menu-row="${key}"]`) as HTMLElement).click();
    m.redraw.sync();
  }

  it("routes the entry menu's style row and the chooser's Design your own... to the store", async () => {
    const entry = pinnedEntry();
    openEntryMenuRow(entry, "change-avatar");
    await settle();
    m.redraw.sync();
    (document.querySelector(".avatar-design-own") as HTMLElement).click();
    m.redraw.sync();
    expect(document.querySelector("[data-avatar-chooser]")).toBeNull();
    await settle();
    expect(api.calls).toContain(
      `launch:home:buddy:draft:${JSON.stringify({ message: AVATAR_DESIGN_PROMPT })}:window:win-9`,
    );
    expect(api.calls).toContain(`reportWindowLocation:home:win-9:${CLIENT}:/?chat=agent-1:`);
    expect(api.calls.some((call) => call.startsWith("openWindow:"))).toBe(false);

    openEntryMenuRow(entry, "style-plain");
    await settle();
    expect(api.calls).toContain("setEntryPresentation:client-1:buddy:bar:plain:-");
  });

  it("opens from the pinned entry's menu, and stays closed when dismissed before the catalog answered", async () => {
    const entry = pinnedEntry();
    const answerCatalog = api.holdReads();
    openEntryMenuRow(entry, "change-avatar");
    expect(document.querySelector("[data-avatar-chooser]")).not.toBeNull();
    expect(document.querySelector("[data-avatar-chooser] [role='status']")).not.toBeNull();

    (document.querySelector(".avatar-chooser-done") as HTMLElement).click();
    m.redraw.sync();
    expect(document.querySelector("[data-avatar-chooser]")).toBeNull();
    answerCatalog();
    await settle();
    m.redraw.sync();
    expect(document.querySelector("[data-avatar-chooser]")).toBeNull();
  });
});

describe("a press outside what is open", () => {
  it("closes an open menu through its own sheet, with the focused page shielded under it", () => {
    expect(focusedShield()).toBeNull();
    (document.querySelector('[data-window-control="menu"]') as HTMLElement).click();
    m.redraw.sync();
    expect(document.querySelector(".window-menu")).not.toBeNull();
    expect(focusedShield()).not.toBeNull();
    // The sheet covers the page, so the press that dismisses the menu cannot also reach it.
    const sheet = document.querySelector('[data-menu-part="sheet"]') as HTMLElement;
    sheet.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    m.redraw.sync();
    expect(document.querySelector(".window-menu")).toBeNull();
    expect(focusedShield()).toBeNull();
  });

  it("closes the launcher from a press into the focused page, which is shielded for it", () => {
    store.openLauncher();
    m.redraw.sync();
    const shield = focusedShield();
    expect(shield).not.toBeNull();
    pressOn(shield as HTMLElement);
    expect(store.isLauncherOpen()).toBe(false);
    expect(focusedShield()).toBeNull();
  });
});
