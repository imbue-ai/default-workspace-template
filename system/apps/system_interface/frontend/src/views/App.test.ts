// @vitest-environment jsdom
/**
 * The desktop's root over the fake shell: what the App owns beyond the views it composes, the
 * document-level keyboard handling around the launcher.
 */
import "../testing/dom";
import { mountView, unmountViews } from "../testing/mount";
import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { GestureSource } from "../gestures/pointerGestures";
import { DesktopStore } from "../store/DesktopStore";
import { FakeDesktopApi, FakeDesktopSocket } from "../testing/fakeShell";
import { appRecord, desktopRecord, themeMetricsRecord, windowRecord } from "../testing/records";
import { App } from "./App";

const CLIENT = "client-1";
const NO_LINK = { desktopId: null, open: null, launch: null };
const gestures: GestureSource = { attach: () => () => undefined };

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
});
