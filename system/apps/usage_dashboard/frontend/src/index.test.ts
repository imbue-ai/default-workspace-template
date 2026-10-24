// @vitest-environment jsdom
//
// The Usage page's entry, loaded as the browser loads it: in a frame whose parent is a spy, over a collector that
// cannot be reached and the app's own settings, with fakes for EventSource (which jsdom lacks) and the look's channel.

import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SHELL_FOCUSED, SHELL_LOCATION, SHELL_WIDGET_PLACEMENT } from "@imbue/workspace-ui/src/app_contract";

import { DEFAULT_WIDGET_LOOK, WIDGET_LOOK_CHANNEL } from "./models/widgetLook";

class FakeStream {
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  addEventListener(): void {}
  close(): void {}
}

class FakeChannel {
  static posted: [string, unknown][] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;
  constructor(readonly name: string) {}
  postMessage(message: unknown): void {
    FakeChannel.posted.push([this.name, message]);
  }
  close(): void {}
}

let parent: { postMessage: ReturnType<typeof vi.fn> };
let added: [string, EventListenerOrEventListenerObject, boolean | AddEventListenerOptions | undefined][];
let saved: unknown[];
let onLoad: EventListener | null = null;

/** Load the page as the browser does: its entry, imported once, starts on the window's load. Called here rather than
 *  on a dispatched load, so each test starts it once and no test's load starts it again. */
async function load(): Promise<void> {
  if (onLoad === null) {
    await import("./index");
    const entry = added.find(([type]) => type === "load");
    if (entry === undefined) throw new Error("the page waits for no load");
    onLoad = entry[1] as EventListener;
  }
  onLoad(new Event("load"));
}

function sent(type: string): unknown[] {
  return parent.postMessage.mock.calls.map(([message]) => message).filter((message) => message.type === type);
}

/** The element ``selector`` names in the widget picker at the page's foot, or null. */
function inPicker(selector: string): Element | null {
  return document.querySelector(`footer.widget-style ${selector}`);
}

beforeEach(() => {
  document.body.innerHTML = '<div id="app"></div>';
  parent = { postMessage: vi.fn() };
  Object.defineProperty(window, "parent", { value: parent, configurable: true });
  FakeChannel.posted = [];
  saved = [];
  vi.stubGlobal("EventSource", FakeStream);
  vi.stubGlobal("BroadcastChannel", FakeChannel);
  // The collector is down (every read but the settings answers 503); the settings store what is posted.
  vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
    if (!url.startsWith("/api/settings")) return new Response("{}", { status: 503 });
    if (init?.method === "POST") {
      const body: unknown = JSON.parse(String(init.body));
      saved.push(body);
      return new Response(JSON.stringify(body), { status: 200 });
    }
    return new Response(JSON.stringify({ widget_place: "desktop" }), { status: 200 });
  });
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
  for (const [type, listener, options] of added) window.removeEventListener(type, listener, options);
  vi.unstubAllGlobals();
  Object.defineProperty(window, "parent", { value: window, configurable: true });
  document.body.innerHTML = "";
});

describe("the Usage page", () => {
  it("tells the shell where it is, and that it has focus whenever it takes it", async () => {
    await load();
    expect(sent(SHELL_LOCATION)).toEqual([{ type: SHELL_LOCATION, path: "/", title: "Usage" }]);

    window.dispatchEvent(new Event("focus"));
    expect(sent(SHELL_FOCUSED)).toHaveLength(1);
  });

  it("offers the stored widget look at its foot, and a pick saves it, tells the widget, and places this client's", async () => {
    await load();
    await vi.waitFor(() => expect(inPicker('[data-widget-place="desktop"]')).not.toBeNull());
    expect(inPicker('[data-widget-place="desktop"]')?.getAttribute("aria-checked")).toBe("true");

    inPicker('[data-widget-place="tray"]')?.dispatchEvent(new MouseEvent("click", { bubbles: true }));

    await vi.waitFor(() =>
      expect(sent(SHELL_WIDGET_PLACEMENT)).toEqual([{ type: SHELL_WIDGET_PLACEMENT, placement: "tray" }]),
    );
    expect(saved).toEqual([expect.objectContaining({ widget_place: "tray" })]);
    expect(FakeChannel.posted).toEqual([[WIDGET_LOOK_CHANNEL, { ...DEFAULT_WIDGET_LOOK, place: "tray" }]]);
    await vi.waitFor(() => expect(inPicker('[aria-label="Widget design in the tray"]')).not.toBeNull());
  });
});
