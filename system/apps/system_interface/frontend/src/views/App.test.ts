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
import { FakeDesktopApi, FakeDesktopSocket, settle } from "../testing/fakeShell";
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
  // The template catalog request the App fires on mount: a shell with no catalog configured.
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: true, json: async () => ({ catalog: null }) })),
  );
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
  vi.unstubAllGlobals();
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

describe("the avatar chooser", () => {
  // The pinned app takes a draft at its home path, so the chooser's prompt goes to this client's view of its window.
  const buddy = appRecord("buddy", {
    pin: { path: "/", style: "avatar", scope: "linked", default_mode: "bar" },
    launch_paths: [launchPathRecord({ id: "root", path: "/", params: ["draft"] })],
  });

  /** The desktop with buddy's pinned window, its entry in the bar in the avatar style; answers the entry. */
  function pinnedEntry(...apps: readonly ReturnType<typeof appRecord>[]): HTMLElement {
    socket.deliver().onAppsUpdated([appRecord("docs"), buddy, ...apps]);
    socket.deliver().onDesktopsUpdated([
      desktopRecord("home", {
        windows: [windowRecord("win-1", "docs", "/a"), windowRecord("win-9", "buddy", "/", { is_pinned: true })],
      }),
    ]);
    m.redraw.sync();
    return document.querySelector('[data-taskbar-entry="win-9"]') as HTMLElement;
  }

  function openEntryMenuRow(entry: HTMLElement, key: string): void {
    entry.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, clientX: 10, clientY: 10 }));
    m.redraw.sync();
    (document.querySelector(`[data-menu-item="${key}"]`) as HTMLElement).click();
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
    const prompt = new URLSearchParams({ draft: AVATAR_DESIGN_PROMPT }).toString();
    expect(api.calls).toContain(`reportWindowLocation:home:win-9:${CLIENT}:/?${prompt}:Buddy`);
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
