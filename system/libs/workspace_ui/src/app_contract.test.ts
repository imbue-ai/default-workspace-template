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
  SHELL_WIDGET_DRAG,
  SHELL_WIDGET_HIDDEN,
  SHELL_WIDGET_MENU,
  SHELL_WIDGET_PRESS,
  SHELL_WIDGET_TOOLTIP,
  SHELL_WIDGET_MODE,
  SHELL_WIDGET_PLACEMENT,
  SHELL_WIDGET_SIZE,
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

/** Connect this window under a spy parent that has handed it a shell's handshake. */
function shellFramed(): { postMessage: ReturnType<typeof vi.fn> } {
  const parent = framed();
  connection = connectToShell({});
  deliver(HANDSHAKE, parent);
  return parent;
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
      surface: "window",
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
      surface: "window",
    });
  });

  it("as a widget, takes its mode, reports its size and hiddenness, and sends no location or focus", () => {
    const parent = framed();
    const onWidgetMode = vi.fn();
    connection = connectToShell({ onWidgetMode });
    // Before any handshake the page is a window: a widget's reports go nowhere.
    connection.widgetSize(180, 64);
    expect(connection.surface()).toBe("window");

    deliver({ ...HANDSHAKE, windowId: null, surface: "widget", path: "/widget" }, parent);
    deliver({ type: SHELL_WIDGET_MODE, mode: "collapsed" }, parent);
    deliver({ type: SHELL_WIDGET_MODE, mode: "tiny" }, parent);
    connection.widgetSize(180, 64);
    connection.widgetHidden(true);
    connection.location("/widget", "Usage");
    connection.focused();

    expect(connection.surface()).toBe("widget");
    expect(onWidgetMode.mock.calls).toEqual([["collapsed"]]);
    expect(sentAfterConnect(parent)).toEqual([
      [{ type: SHELL_WIDGET_SIZE, width: 180, height: 64 }, "*"],
      [{ type: SHELL_WIDGET_HIDDEN, isHidden: true }, "*"],
    ]);
  });

  describe("a press on a widget's page", () => {
    /** A pointer event as a browser dispatches it (jsdom has no PointerEvent), at a screen point and the same
     *  client point. */
    function pointer(type: string, target: EventTarget, x: number, y: number, button = 0): void {
      const event = new MouseEvent(type, {
        bubbles: true,
        cancelable: true,
        button,
        clientX: x,
        clientY: y,
        screenX: x,
        screenY: y,
      });
      Object.defineProperty(event, "pointerId", { value: 1 });
      Object.defineProperty(event, "isPrimary", { value: true });
      target.dispatchEvent(event);
    }

    function widgetPage(html: string): { parent: { postMessage: ReturnType<typeof vi.fn> }; root: HTMLElement } {
      const parent = framed();
      connection = connectToShell({});
      deliver({ ...HANDSHAKE, windowId: null, surface: "widget", path: "/widget" }, parent);
      const root = document.createElement("div");
      root.innerHTML = html;
      document.body.appendChild(root);
      parent.postMessage.mockClear();
      return { parent, root };
    }

    afterEach(() => {
      document.body.innerHTML = "";
    });

    it("drags the widget once it travels past the threshold, and swallows the click that ends it", () => {
      const { parent, root } = widgetPage('<a id="link" href="/" target="_blank">Usage</a>');
      const link = root.querySelector("#link") as Element;
      const onClick = vi.fn();
      link.addEventListener("click", onClick);

      pointer("pointerdown", link, 10, 20);
      pointer("pointermove", link, 12, 21);
      pointer("pointermove", link, 30, 40);
      pointer("pointermove", link, 50, 60);
      pointer("pointerup", link, 50, 60);
      link.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 }));

      expect(parent.postMessage.mock.calls).toEqual([
        [
          {
            type: SHELL_WIDGET_DRAG,
            phase: "start",
            pressX: 10,
            pressY: 20,
            pressScreenX: 10,
            pressScreenY: 20,
            screenX: 30,
            screenY: 40,
          },
          "*",
        ],
        [{ type: SHELL_WIDGET_DRAG, phase: "move", screenX: 50, screenY: 60 }, "*"],
        [{ type: SHELL_WIDGET_DRAG, phase: "end", screenX: 50, screenY: 60 }, "*"],
      ]);
      // Neither the page nor the link rule saw the click, so no window was opened.
      expect(onClick).not.toHaveBeenCalled();
    });

    it("leaves a press that barely moves to be a click, and never drags from a control or as a window", () => {
      const { parent, root } = widgetPage(
        '<a id="link" href="/" target="_blank">Usage</a><button id="button">x</button>',
      );
      const link = root.querySelector("#link") as Element;
      pointer("pointerdown", link, 10, 20);
      pointer("pointermove", link, 12, 22);
      pointer("pointerup", link, 12, 22);
      link.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 }));
      expect(parent.postMessage.mock.calls).toEqual([[{ type: SHELL_OPEN, path: "/", ifPresent: "focus" }, "*"]]);

      parent.postMessage.mockClear();
      const button = root.querySelector("#button") as Element;
      pointer("pointerdown", button, 10, 20);
      pointer("pointermove", button, 60, 80);
      pointer("pointerup", button, 60, 80);
      // A secondary press is the page's too.
      pointer("pointerdown", link, 10, 20, 2);
      pointer("pointermove", link, 60, 80, 2);
      expect(parent.postMessage).not.toHaveBeenCalled();

      deliver(HANDSHAKE, parent);
      pointer("pointerdown", link, 10, 20);
      pointer("pointermove", link, 60, 80);
      pointer("pointerup", link, 60, 80);
      expect(parent.postMessage).not.toHaveBeenCalled();
    });
  });

  it("on a widget, reports the tooltip of the element the pointer enters, and none once it leaves or presses", () => {
    const parent = framed();
    connection = connectToShell({});
    deliver({ ...HANDSHAKE, windowId: null, surface: "widget", path: "/widget" }, parent);
    const root = document.createElement("div");
    root.innerHTML =
      '<div id="bar" data-widget-tooltip="5-hour limit: 61% used"><span id="fill"></span></div><div id="plain"></div>';
    document.body.appendChild(root);
    const bar = root.querySelector("#bar") as HTMLElement;
    bar.getBoundingClientRect = () => ({ left: 10, top: 20, width: 100, height: 8 }) as DOMRect;
    parent.postMessage.mockClear();
    const over = (target: Element, related: Element | null = null): void => {
      target.dispatchEvent(new MouseEvent("mouseover", { bubbles: true, relatedTarget: related }));
    };

    over(root.querySelector("#fill") as Element);
    // Moving within the same trigger says nothing more.
    over(bar);
    over(root.querySelector("#plain") as Element, bar);
    over(bar);
    bar.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true }));
    over(bar);
    root.querySelector("#plain")?.dispatchEvent(new MouseEvent("mouseout", { bubbles: true, relatedTarget: null }));
    root.remove();

    const shown = {
      type: SHELL_WIDGET_TOOLTIP,
      text: "5-hour limit: 61% used",
      left: 10,
      top: 20,
      width: 100,
      height: 8,
    };
    const hidden = { type: SHELL_WIDGET_TOOLTIP, text: null };
    expect(parent.postMessage.mock.calls.map(([message]) => message)).toEqual([
      shown,
      hidden,
      shown,
      hidden,
      shown,
      hidden,
    ]);
  });

  it("hands a widget's right-click to the shell, and in the taskbar its click instead of the link and no drag", () => {
    const parent = framed();
    const onWidgetMode = vi.fn();
    connection = connectToShell({ onWidgetMode });
    deliver({ ...HANDSHAKE, windowId: null, surface: "widget", path: "/widget" }, parent);
    const root = document.createElement("div");
    root.innerHTML = '<a id="link" href="/" target="_blank">Usage</a>';
    document.body.appendChild(root);
    const link = root.querySelector("#link") as HTMLElement;
    const click = (): void => {
      link.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 }));
    };
    parent.postMessage.mockClear();

    link.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true, clientX: 12, clientY: 7 }));
    // On the desktop, a click is the page's: its link opens the app.
    click();
    deliver({ type: SHELL_WIDGET_MODE, mode: "tray" }, parent);
    click();
    const press = (x: number): void => {
      const event = new MouseEvent(x === 0 ? "pointerdown" : "pointermove", {
        bubbles: true,
        button: 0,
        screenX: x,
        screenY: 0,
      });
      Object.defineProperty(event, "pointerId", { value: 1 });
      Object.defineProperty(event, "isPrimary", { value: true });
      link.dispatchEvent(event);
    };
    press(0);
    press(40);
    deliver({ type: SHELL_WIDGET_MODE, mode: "toolbar" }, parent);
    click();
    press(0);
    press(40);
    root.remove();

    expect(onWidgetMode.mock.calls).toEqual([["tray"], ["toolbar"]]);
    expect(parent.postMessage.mock.calls.map(([message]) => message)).toEqual([
      { type: SHELL_WIDGET_MENU, x: 12, y: 7 },
      { type: SHELL_OPEN, path: "/", ifPresent: "focus" },
      { type: SHELL_WIDGET_PRESS },
      { type: SHELL_WIDGET_PRESS },
    ]);
  });

  it("asks the shell to place its app's widget, from a window as from a widget", () => {
    const parent = shellFramed();
    parent.postMessage.mockClear();
    connection?.placeWidget("tray");
    connection?.placeWidget("toolbar");
    connection?.placeWidget("desktop");
    expect(parent.postMessage.mock.calls).toEqual([
      [{ type: SHELL_WIDGET_PLACEMENT, placement: "tray" }, "*"],
      [{ type: SHELL_WIDGET_PLACEMENT, placement: "toolbar" }, "*"],
      [{ type: SHELL_WIDGET_PLACEMENT, placement: "desktop" }, "*"],
    ]);
  });

  it("as a window, ignores a widget mode and sends no widget report", () => {
    const parent = shellFramed();
    const onWidgetMode = vi.fn();
    connection?.disconnect();
    connection = connectToShell({ onWidgetMode });
    deliver(HANDSHAKE, parent);
    deliver({ type: SHELL_WIDGET_MODE, mode: "expanded" }, parent);
    parent.postMessage.mockClear();
    connection.widgetSize(10, 10);
    connection.widgetHidden(false);

    expect(onWidgetMode).not.toHaveBeenCalled();
    expect(parent.postMessage).not.toHaveBeenCalled();
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

  it("has a shell once a handshake with a client id arrives from the parent, and none on a top-level page", () => {
    const parent = framed();
    const live = connectToShell({});
    expect(live.hasShell()).toBe(false);
    deliver({ ...HANDSHAKE, clientId: "" }, parent);
    deliver(HANDSHAKE, {});
    expect(live.hasShell()).toBe(false);
    deliver(HANDSHAKE, parent);
    expect(live.hasShell()).toBe(true);
    live.disconnect();
    Object.defineProperty(window, "parent", { value: window, configurable: true });
    connection = connectToShell({});
    deliver(HANDSHAKE, window);
    expect(connection.hasShell()).toBe(false);
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
    const parent = shellFramed();
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
    ["another app's share address", "https://files-ab12cd34.0123456789abcdef0123456789abcdef.us1.example.com/"],
    ["a web link", "https://example.com/docs"],
    ["mailto", "mailto:someone@example.com"],
    ["tel", "tel:+15551234567"],
    ["a custom-scheme link", "slack://channel?id=C1"],
  ])("hands %s to the shell and keeps the page where it is, whatever its target", (_what, href) => {
    const parent = connect();
    click(`<a href="${href}"><span data-click>open</span></a>`);
    click(`<a href="${href}" target="_blank">open</a>`);
    expect(sentAfterConnect(parent)).toEqual([
      [{ type: SHELL_OPEN_LINK, url: new URL(href).href }, "*"],
      [{ type: SHELL_OPEN_LINK, url: new URL(href).href }, "*"],
    ]);
    expect(cancelled).toEqual([true, true]);
    expect(opened).not.toHaveBeenCalled();
  });

  it("hands a modified click on another origin's link to the shell like a plain one", () => {
    const parent = connect();
    click('<a href="https://example.com/docs">out</a>', { metaKey: true });
    expect(sentAfterConnect(parent)).toEqual([[{ type: SHELL_OPEN_LINK, url: "https://example.com/docs" }, "*"]]);
    expect(cancelled).toEqual([true]);
    expect(opened).not.toHaveBeenCalled();
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

  it("leaves every link click to the browser until a shell's handshake arrives", () => {
    const parent = framed();
    connection = connectToShell({});
    window.addEventListener("click", observeClick);
    click('<a href="http://localhost:5173/">local</a>');
    click('<a href="https://example.com/" target="_blank">out</a>');
    click('<a href="/docs/intro" target="_blank">intro</a>');
    expect(sentAfterConnect(parent)).toEqual([]);
    expect(cancelled).toEqual([false, false, false]);
    deliver(HANDSHAKE, parent);
    click('<a href="https://example.com/" target="_blank">out</a>');
    expect(sentAfterConnect(parent)).toEqual([[{ type: SHELL_OPEN_LINK, url: "https://example.com/" }, "*"]]);
    expect(cancelled).toEqual([false, false, false, true]);
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

describe("a framed page's scripted popups", () => {
  let nativeOpen: ReturnType<typeof vi.fn>;
  let savedOpen: typeof window.open;

  beforeEach(() => {
    savedOpen = window.open;
    nativeOpen = vi.fn(() => ({ closed: false }) as unknown as Window);
    window.open = nativeOpen as unknown as typeof window.open;
  });

  afterEach(() => {
    connection?.disconnect();
    connection = null;
    window.open = savedOpen;
  });

  it.each([
    ["a local URL", "http://localhost:5173/preview?x=1"],
    ["another app's address", "http://files-ab12cd34.host-0123.localhost:8421/home/user/?view"],
    ["a file URL", "file:///home/user/workspace/plan.md"],
  ])("hands %s to the shell, opens no window, and answers null", (_what, url) => {
    const parent = shellFramed();
    expect(window.open(url, "_blank", "noopener")).toBeNull();
    expect(sentAfterConnect(parent)).toEqual([[{ type: SHELL_OPEN_LINK, url: new URL(url).href }, "*"]]);
    expect(nativeOpen).not.toHaveBeenCalled();
  });

  it("opens a page of its own app as a window of the app, its path and query kept", () => {
    const parent = shellFramed();
    expect(window.open("/docs/intro?tab=2#part")).toBeNull();
    expect(sentAfterConnect(parent)).toEqual([
      [{ type: SHELL_OPEN, path: "/docs/intro?tab=2", ifPresent: "focus" }, "*"],
    ]);
    expect(nativeOpen).not.toHaveBeenCalled();
  });

  it("leaves another site, a blank window, a call with no URL, and other schemes to the browser's own window.open", () => {
    const parent = shellFramed();
    const calls: unknown[][] = [
      ["https://example.com/sign-in", "signin", "popup"],
      ["mailto:someone@example.com"],
      ["slack://channel?id=C1"],
      ["about:blank"],
      [],
      [""],
      ["javascript:void(0)"],
    ];
    for (const args of calls) {
      expect((window.open as (...a: unknown[]) => Window | null)(...args)).toEqual({ closed: false });
    }
    expect(nativeOpen.mock.calls).toEqual(calls);
    expect(sentAfterConnect(parent)).toEqual([]);
  });

  it("follows a call naming _self as a plain click, and leaves one naming a frame of the page to the browser", () => {
    const parent = shellFramed();
    document.body.innerHTML = '<iframe name="preview"></iframe>';
    const open = window.open as (...a: unknown[]) => Window | null;
    expect(open("/docs/intro", "_self")).toEqual({ closed: false });
    expect(open("http://localhost:5173/preview", "_SELF")).toBeNull();
    expect(open("https://example.com/docs", "_self")).toBeNull();
    expect(open("slack://channel?id=C1", "_self")).toBeNull();
    expect(open("http://localhost:5173/preview", "preview")).toEqual({ closed: false });
    expect(open("/docs/intro", "preview")).toEqual({ closed: false });
    expect(nativeOpen.mock.calls).toEqual([
      ["/docs/intro", "_self"],
      ["http://localhost:5173/preview", "preview"],
      ["/docs/intro", "preview"],
    ]);
    expect(sentAfterConnect(parent)).toEqual([
      [{ type: SHELL_OPEN_LINK, url: "http://localhost:5173/preview" }, "*"],
      [{ type: SHELL_OPEN_LINK, url: "https://example.com/docs" }, "*"],
      [{ type: SHELL_OPEN_LINK, url: "slack://channel?id=C1" }, "*"],
    ]);
    document.body.innerHTML = "";
  });

  it("leaves every popup to the browser's own window.open until a shell's handshake arrives", () => {
    const parent = framed();
    connection = connectToShell({});
    expect(window.open("http://localhost:5173/preview")).toEqual({ closed: false });
    expect(window.open("/docs/intro")).toEqual({ closed: false });
    expect(nativeOpen.mock.calls).toEqual([["http://localhost:5173/preview"], ["/docs/intro"]]);
    expect(sentAfterConnect(parent)).toEqual([]);
  });

  it("leaves window.open alone on a top-level page, and puts it back once disconnected", () => {
    connection = connectToShell({});
    expect(window.open).toBe(nativeOpen);
    connection.disconnect();
    framed();
    connection = connectToShell({});
    expect(window.open).not.toBe(nativeOpen);
    connection.disconnect();
    connection = null;
    expect(window.open).toBe(nativeOpen);
  });
});
