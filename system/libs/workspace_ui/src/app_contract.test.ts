// @vitest-environment jsdom
//
// The app side of the contract is about what a real browser event carries -- a source
// window and a payload -- so these tests dispatch real MessageEvents at a real (jsdom)
// window whose parent is stood in for.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  SHELL_CAPABILITIES,
  SHELL_CLOSE_REQUEST,
  SHELL_DRAFT_TEXT,
  SHELL_FOCUSED,
  SHELL_HANDSHAKE,
  SHELL_HIDDEN,
  SHELL_LOCATION,
  SHELL_MESSAGE,
  SHELL_NAVIGATE,
  SHELL_OPEN,
  SHELL_OPEN_LINK,
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
  app: "chat",
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
      app: "chat",
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
    expect(onHandshake).toHaveBeenCalledWith({
      clientId: "client-1",
      windowId: "",
      desktopId: "home",
      app: "",
      path: "",
    });
  });

  it("announces its capabilities to the parent once, before anything else", () => {
    const parent = framed();
    connection = connectToShell({});
    expect(parent.postMessage.mock.calls).toEqual([
      [{ type: SHELL_CAPABILITIES, navigation: false, closeChord: false }, "*"],
    ]);

    connection.disconnect();
    const navigating = connectToShell({
      onNavigate: vi.fn(),
      capabilities: { navigation: true, closeChord: false },
    });
    expect(parent.postMessage.mock.calls[1]).toEqual([
      { type: SHELL_CAPABILITIES, navigation: true, closeChord: false },
      "*",
    ]);
    navigating.disconnect();

    const closing = connectToShell({
      onCloseRequest: vi.fn(),
      capabilities: { navigation: false, closeChord: true },
    });
    expect(parent.postMessage.mock.calls[2]).toEqual([
      { type: SHELL_CAPABILITIES, navigation: false, closeChord: true },
      "*",
    ]);
    closing.disconnect();
  });

  it("refuses a navigate handler without the capability, and the capability without a handler", () => {
    framed();
    expect(() => connectToShell({ onNavigate: vi.fn() })).toThrow(ShellContractError);
    expect(() => connectToShell({ capabilities: { navigation: true, closeChord: false } })).toThrow(
      ShellContractError,
    );
  });

  it("refuses the close-chord capability without a close handler", () => {
    framed();
    expect(() => connectToShell({ capabilities: { navigation: false, closeChord: true } })).toThrow(
      ShellContractError,
    );
  });

  it("delivers a navigate with a string path from the parent only", () => {
    const parent = framed();
    const onNavigate = vi.fn();
    connection = connectToShell({ onNavigate, capabilities: { navigation: true, closeChord: false } });

    deliver({ type: SHELL_NAVIGATE, path: "/?chat=agent-2" }, parent);
    deliver({ type: SHELL_NAVIGATE, path: 7 }, parent);
    deliver({ type: SHELL_NAVIGATE, path: "/elsewhere" }, {});

    expect(onNavigate.mock.calls).toEqual([["/?chat=agent-2"]]);
  });

  it("posts focused, location, openPath, draftText, sendMessage, and openLink to the parent with the contract shapes", () => {
    const parent = framed();
    connection = connectToShell({});

    connection.focused();
    connection.location("/docs", "Docs");
    connection.openPath("/?chat=agent-3", "focus");
    connection.openPath("/new", "new");
    connection.draftText("Explain this element:");
    connection.sendMessage("open:file", { path: "/home/user/plan.md", type: "ignored" });
    connection.openLink("http://files-ab12cd34.host-0123.localhost:8421/");

    expect(sentAfterConnect(parent)).toEqual([
      [{ type: SHELL_FOCUSED }, "*"],
      [{ type: SHELL_LOCATION, path: "/docs", title: "Docs" }, "*"],
      [{ type: SHELL_OPEN, path: "/?chat=agent-3", ifPresent: "focus" }, "*"],
      [{ type: SHELL_OPEN, path: "/new", ifPresent: "new" }, "*"],
      [{ type: SHELL_DRAFT_TEXT, text: "Explain this element:" }, "*"],
      [{ type: SHELL_MESSAGE, message: { type: "open:file", path: "/home/user/plan.md" } }, "*"],
      [{ type: SHELL_OPEN_LINK, url: "http://files-ab12cd34.host-0123.localhost:8421/" }, "*"],
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

describe("a framed page's link clicks", () => {
  /** Whether each click was cancelled, as the last listener of its bubbling sees it; the click is then cancelled, so
   *  the test document never navigates. */
  let cancelled: boolean[] = [];
  const observeClick = (event: Event): void => {
    cancelled.push(event.defaultPrevented);
    event.preventDefault();
  };
  let opened: ReturnType<typeof vi.spyOn>;

  function connect(): { postMessage: ReturnType<typeof vi.fn> } {
    const parent = framed();
    connection = connectToShell({});
    window.addEventListener("click", observeClick);
    window.addEventListener("auxclick", observeClick);
    return parent;
  }

  function click(html: string, init: MouseEventInit = {}, type: "click" | "auxclick" = "click"): void {
    document.body.innerHTML = html;
    const target = document.querySelector("[data-click]") ?? document.querySelector("a, area");
    target?.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, composed: true, ...init }));
  }

  beforeEach(() => {
    cancelled = [];
    opened = vi.spyOn(window, "open").mockImplementation(() => null);
  });

  afterEach(() => {
    window.removeEventListener("click", observeClick);
    window.removeEventListener("auxclick", observeClick);
    opened.mockRestore();
    document.body.innerHTML = "";
  });

  it.each([
    ["a local URL", "http://localhost:5173/preview?x=1"],
    ["a file URL", "file:///home/user/workspace/plan.md"],
    ["another app's address", "http://files-ab12cd34.host-0123.localhost:8421/home/user/?view"],
  ])("hands %s to the shell and keeps the page where it is", (_what, href) => {
    const parent = connect();
    click(`<a href="${href}"><span data-click>open</span></a>`);
    expect(sentAfterConnect(parent)).toEqual([[{ type: SHELL_OPEN_LINK, url: new URL(href).href }, "*"]]);
    expect(cancelled).toEqual([true]);
    expect(opened).not.toHaveBeenCalled();
  });

  it.each([
    ["a web link", "https://example.com/docs"],
    ["mailto", "mailto:someone@example.com"],
  ])("opens %s in a new browsing context, which Imbue Studio sends to the user's browser", (_what, href) => {
    const parent = connect();
    click(`<a href="${href}">out</a>`);
    expect(opened.mock.calls).toEqual([[new URL(href).href, "_blank", "noopener"]]);
    expect(sentAfterConnect(parent)).toEqual([]);
    expect(cancelled).toEqual([true]);
  });

  it("leaves a plain click on a link to the page's own origin to the page", () => {
    const parent = connect();
    click('<a href="/docs/intro?tab=2">intro</a>');
    expect(sentAfterConnect(parent)).toEqual([]);
    expect(cancelled).toEqual([false]);
  });

  it.each<[string, string, MouseEventInit, "click" | "auxclick"]>([
    ["a link naming a new browsing context", ' target="_blank"', {}, "click"],
    ["a link naming the top browsing context", ' target="_top"', {}, "click"],
    ["a link naming a browsing context that is no frame of the page", ' target="docs"', {}, "click"],
    ["a command-click", "", { metaKey: true }, "click"],
    ["a control-click", "", { ctrlKey: true }, "click"],
    ["a shift-click", "", { shiftKey: true }, "click"],
    ["a middle click", "", { button: 1 }, "auxclick"],
  ])("opens a page of its own app beside it for %s", (_what, attributes, init, type) => {
    const parent = connect();
    click(`<a href="/docs/intro?tab=2#part"${attributes}>intro</a>`, init, type);
    expect(sentAfterConnect(parent)).toEqual([
      [{ type: SHELL_OPEN, path: "/docs/intro?tab=2", ifPresent: "focus" }, "*"],
    ]);
    expect(cancelled).toEqual([true]);
  });

  it("leaves a click the page handled, a download, another scheme, and a right-button click to the page", () => {
    const parent = connect();
    document.body.innerHTML = '<a id="handled" href="http://localhost:5173/">handled</a>';
    document.getElementById("handled")?.addEventListener("click", (event) => event.preventDefault());
    document.getElementById("handled")?.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    click('<a href="http://localhost:5173/report.csv" download>report</a>');
    click('<a href="javascript:void(0)">run</a>');
    click('<a href="http://localhost:5173/">menu</a>', { button: 2 }, "auxclick");
    expect(sentAfterConnect(parent)).toEqual([]);
    expect(opened).not.toHaveBeenCalled();
    expect(cancelled).toEqual([true, false, false, false]);
  });

  it("leaves a plain click on a link into a frame of the page to the page, and opens a window for a modified one", () => {
    const parent = connect();
    const frameAndLink = (href: string): string =>
      `<iframe name="preview"></iframe><a href="${href}" target="preview">x</a>`;
    click(frameAndLink("http://localhost:5173/"));
    click(frameAndLink("/docs/intro"));
    click(frameAndLink("/docs/intro"), { metaKey: true });
    expect(sentAfterConnect(parent)).toEqual([[{ type: SHELL_OPEN, path: "/docs/intro", ifPresent: "focus" }, "*"]]);
    expect(cancelled).toEqual([false, false, true]);
  });

  it("leaves every link click alone on a top-level page, and once disconnected", () => {
    connection = connectToShell({});
    window.addEventListener("click", observeClick);
    click('<a href="http://localhost:5173/">local</a>');
    click('<a href="https://example.com/">out</a>');
    const parent = framed();
    const live = connectToShell({});
    live.disconnect();
    click('<a href="http://localhost:5173/">local</a>');
    expect(parent.postMessage.mock.calls).toEqual([
      [{ type: SHELL_CAPABILITIES, navigation: false, closeChord: false }, "*"],
    ]);
    expect(opened).not.toHaveBeenCalled();
    expect(cancelled).toEqual([false, false, false]);
  });
});
