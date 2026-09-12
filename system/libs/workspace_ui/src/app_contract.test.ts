// @vitest-environment jsdom
//
// The app side of the contract is about what a real browser event carries -- a source
// window and a payload -- so these tests dispatch real MessageEvents at a real (jsdom)
// window whose parent is stood in for.

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  SHELL_CLOSE_REQUEST,
  SHELL_FOCUSED,
  SHELL_HANDSHAKE,
  SHELL_HIDDEN,
  SHELL_LOCATION,
  SHELL_NEW_TAB,
  SHELL_OPEN,
  SHELL_SHOWN,
  connectToShell,
  isNewTabChord,
} from "./app_contract";
import type { ChordKeys, ShellConnection } from "./app_contract";

const HANDSHAKE = {
  type: SHELL_HANDSHAKE,
  clientId: "client-1",
  deviceKind: "desktop",
  viewId: "everything",
  address: "app:chat?instance=agent-1",
  tabId: "chat-agent-1",
};

let connection: ShellConnection | null = null;
let parentSpy: { postMessage: ReturnType<typeof vi.fn> } | null = null;

/** Frame this window under a spy parent for the duration of the test. */
function framed(): { postMessage: ReturnType<typeof vi.fn> } {
  const parent = { postMessage: vi.fn() };
  Object.defineProperty(window, "parent", { value: parent, configurable: true });
  parentSpy = parent;
  return parent;
}

function deliver(data: unknown, source: unknown): void {
  window.dispatchEvent(new MessageEvent("message", { data, source: source as Window }));
}

afterEach(() => {
  connection?.disconnect();
  connection = null;
  Object.defineProperty(window, "parent", { value: window, configurable: true });
  parentSpy = null;
});

describe("connectToShell", () => {
  it("delivers the handshake, shown, hidden, and close-request from the parent only", () => {
    const parent = framed();
    const handlers = { onHandshake: vi.fn(), onShown: vi.fn(), onHidden: vi.fn(), onCloseRequest: vi.fn() };
    connection = connectToShell(handlers);

    deliver(HANDSHAKE, parent);
    deliver({ type: SHELL_SHOWN }, parent);
    deliver({ type: SHELL_HIDDEN }, parent);
    deliver({ type: SHELL_CLOSE_REQUEST }, parent);
    // A nested frame can post here but is not the parent.
    deliver({ type: SHELL_SHOWN }, {});
    deliver({ type: "shell:unknown" }, parent);
    deliver("not an object", parent);

    expect(handlers.onHandshake).toHaveBeenCalledTimes(1);
    expect(handlers.onHandshake).toHaveBeenCalledWith({
      clientId: "client-1",
      deviceKind: "desktop",
      viewId: "everything",
      address: "app:chat?instance=agent-1",
      tabId: "chat-agent-1",
    });
    expect(handlers.onShown).toHaveBeenCalledTimes(1);
    expect(handlers.onHidden).toHaveBeenCalledTimes(1);
    expect(handlers.onCloseRequest).toHaveBeenCalledTimes(1);
  });

  it("drops a handshake missing a field", () => {
    const parent = framed();
    const onHandshake = vi.fn();
    connection = connectToShell({ onHandshake });
    deliver({ ...HANDSHAKE, tabId: undefined }, parent);
    expect(onHandshake).not.toHaveBeenCalled();
  });

  it("posts focused, location, and open to the parent with the contract shapes", () => {
    const parent = framed();
    connection = connectToShell({});

    connection.focused();
    connection.location("/docs");
    connection.open("app:chat?instance=agent-2");
    connection.open("app:chat?instance=agent-3");

    expect(parent.postMessage.mock.calls).toEqual([
      [{ type: SHELL_FOCUSED }, "*"],
      [{ type: SHELL_LOCATION, path: "/docs" }, "*"],
      [{ type: SHELL_OPEN, address: "app:chat?instance=agent-2" }, "*"],
      [{ type: SHELL_OPEN, address: "app:chat?instance=agent-3" }, "*"],
    ]);
  });

  it("is inert on a top-level page", () => {
    const onShown = vi.fn();
    connection = connectToShell({ onShown });
    expect(connection.isFramed).toBe(false);
    connection.focused();
    deliver({ type: SHELL_SHOWN }, window);
    expect(onShown).not.toHaveBeenCalled();
    expect(parentSpy).toBeNull();
  });

  it("stops listening once disconnected", () => {
    const parent = framed();
    const onShown = vi.fn();
    const live = connectToShell({ onShown });
    live.disconnect();
    deliver({ type: SHELL_SHOWN }, parent);
    expect(onShown).not.toHaveBeenCalled();
  });
});

describe("isNewTabChord", () => {
  const keys = (over: Partial<ChordKeys>): ChordKeys => ({
    key: "t",
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    shiftKey: false,
    ...over,
  });

  it("takes Cmd+T on an Apple platform and Ctrl+T elsewhere", () => {
    expect(isNewTabChord(keys({ metaKey: true }), true)).toBe(true);
    expect(isNewTabChord(keys({ ctrlKey: true }), false)).toBe(true);
  });

  it("ignores the other platform's chord, so Ctrl+T stays text editing on a Mac", () => {
    expect(isNewTabChord(keys({ ctrlKey: true }), true)).toBe(false);
    expect(isNewTabChord(keys({ metaKey: true }), false)).toBe(false);
  });

  it("takes the shifted key a caps-lock or Shift press reports", () => {
    expect(isNewTabChord(keys({ key: "T", metaKey: true }), true)).toBe(true);
  });

  it("leaves every neighbouring chord alone", () => {
    expect(isNewTabChord(keys({}), true)).toBe(false);
    expect(isNewTabChord(keys({ key: "n", metaKey: true }), true)).toBe(false);
    expect(isNewTabChord(keys({ metaKey: true, shiftKey: true }), true)).toBe(false);
    expect(isNewTabChord(keys({ metaKey: true, altKey: true }), true)).toBe(false);
    expect(isNewTabChord(keys({ metaKey: true, ctrlKey: true }), true)).toBe(false);
  });
});

describe("the new-tab chord from a framed page", () => {
  it("carries the chord up to the shell and keeps the browser out of it", () => {
    const parent = framed();
    connection = connectToShell({});

    const event = new KeyboardEvent("keydown", { key: "t", metaKey: true, cancelable: true });
    Object.defineProperty(navigator, "userAgent", { value: "Mac OS X", configurable: true });
    window.dispatchEvent(event);

    expect(parent.postMessage).toHaveBeenCalledWith({ type: SHELL_NEW_TAB }, "*");
    expect(event.defaultPrevented).toBe(true);
  });

  it("stays out of the way on a page visited directly, which has no shell to open a tab in", () => {
    connection = connectToShell({});

    const event = new KeyboardEvent("keydown", { key: "t", metaKey: true, cancelable: true });
    window.dispatchEvent(event);

    expect(event.defaultPrevented).toBe(false);
  });

  it("stops listening once disconnected", () => {
    const parent = framed();
    connection = connectToShell({});
    connection.disconnect();
    connection = null;

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "t", metaKey: true, cancelable: true }));

    expect(parent.postMessage).not.toHaveBeenCalled();
  });
});
