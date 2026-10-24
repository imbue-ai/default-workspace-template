// @vitest-environment jsdom
//
// The widget page's entry, loaded as the browser loads it: in a frame whose parent is a spy, over a stubbed collector,
// with fakes for the browser parts jsdom lacks (EventSource, ResizeObserver) and for the look's broadcast channel.

import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  SHELL_CAPABILITIES,
  SHELL_HANDSHAKE,
  SHELL_WIDGET_HIDDEN,
  SHELL_WIDGET_MODE,
  SHELL_WIDGET_SIZE,
} from "@imbue/workspace-ui/src/app_contract";

import type { LimitReading } from "./models/api";
import { DEFAULT_WIDGET_LOOK, WIDGET_LOOK_CHANNEL } from "./models/widgetLook";

const READING: LimitReading = {
  source: "claude",
  window: "five_hour",
  label: "5h",
  used_percentage: 61,
  resets_at: "2999-01-01T00:00:00Z",
  window_seconds: 18_000,
  observed_at: new Date().toISOString(),
  first_used_percentage: null,
  first_observed_at: null,
};

class FakeStream {
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  addEventListener(): void {}
  close(): void {}
}

class FakeChannel {
  static opened: FakeChannel[] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;
  constructor(readonly name: string) {
    FakeChannel.opened.push(this);
  }
  postMessage(): void {}
  close(): void {}
}

class FakeResizeObserver {
  static made: FakeResizeObserver[] = [];
  readonly observed: Element[] = [];
  constructor(readonly callback: (entries: { target: Element }[]) => void) {
    FakeResizeObserver.made.push(this);
  }
  observe(element: Element): void {
    this.observed.push(element);
  }
  disconnect(): void {}
}

/** A collector with ``limits`` and nothing else, and ``settings`` as the stored look. */
function stubCollector(limits: LimitReading[], settings: Record<string, string> = {}): void {
  vi.stubGlobal("fetch", async (url: string) => {
    const answer = url.includes("/limits")
      ? { limits }
      : url.includes("/totals")
        ? { rows: [] }
        : url.includes("/settings")
          ? settings
          : { status: "ok", catching_up: false };
    return new Response(JSON.stringify(answer), { status: 200 });
  });
}

let parent: { postMessage: ReturnType<typeof vi.fn> };
let added: [string, EventListenerOrEventListenerObject, boolean | AddEventListenerOptions | undefined][];

let onLoad: EventListener | null = null;

/** Load the widget page as the browser does: its entry, imported once, starts on the window's load. Called here
 *  rather than on a dispatched load, so each test starts it once and no test's load starts it again. */
async function load(): Promise<void> {
  if (onLoad === null) {
    await import("./widget");
    const entry = added.find(([type]) => type === "load");
    if (entry === undefined) throw new Error("the widget page waits for no load");
    onLoad = entry[1] as EventListener;
  }
  onLoad(new Event("load"));
}

function sent(type: string): unknown[] {
  return parent.postMessage.mock.calls.map(([message]) => message).filter((message) => message.type === type);
}

function fromShell(data: Record<string, unknown>): void {
  window.dispatchEvent(new MessageEvent("message", { data, source: parent as unknown as Window }));
}

/** The shell's handshake to a widget frame, then the mode it draws in. */
function frameAsWidget(mode: string): void {
  fromShell({ type: SHELL_HANDSHAKE, clientId: "client-1", app: "usage", path: "/widget", surface: "widget" });
  fromShell({ type: SHELL_WIDGET_MODE, mode });
}

function widget(): HTMLElement | null {
  return document.getElementById("usage-widget");
}

beforeEach(() => {
  document.body.innerHTML = '<div id="app"></div>';
  parent = { postMessage: vi.fn() };
  Object.defineProperty(window, "parent", { value: parent, configurable: true });
  FakeChannel.opened = [];
  FakeResizeObserver.made = [];
  vi.stubGlobal("EventSource", FakeStream);
  vi.stubGlobal("BroadcastChannel", FakeChannel);
  vi.stubGlobal("ResizeObserver", FakeResizeObserver);
  added = [];
  const original = window.addEventListener.bind(window);
  vi.spyOn(window, "addEventListener").mockImplementation((type, listener, options) => {
    added.push([type, listener, options]);
    original(type, listener, options);
  });
});

afterEach(() => {
  const root = document.getElementById("app");
  if (root !== null) m.mount(root, null);
  vi.restoreAllMocks();
  // Each load connects anew: the last one's listeners would otherwise answer the next test's shell too.
  for (const [type, listener, options] of added) window.removeEventListener(type, listener, options);
  vi.unstubAllGlobals();
  Object.defineProperty(window, "parent", { value: window, configurable: true });
  window.history.replaceState(null, "", "/");
  document.body.innerHTML = "";
});

describe("the widget page", () => {
  it("draws expanded and tells the shell nothing until a shell frames it as a widget, then follows its mode", async () => {
    stubCollector([READING]);
    await load();
    await vi.waitFor(() => expect(widget()?.textContent).toContain("Claude"));

    expect(widget()?.getAttribute("data-widget-mode")).toBe("expanded");
    expect(parent.postMessage.mock.calls.map(([message]) => message.type)).toEqual([SHELL_CAPABILITIES]);

    frameAsWidget("collapsed");
    await vi.waitFor(() => expect(widget()?.getAttribute("data-widget-mode")).toBe("collapsed"));
    expect(sent(SHELL_WIDGET_HIDDEN)).toEqual([{ type: SHELL_WIDGET_HIDDEN, isHidden: false }]);
    expect(sent(SHELL_WIDGET_SIZE).length).toBeGreaterThan(0);

    fromShell({ type: SHELL_WIDGET_MODE, mode: "tray" });
    await vi.waitFor(() => expect(widget()?.getAttribute("data-widget-mode")).toBe("tray"));
  });

  it("says it has nothing to show while no usage exists, once per change and again after every handshake", async () => {
    stubCollector([]);
    await load();
    frameAsWidget("expanded");
    await vi.waitFor(() => expect(sent(SHELL_WIDGET_HIDDEN)).toEqual([{ type: SHELL_WIDGET_HIDDEN, isHidden: true }]));

    // A redraw with nothing changed says nothing new.
    m.redraw.sync();
    expect(sent(SHELL_WIDGET_HIDDEN)).toHaveLength(1);

    // A new handshake (the shell reloaded) hears it again.
    frameAsWidget("collapsed");
    await vi.waitFor(() => expect(sent(SHELL_WIDGET_HIDDEN)).toHaveLength(2));
    expect(sent(SHELL_WIDGET_HIDDEN)[1]).toEqual({ type: SHELL_WIDGET_HIDDEN, isHidden: true });
  });

  it("reports the drawn widget's size, rounded up, whenever it resizes", async () => {
    stubCollector([READING]);
    await load();
    frameAsWidget("expanded");
    await vi.waitFor(() => expect(widget()).not.toBeNull());

    const [observer] = FakeResizeObserver.made;
    expect(observer.observed).toEqual([widget()]);
    const drawn = widget() as HTMLElement;
    drawn.getBoundingClientRect = () => ({ width: 120.2, height: 40.5 }) as DOMRect;
    observer.callback([{ target: drawn }]);

    expect(sent(SHELL_WIDGET_SIZE).slice(-1)[0]).toEqual({ type: SHELL_WIDGET_SIZE, width: 121, height: 41 });
  });

  it("draws the stored look, takes a pick heard on the look's channel at once, and keeps its URL's overrides", async () => {
    window.history.replaceState(null, "", "/widget?size=small");
    stubCollector([READING], { widget_variant: "minimal", widget_size: "large" });
    await load();
    await vi.waitFor(() => expect(widget()?.getAttribute("data-widget-variant")).toBe("minimal"));
    expect(widget()?.getAttribute("data-widget-size")).toBe("small");

    const [channel] = FakeChannel.opened;
    expect(channel.name).toBe(WIDGET_LOOK_CHANNEL);
    channel.onmessage?.(new MessageEvent("message", { data: { ...DEFAULT_WIDGET_LOOK, variant: "bars" } }));
    await vi.waitFor(() => expect(widget()?.getAttribute("data-widget-variant")).toBe("bars"));
    expect(widget()?.getAttribute("data-widget-size")).toBe("small");

    // Anything that is not a whole look is ignored.
    channel.onmessage?.(new MessageEvent("message", { data: { variant: "rings" } }));
    m.redraw.sync();
    expect(widget()?.getAttribute("data-widget-variant")).toBe("bars");
  });

  it("does nothing on a page with no root to draw in", async () => {
    document.body.innerHTML = "";
    stubCollector([READING]);
    await load();
    expect(parent.postMessage).not.toHaveBeenCalled();
    expect(FakeChannel.opened).toEqual([]);
  });
});
