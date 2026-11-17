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
  SHELL_INPUT,
  SHELL_LOCATION,
  SHELL_MESSAGE,
  SHELL_NAVIGATE,
  SHELL_OPEN,
  SHELL_OPEN_LINK,
  SHELL_REPORT_BUG,
  SHELL_SHOWN,
  SHELL_KEYBINDINGS_CHANGED,
  SHELL_START_NEW_WITH_TEXT,
  SHELL_START_NEW_WITH_TEXT_RESULT,
  SHELL_START_WITH_TEXT,
  SHELL_START_WITH_TEXT_RESULT,
  CHANGE_NOT_ANSWERED_SENTENCE,
  SHELL_THEME,
  ShellContractError,
  askAgentToFix,
  bugReportText,
  withAskFailure,
  connectToShell,
  followShellTheme,
  reportedFailureOf,
} from "./app_contract";
import type { ShellConnection, StartNewOutcome } from "./app_contract";

const HANDSHAKE = {
  type: SHELL_HANDSHAKE,
  clientId: "client-1",
  windowId: "win-1",
  desktopId: "home",
  app: "chat",
  path: "/?chat=agent-1",
  shellId: "shell-1",
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
      canReportBug: false,
      opensLocalFiles: false,
      isViewing: false,
      shellId: "shell-1",
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
      canReportBug: false,
      opensLocalFiles: false,
      isViewing: false,
      shellId: "",
    });
  });

  it("reads whether the embedder opens its shared-files settings, and anything but true as no", () => {
    const parent = framed();
    const onHandshake = vi.fn();
    connection = connectToShell({ onHandshake });
    deliver({ ...HANDSHAKE, opensLocalFiles: true }, parent);
    deliver({ ...HANDSHAKE, opensLocalFiles: "yes" }, parent);
    expect(onHandshake.mock.calls.map((call) => call[0].opensLocalFiles)).toEqual([true, false]);
  });

  it("reads whether the window is being viewed on somebody else's desktop, and anything but true as no", () => {
    const parent = framed();
    const onHandshake = vi.fn();
    connection = connectToShell({ onHandshake });
    deliver({ ...HANDSHAKE, isViewing: true }, parent);
    deliver(HANDSHAKE, parent);
    expect(onHandshake.mock.calls.map((call) => call[0].isViewing)).toEqual([true, false]);
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

  it("posts focused, location, openPath, the text starts, draftText, sendMessage, and openLink with the contract shapes", () => {
    const parent = shellFramed();
    const live = connection as ShellConnection;

    live.focused();
    live.location("/docs", "Docs");
    live.openPath("/?chat=agent-3", "focus");
    live.openPath("/new", "new");
    live.startWithText("Free up space.");
    live.startNewWithText("Fix this.");
    live.draftText("Explain this element:");
    live.sendMessage("open:file", { path: "/home/user/plan.md", type: "ignored" });
    live.openLink("http://files-ab12cd34.host-0123.localhost:8421/");

    expect(sentAfterConnect(parent)).toEqual([
      [{ type: SHELL_FOCUSED }, "*"],
      [{ type: SHELL_LOCATION, path: "/docs", title: "Docs" }, "*"],
      [{ type: SHELL_OPEN, path: "/?chat=agent-3", ifPresent: "focus" }, "*"],
      [{ type: SHELL_OPEN, path: "/new", ifPresent: "new" }, "*"],
      [{ type: SHELL_START_WITH_TEXT, text: "Free up space.", requestId: expect.any(String) }, "*"],
      [{ type: SHELL_START_NEW_WITH_TEXT, text: "Fix this.", requestId: expect.any(String) }, "*"],
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

  it("can report a bug while the shell's latest handshake says so, and asks the shell to open the report", () => {
    const parent = framed();
    connection = connectToShell({});
    expect(connection.canReportBug()).toBe(false);
    deliver(HANDSHAKE, parent);
    expect(connection.canReportBug()).toBe(false);
    // The shell hands every page a fresh handshake once Imbue Studio says it opens its bug report.
    deliver({ ...HANDSHAKE, canReportBug: true }, parent);
    expect(connection.canReportBug()).toBe(true);
    deliver({ ...HANDSHAKE, canReportBug: false }, {});
    expect(connection.canReportBug()).toBe(true);
    connection.reportBug({ message: "Docs couldn't save.", details: "HTTP 500\nboom" });
    expect(parent.postMessage).toHaveBeenLastCalledWith(
      { type: SHELL_REPORT_BUG, message: "Docs couldn't save.", details: "HTTP 500\nboom" },
      "*",
    );
    deliver({ ...HANDSHAKE, canReportBug: "yes" }, parent);
    expect(connection.canReportBug()).toBe(false);
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

  it("re-fetch request on a keybindings change reaches the handler", () => {
    const parent = framed();
    const onKeybindingsChanged = vi.fn();
    connection = connectToShell({ onKeybindingsChanged });
    deliver({ type: SHELL_KEYBINDINGS_CHANGED }, parent);
    expect(onKeybindingsChanged).toHaveBeenCalledTimes(1);
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

describe("a framed page's input reports", () => {
  it("posts shell:input on a pointer press or a key, at most once per interval", () => {
    vi.useFakeTimers();
    try {
      const parent = shellFramed();
      const inputs = (): unknown[][] =>
        sentAfterConnect(parent).filter(([message]) => (message as { type?: string }).type === SHELL_INPUT);

      window.dispatchEvent(new Event("pointerdown"));
      window.dispatchEvent(new Event("keydown"));
      expect(inputs()).toEqual([[{ type: SHELL_INPUT }, "*"]]);

      // Past the throttle the next touch reports again; activity is measured in minutes, so this loses nothing.
      vi.setSystemTime(Date.now() + 21_000);
      window.dispatchEvent(new Event("keydown"));
      expect(inputs()).toHaveLength(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("reports no input from an unframed page, which the shell's own window already hears", () => {
    connection = connectToShell({});
    const posted = vi.spyOn(window, "postMessage");
    window.dispatchEvent(new Event("pointerdown"));
    expect(posted).not.toHaveBeenCalled();
    posted.mockRestore();
  });
});

describe("followShellTheme", () => {
  it("follows the parent's theme messages alone, without posting anything", () => {
    const parent = framed();
    const onTheme = vi.fn();
    const stop = followShellTheme(onTheme);

    deliver({ type: SHELL_THEME, theme: "windows-2000", revision: "r1" }, parent);
    deliver({ type: SHELL_THEME, theme: "mac-classic" }, parent);
    deliver({ type: SHELL_THEME, theme: 7 }, parent);
    deliver({ type: SHELL_THEME, theme: "mac-classic", revision: "r2" }, {});
    deliver({ type: SHELL_SHOWN }, parent);
    stop();
    deliver({ type: SHELL_THEME, theme: "standard", revision: "r3" }, parent);

    expect(onTheme.mock.calls).toEqual([
      ["windows-2000", "r1", false],
      ["mac-classic", "", false],
    ]);
    expect(parent.postMessage).not.toHaveBeenCalled();
  });
});

describe("connectToShell's theme", () => {
  it("delivers the parent's theme with its revision, an empty revision when it names none, and a preview flag", () => {
    const parent = framed();
    const onTheme = vi.fn();
    connection = connectToShell({ onTheme });

    deliver({ type: SHELL_THEME, theme: "windows-2000", revision: "r1" }, parent);
    deliver({ type: SHELL_THEME, theme: "mac-classic" }, parent);
    deliver({ type: SHELL_THEME, theme: "paper", revision: "r2", isPreview: true }, parent);
    deliver({ type: SHELL_THEME, theme: 7, revision: "r3" }, parent);
    deliver({ type: SHELL_THEME, theme: "ink", revision: "r4" }, {});

    expect(onTheme.mock.calls).toEqual([
      ["windows-2000", "r1", false],
      ["mac-classic", "", false],
      ["paper", "r2", true],
    ]);
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

describe("askAgentToFix", () => {
  /** The text the shell was asked to start something with. */
  function startedText(parent: { postMessage: ReturnType<typeof vi.fn> }): string {
    const [message] = parent.postMessage.mock.lastCall as [Record<string, unknown>, string];
    expect(message.type).toBe(SHELL_START_NEW_WITH_TEXT);
    return message.text as string;
  }

  it("asks the shell to start a new chat whose task says what failed and carries the details as they are", () => {
    const parent = shellFramed();
    const details = "HTTP 502\ndocs answered the launch with 500: boom\n  at render (``` in a template)";
    askAgentToFix(connection as ShellConnection, { whatFailed: "Docs couldn't open that", details });

    const text = startedText(parent);
    expect(text.startsWith("Docs couldn't open that.\n\n")).toBe(true);
    // The details sit whole inside a fence the backticks among them cannot close.
    const fenced = text.match(/^(`{4,})\n([\s\S]*?)\n\1$/m);
    expect(fenced?.[2]).toBe(details);
  });

  it("gives a failure without details as its sentence and the task alone", () => {
    const parent = shellFramed();
    askAgentToFix(connection as ShellConnection, { whatFailed: "This window couldn't be closed.", details: "  " });
    const text = startedText(parent);
    expect(text.startsWith("This window couldn't be closed.\n\n")).toBe(true);
    expect(text).not.toContain("`");
    expect(text).not.toContain("closed..");
  });
});

describe("startNewWithText's answer", () => {
  /** The request id of the last ``shell:start-new-with-text`` the page sent. */
  function lastRequestId(parent: { postMessage: ReturnType<typeof vi.fn> }): string {
    const [message] = parent.postMessage.mock.lastCall as [Record<string, unknown>, string];
    expect(message.type).toBe(SHELL_START_NEW_WITH_TEXT);
    return message.requestId as string;
  }

  /** Whether ``promise`` has settled by the time the microtasks queued now have run. */
  async function settledValue<T>(promise: Promise<T>): Promise<T | "pending"> {
    return Promise.race([promise, new Promise<"pending">((resolve) => setTimeout(() => resolve("pending"), 0))]);
  }

  it("settles on the shell's answer to its own request, and on no other", async () => {
    const parent = shellFramed();
    const live = connection as ShellConnection;
    const first = live.startNewWithText("Fix this.");
    const firstId = lastRequestId(parent);
    const second = live.startNewWithText("Fix that.");
    const secondId = lastRequestId(parent);
    expect(secondId).not.toBe(firstId);

    deliver({ type: SHELL_START_NEW_WITH_TEXT_RESULT, requestId: firstId, isOpened: true }, {});
    deliver({ type: SHELL_START_NEW_WITH_TEXT_RESULT, requestId: "someone-else", isOpened: true }, parent);
    expect(await settledValue(first)).toBe("pending");

    const failure = { message: "Chat couldn't open that.", details: "HTTP 400\nno account", isReportable: false };
    deliver({ type: SHELL_START_NEW_WITH_TEXT_RESULT, requestId: secondId, isOpened: false, failure }, parent);
    deliver({ type: SHELL_START_NEW_WITH_TEXT_RESULT, requestId: firstId, isOpened: true }, parent);
    expect(await first).toEqual({ isOpened: true });
    expect(await second).toEqual({ isOpened: false, failure });
  });

  it("tells the shell a request's outcome is shown by the page only when the caller says so", () => {
    const parent = shellFramed();
    const live = connection as ShellConnection;
    void live.startWithText("Add a routine: water the plants");
    void live.startNewWithText("Fix this.");
    void live.startNewWithText("Fix that.", { showsOutcome: true });
    const sent = (parent.postMessage.mock.calls as [Record<string, unknown>, string][]).slice(-3).map(([m]) => m);
    expect(sent.map((message) => "showsOutcome" in message)).toEqual([false, false, true]);
    expect(sent[2].showsOutcome).toBe(true);
  });

  it("keeps a request pending through the same shell's handshakes, and settles it on the answer", async () => {
    const parent = shellFramed();
    const pending = (connection as ShellConnection).startNewWithText("Fix this.");
    const requestId = lastRequestId(parent);
    // The shell greets a page again when the embedder says it opens a bug report, a window moves, viewing changes.
    deliver({ ...HANDSHAKE, canReportBug: true }, parent);
    deliver({ ...HANDSHAKE, desktopId: "work", isViewing: true }, parent);
    expect(await settledValue(pending)).toBe("pending");
    deliver({ type: SHELL_START_NEW_WITH_TEXT_RESULT, requestId, isOpened: true }, parent);
    expect(await pending).toEqual({ isOpened: true });
  });

  it("answers startWithText the same way, through shell:start-with-text-result, late answers included", async () => {
    const parent = shellFramed();
    const live = connection as ShellConnection;
    const late: StartNewOutcome[] = [];
    const asked = live.startWithText("Free up space.", {
      showsOutcome: true,
      onLateAnswer: (outcome) => late.push(outcome),
    });
    const [message] = parent.postMessage.mock.lastCall as [Record<string, unknown>, string];
    expect(message).toMatchObject({ type: SHELL_START_WITH_TEXT, text: "Free up space.", showsOutcome: true });
    deliver({ ...HANDSHAKE, canReportBug: true }, parent);
    expect(await settledValue(asked)).toBe("pending");
    deliver({ ...HANDSHAKE, shellId: "shell-2" }, parent);
    expect((await asked).isOpened).toBe(false);
    const failure = { message: "Chat couldn't open that.", details: "HTTP 400", isReportable: false };
    deliver({ type: SHELL_START_WITH_TEXT_RESULT, requestId: message.requestId, isOpened: false, failure }, parent);
    expect(late).toEqual([{ isOpened: false, failure }]);

    const second = live.startWithText("Free up space.");
    const [again] = parent.postMessage.mock.lastCall as [Record<string, unknown>, string];
    deliver({ type: SHELL_START_WITH_TEXT_RESULT, requestId: again.requestId, isOpened: true }, parent);
    expect(await second).toEqual({ isOpened: true });
  });

  it("settles a request as not answered once a shell that started again greets the page", async () => {
    const parent = shellFramed();
    const pending = (connection as ShellConnection).startNewWithText("Fix this.");
    expect(await settledValue(pending)).toBe("pending");
    deliver({ ...HANDSHAKE, shellId: "shell-2" }, parent);
    expect(await pending).toEqual({
      isOpened: false,
      failure: { message: CHANGE_NOT_ANSWERED_SENTENCE, details: null, isReportable: false },
    });
  });

  it("hands a late answer to a request already settled as not answered to its late-answer callback", async () => {
    const parent = shellFramed();
    const late: StartNewOutcome[] = [];
    const pending = (connection as ShellConnection).startNewWithText("Fix this.", {
      showsOutcome: true,
      onLateAnswer: (outcome) => late.push(outcome),
    });
    const requestId = lastRequestId(parent);
    deliver({ ...HANDSHAKE, shellId: "shell-2" }, parent);
    expect((await pending).isOpened).toBe(false);
    expect(late).toEqual([]);

    deliver({ type: SHELL_START_NEW_WITH_TEXT_RESULT, requestId, isOpened: true }, parent);
    deliver({ type: SHELL_START_NEW_WITH_TEXT_RESULT, requestId, isOpened: true }, parent);
    expect(late).toEqual([{ isOpened: true }]);
  });

  it("settles a pending request as not answered once the page disconnects", async () => {
    shellFramed();
    const live = connection as ShellConnection;
    const pending = live.startNewWithText("Fix this.");
    expect(await settledValue(pending)).toBe("pending");
    live.disconnect();
    expect(await pending).toEqual({
      isOpened: false,
      failure: { message: CHANGE_NOT_ANSWERED_SENTENCE, details: null, isReportable: false },
    });
  });

  it("settles at once as not answered, asking nothing of the shell, once the page has disconnected", async () => {
    const parent = shellFramed();
    const live = connection as ShellConnection;
    live.disconnect();
    const sentBefore = parent.postMessage.mock.calls.length;
    expect(await settledValue(live.startNewWithText("Fix this."))).toEqual({
      isOpened: false,
      failure: { message: CHANGE_NOT_ANSWERED_SENTENCE, details: null, isReportable: false },
    });
    expect(parent.postMessage.mock.calls.length).toBe(sentBefore);
  });

  it("settles at once as not answered on a page no shell frames", async () => {
    connection = connectToShell({});
    expect(await connection.startNewWithText("Fix this.")).toEqual({
      isOpened: false,
      failure: { message: CHANGE_NOT_ANSWERED_SENTENCE, details: null, isReportable: false },
    });
  });

  it("is what askAgentToFix answers", async () => {
    const parent = shellFramed();
    const asked = askAgentToFix(connection as ShellConnection, {
      whatFailed: "Docs couldn't open that",
      details: null,
    });
    deliver({ type: SHELL_START_NEW_WITH_TEXT_RESULT, requestId: lastRequestId(parent), isOpened: true }, parent);
    expect(await asked).toEqual({ isOpened: true });
  });
});

describe("withAskFailure", () => {
  it("adds a refused ask's failure under the page's details, labelled, and leaves the page's failure alone without one", () => {
    const page = { message: "The terminal couldn't start.", details: null };
    expect(withAskFailure(page, null)).toEqual(page);
    expect(withAskFailure(page, { message: "No app in this workspace can start a chat.", details: null })).toEqual({
      message: "The terminal couldn't start.",
      details: "Asking an agent to fix this failed too:\nNo app in this workspace can start a chat.",
    });
  });
});

describe("the failure a bug report carries", () => {
  it("reads a shell:report-bug payload's failure, and none from a payload not in that shape", () => {
    expect(reportedFailureOf({ message: "Docs couldn't save.", details: "HTTP 500" })).toEqual({
      message: "Docs couldn't save.",
      details: "HTTP 500",
    });
    expect(reportedFailureOf({ message: "Docs couldn't save." })).toEqual({
      message: "Docs couldn't save.",
      details: null,
    });
    expect(reportedFailureOf({})).toBeNull();
    expect(reportedFailureOf({ message: " ", details: "HTTP 500" })).toBeNull();
    expect(reportedFailureOf({ message: "Docs couldn't save.", details: { status: 500 } })).toBeNull();
  });

  it("is the sentence the user saw, then everything under it", () => {
    expect(bugReportText({ message: "Docs couldn't save.", details: "HTTP 500\nboom" })).toBe(
      "Docs couldn't save.\n\nHTTP 500\nboom",
    );
    expect(bugReportText({ message: "Docs couldn't save.", details: null })).toBe("Docs couldn't save.");
  });
});
