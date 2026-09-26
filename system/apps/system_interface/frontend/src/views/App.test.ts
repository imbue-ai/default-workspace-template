// @vitest-environment jsdom
/**
 * The desktop's root over the fake shell: what the App owns beyond the views it composes, the
 * document-level keyboard and pointer handling around the launcher and the menus.
 */
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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

// The pinned app declares a launch path taking a draft, so a draft (the avatar chooser's prompt, an element
// reference) is launched into this client's view of its window.
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

describe("the avatar chooser", () => {
  function openEntryMenuRow(entry: HTMLElement, key: string): void {
    entry.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true, clientX: 10, clientY: 10 }));
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

describe("the element menu", () => {
  function menuRowKeys(): string[] {
    return Array.from(document.body.querySelectorAll('[data-menu-part="menu"] [data-menu-row]')).map(
      (row) => row.getAttribute("data-menu-row") ?? "",
    );
  }

  it("opens over the shell's own chrome on a right-click the views leave alone, with the reference rows", () => {
    const area = document.querySelector("[data-backdrop-area]") as HTMLElement;
    const event = new MouseEvent("contextmenu", { bubbles: true, cancelable: true, clientX: 30, clientY: 40 });
    area.dispatchEvent(event);
    m.redraw.sync();
    expect(event.defaultPrevented).toBe(true);
    expect(document.body.querySelector(".element-menu")).not.toBeNull();
    expect(menuRowKeys()).toEqual(["copy-reference", "explain-element", "modify-element"]);
    pressEscape();
    expect(document.body.querySelector('[data-menu-part="menu"]')).toBeNull();
  });

  it("stays closed for a right-click on a window's shield, which is the press that closes the launcher", () => {
    store.openLauncher();
    m.redraw.sync();
    const shield = focusedShield() as HTMLElement;
    expect(shield).not.toBeNull();
    shield.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    const event = new MouseEvent("contextmenu", { bubbles: true, cancelable: true, clientX: 30, clientY: 40 });
    shield.dispatchEvent(event);
    m.redraw.sync();
    expect(store.isLauncherOpen()).toBe(false);
    expect(event.defaultPrevented).toBe(true);
    expect(document.body.querySelector(".element-menu")).toBeNull();
  });

  it("ends a taskbar entry's menu with the reference rows, and drafts the reference through the store", async () => {
    pinnedEntry();
    const entry = document.querySelector('[data-taskbar-entry="win-1"]') as HTMLElement;
    entry.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true, clientX: 10, clientY: 10 }));
    m.redraw.sync();
    const keys = menuRowKeys();
    expect(keys.slice(-3)).toEqual(["copy-reference", "explain-element", "modify-element"]);
    expect(keys).toContain("close");
    (document.querySelector('[data-menu-row="explain-element"]') as HTMLElement).click();
    await settle();
    const launch = api.calls.find((call) => call.startsWith("launch:home:buddy:draft:")) ?? "";
    expect(launch).toContain("Explain what I attached in REF-");
    expect(launch).toContain('\\"window_id\\":null');
    expect(launch).toContain('\\"data-taskbar-entry\\":\\"win-1\\"');
    expect(launch).toContain('\\"app\\":\\"system_interface\\"');
    expect(launch.endsWith(":window:win-9")).toBe(true);
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

describe("a pulled-out window", () => {
  it("draws a ghost in place of the window, whose Bring back returns the window to the desktop", async () => {
    api.writeLayout("home", CLIENT, {
      updated_at: null,
      placements: [placementRecord("win-1", { is_detached: true })],
    });
    socket.deliver().onPlacementsUpdated({ desktopId: "home", clientId: CLIENT, saveId: "save-elsewhere" });
    await settle();
    m.redraw.sync();
    expect(document.querySelector('[data-detached-window="win-1"]')).not.toBeNull();
    expect(document.querySelector('[data-window-id="win-1"]')).toBeNull();
    expect(document.querySelector('[data-taskbar-entry="win-1"]')?.getAttribute("data-detached")).toBe("true");
    (document.querySelector('[data-ghost-action="bring-back"]') as HTMLElement).click();
    await settle();
    m.redraw.sync();
    expect(document.querySelector('[data-detached-window="win-1"]')).toBeNull();
    expect(document.querySelector('[data-window-id="win-1"]')).not.toBeNull();
    expect(document.querySelector('[data-taskbar-entry="win-1"]')?.getAttribute("data-detached")).toBe("false");
    expect(api.layoutOf("home", CLIENT).placements.find((p) => p.window_id === "win-1")?.is_detached).toBe(false);
  });
});

describe("a solo shell", () => {
  /** What the App observes for its size, recorded so a test can resize it: under jsdom every box measures as
   *  empty and the real observer never fires. */
  const observed: { element: Element; callback: ResizeObserverCallback }[] = [];

  beforeEach(() => {
    observed.length = 0;
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(private readonly callback: ResizeObserverCallback) {}
        observe(element: Element): void {
          observed.push({ element, callback: this.callback });
        }
        unobserve(): void {}
        disconnect(): void {}
      },
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /** Mount the App over a fresh store opened to show win-1 alone, the window pulled out in the stored layout. */
  async function mountSolo(): Promise<void> {
    unmountViews();
    api = new FakeDesktopApi();
    socket = new FakeDesktopSocket();
    api.desktops = [desktopRecord("home", { windows: [windowRecord("win-1", "docs", "/a")] })];
    api.writeLayout("home", CLIENT, {
      updated_at: null,
      placements: [placementRecord("win-1", { is_detached: true })],
    });
    store = new DesktopStore({
      clientId: CLIENT,
      api,
      socket,
      metrics: themeMetricsRecord(),
      modes: { isCompact: false, isTouch: false },
      redraw: () => m.redraw(),
      notify: () => undefined,
      reloadInterface: () => undefined,
      soloWindowId: "win-1",
    });
    await store.start(NO_LINK);
    socket.deliver().onAppsUpdated([appRecord("docs")]);
    mountView(() => m(App, { store, gestures, host: "127.0.0.1:8000", protocol: "http:" }));
  }

  /** Give the host a size and fire the App's observation of it, as a resize of the desktop window does. */
  function resizeHost(host: HTMLElement, width: number, height: number): void {
    host.getBoundingClientRect = () => ({ left: 0, top: 0, width, height }) as DOMRect;
    const watch = observed.find((candidate) => candidate.element === host);
    if (watch === undefined) throw new Error("the solo host is not observed");
    watch.callback([], {} as ResizeObserver);
    m.redraw.sync();
  }

  it("lays its one page over the whole host, live, and re-lays it as the host's size changes", async () => {
    await mountSolo();
    expect(document.querySelector("[data-backdrop-area]")).toBeNull();
    expect(document.querySelector("[data-taskbar]")).toBeNull();
    const host = document.querySelector('[data-solo-window="win-1"] .live-pages') as HTMLElement;
    resizeHost(host, 1000, 800);
    const page = document.querySelector('iframe[data-live-page="win-1"]')?.parentElement as HTMLElement;
    expect(page.style.display).toBe("");
    expect(page.style.pointerEvents).toBe("auto");
    expect([page.style.left, page.style.top, page.style.width, page.style.height]).toEqual([
      "0px",
      "0px",
      "1000px",
      "800px",
    ]);
    resizeHost(host, 1200, 900);
    expect([page.style.width, page.style.height]).toEqual(["1200px", "900px"]);
  });

  it("shows a note in place of the page once its window is gone from the desktop", async () => {
    await mountSolo();
    expect(document.querySelector("[data-solo-window-gone]")).toBeNull();
    socket.deliver().onDesktopsUpdated([desktopRecord("home")]);
    m.redraw.sync();
    expect(document.querySelector("[data-solo-window-gone]")).not.toBeNull();
    expect(document.querySelector('iframe[data-live-page="win-1"]')).toBeNull();
  });
});
