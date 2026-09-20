// @vitest-environment jsdom
//
// The app side of the contract is about what a real browser event carries -- a source
// window and a payload -- so these tests dispatch real MessageEvents at a real (jsdom)
// window whose parent is stood in for.

import { afterEach, describe, expect, it, vi } from "vitest";
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
  ShellContractError,
  connectToShell,
} from "./app_contract";
import type { ShellConnection } from "./app_contract";

const HANDSHAKE = {
  type: SHELL_HANDSHAKE,
  clientId: "client-1",
  windowId: "win-1",
  desktopId: "home",
  path: "/?chat=agent-1",
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

/** The messages a spy parent received after the capabilities announcement every connect sends first. */
function sentAfterConnect(parent: { postMessage: ReturnType<typeof vi.fn> }): unknown[][] {
  return parent.postMessage.mock.calls.slice(1);
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
      windowId: "win-1",
      desktopId: "home",
      path: "/?chat=agent-1",
    });
    expect(handlers.onShown).toHaveBeenCalledTimes(1);
    expect(handlers.onHidden).toHaveBeenCalledTimes(1);
    expect(handlers.onCloseRequest).toHaveBeenCalledTimes(1);
  });

  it("drops a handshake without a client id", () => {
    const parent = framed();
    const onHandshake = vi.fn();
    connection = connectToShell({ onHandshake });
    deliver({ ...HANDSHAKE, clientId: undefined }, parent);
    deliver({ ...HANDSHAKE, clientId: "" }, parent);
    expect(onHandshake).not.toHaveBeenCalled();
  });

  it("reads a field the shell does not send as empty, and ignores fields it does not know", () => {
    const parent = framed();
    const onHandshake = vi.fn();
    connection = connectToShell({ onHandshake });
    deliver({ type: SHELL_HANDSHAKE, clientId: "client-1", desktopId: "home", viewId: "home" }, parent);
    expect(onHandshake).toHaveBeenCalledWith({ clientId: "client-1", windowId: "", desktopId: "home", path: "" });
  });

  it("announces its capabilities to the parent once, before anything else", () => {
    const parent = framed();
    connection = connectToShell({});
    expect(parent.postMessage.mock.calls).toEqual([[{ type: SHELL_CAPABILITIES, navigation: false }, "*"]]);

    connection.disconnect();
    const navigating = connectToShell({ onNavigate: vi.fn(), capabilities: { navigation: true } });
    expect(parent.postMessage.mock.calls[1]).toEqual([{ type: SHELL_CAPABILITIES, navigation: true }, "*"]);
    navigating.disconnect();
  });

  it("refuses a navigate handler without the capability, and the capability without a handler", () => {
    framed();
    expect(() => connectToShell({ onNavigate: vi.fn() })).toThrow(ShellContractError);
    expect(() => connectToShell({ capabilities: { navigation: true } })).toThrow(ShellContractError);
  });

  it("delivers a navigate with a string path from the parent only", () => {
    const parent = framed();
    const onNavigate = vi.fn();
    connection = connectToShell({ onNavigate, capabilities: { navigation: true } });

    deliver({ type: SHELL_NAVIGATE, path: "/?chat=agent-2" }, parent);
    deliver({ type: SHELL_NAVIGATE, path: 7 }, parent);
    deliver({ type: SHELL_NAVIGATE, path: "/elsewhere" }, {});

    expect(onNavigate.mock.calls).toEqual([["/?chat=agent-2"]]);
  });

  it("posts focused, location, open, and openPath to the parent with the contract shapes", () => {
    const parent = framed();
    connection = connectToShell({});

    connection.focused();
    connection.location("/docs", "Docs");
    connection.open("app:chat?instance=agent-2");
    connection.openPath("/?chat=agent-3", "focus");
    connection.openPath("/new", "new");

    expect(sentAfterConnect(parent)).toEqual([
      [{ type: SHELL_FOCUSED }, "*"],
      [{ type: SHELL_LOCATION, path: "/docs", title: "Docs" }, "*"],
      [{ type: SHELL_OPEN, address: "app:chat?instance=agent-2" }, "*"],
      [{ type: SHELL_OPEN, path: "/?chat=agent-3", ifPresent: "focus" }, "*"],
      [{ type: SHELL_OPEN, path: "/new", ifPresent: "new" }, "*"],
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
