// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mithril captures requestAnimationFrame at import time, and jsdom has none;
// shim it before the engine module (which imports mithril) is evaluated.
vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
  globalThis.cancelAnimationFrame ??= ((id: number): void =>
    clearTimeout(id)) as typeof globalThis.cancelAnimationFrame;
});

import m from "mithril";
import { createTranscriptScrollEngine } from "./transcript-scroll-engine";
import type { TranscriptScrollDataSource, TranscriptScrollEngine } from "./transcript-scroll-engine";
import type { RowDescriptor } from "./conversation-rows";

const CLIENT_HEIGHT_PX = 655;
const SCROLL_HEIGHT_PX = 39280;
const BOTTOM_PX = SCROLL_HEIGHT_PX - CLIENT_HEIGHT_PX;

// The scroll element's layout, which a test changes to model the browser
// re-laying it out (the composer growing under it, content shrinking mid-patch).
interface ScrollLayout {
  scrollHeightPx: number;
  clientHeightPx: number;
}

// A scroll element whose scrollTop is a real stored property (jsdom computes no
// layout, so its native scrollTop/scrollHeight/clientHeight are inert) -- the
// engine's FOLLOW pin writes scrollTop and reads it back, which this models,
// clamped to the maximum the way the browser clamps it.
function makeScrollElement(
  layout: ScrollLayout = { scrollHeightPx: SCROLL_HEIGHT_PX, clientHeightPx: CLIENT_HEIGHT_PX },
): HTMLElement {
  const element = document.createElement("div");
  let scrollTopPx = 0;
  Object.defineProperty(element, "scrollTop", {
    configurable: true,
    get: () => scrollTopPx,
    set: (value: number) => {
      scrollTopPx = Math.max(0, Math.min(value, layout.scrollHeightPx - layout.clientHeightPx));
    },
  });
  Object.defineProperty(element, "scrollHeight", { configurable: true, get: () => layout.scrollHeightPx });
  Object.defineProperty(element, "clientHeight", { configurable: true, get: () => layout.clientHeightPx });
  return element;
}

// No loaded rows by default: the FOLLOW pin positions from the element's own
// scrollHeight/clientHeight, so an empty window exercises it, and a null total
// keeps the fill planner idle -- these tests are about positioning alone. Rows
// give the engine something to anchor a reader who scrolled up.
function makeDataSource(rowCount = 0): TranscriptScrollDataSource {
  const eventIds = Array.from({ length: rowCount }, (_, index) => `event-${index}`);
  const rows: RowDescriptor[] = eventIds.map((eventId) => ({
    key: eventId,
    estimate: SCROLL_HEIGHT_PX / rowCount,
    anchorEventId: eventId,
    render: () => null,
    takesLiveTail: false,
    chipCallIds: new Set(),
    speakerTurn: null,
  }));
  return {
    getRows: () => rows,
    getWindowEventIds: () => eventIds,
    getFirstOffset: () => 0,
    getTotalEvents: () => null,
    getRenderVersion: () => 1,
    executeFill: async () => undefined,
  };
}

// afterRender defers its work to a microtask; drain the queue so positioning has run.
async function flushMicrotasks(): Promise<void> {
  for (let i = 0; i < 8; i++) {
    await Promise.resolve();
  }
}

// A constructable ResizeObserver stand-in: jsdom has none, and the engine does
// `new ResizeObserver(...)` in attach(), so a plain mock factory is not enough.
// Each instance keeps its callback and what it observes, so a test can fire the
// observer of a given element the way the browser does after a layout.
class StubResizeObserver {
  static instances: StubResizeObserver[] = [];
  readonly observed: Element[] = [];
  constructor(readonly callback: ResizeObserverCallback) {
    StubResizeObserver.instances.push(this);
  }
  observe = vi.fn((target: Element) => {
    this.observed.push(target);
  });
  unobserve = vi.fn();
  disconnect = vi.fn();
}

function fireResizeObserverOf(target: Element): void {
  for (const observer of StubResizeObserver.instances) {
    if (observer.observed.includes(target)) {
      observer.callback([], observer as unknown as ResizeObserver);
    }
  }
}

// Open a chat in FOLLOW on `element`: the engine pins it to the bottom.
async function openFollowingChat(
  dataSource: TranscriptScrollDataSource,
  element: HTMLElement,
): Promise<TranscriptScrollEngine> {
  const engine = createTranscriptScrollEngine({ dataSource });
  engine.setChat("agent-1");
  engine.computeRenderPlan();
  engine.afterRender(element);
  await flushMicrotasks();
  expect(element.scrollTop).toBe(BOTTOM_PX);
  return engine;
}

let originalResizeObserver: typeof globalThis.ResizeObserver;

beforeEach(() => {
  StubResizeObserver.instances = [];
  originalResizeObserver = globalThis.ResizeObserver;
  globalThis.ResizeObserver = StubResizeObserver as unknown as typeof globalThis.ResizeObserver;
});

afterEach(() => {
  globalThis.ResizeObserver = originalResizeObserver;
});

describe("transcript scroll engine repositions after a chat switch", () => {
  let engine: TranscriptScrollEngine | undefined;

  afterEach(() => {
    engine?.detach();
    engine = undefined;
  });

  it("re-pins FOLLOW to the bottom when the frame's scrollTop is reset (hidden then shown on switch)", async () => {
    const element = makeScrollElement();
    engine = await openFollowingChat(makeDataSource(), element);

    // Switching chats hides this frame (display:none), which resets its scroll
    // container to the top, while nothing about the content changes. The engine's
    // per-frame content key is therefore unchanged -- the regression was that its
    // short-circuit skipped repositioning on that unchanged key, stranding the
    // transcript at the top in FOLLOW until a manual scroll. The re-pin must run.
    element.scrollTop = 0;
    engine.computeRenderPlan();
    engine.afterRender(element);
    await flushMicrotasks();

    expect(element.scrollTop).toBe(BOTTOM_PX);
  });
});

describe("transcript scroll engine keeps FOLLOW at the bottom through layout changes", () => {
  let engine: TranscriptScrollEngine | undefined;
  let layout: ScrollLayout;
  let element: HTMLElement;

  beforeEach(() => {
    layout = { scrollHeightPx: SCROLL_HEIGHT_PX, clientHeightPx: CLIENT_HEIGHT_PX };
    element = makeScrollElement(layout);
  });

  afterEach(() => {
    engine?.detach();
    engine = undefined;
  });

  // The redraw's patch lays the content out shorter for a moment (a spacer
  // shrinks before the row replacing it is inserted), or content shrinks before
  // the redraw (an image, a font swap), and the browser clamps scrollTop up to
  // fit; the content is back to full height by the time the engine positions.
  // A glide back from the clamp would paint as a bounce.
  it.each([
    { clampLands: "during the redraw's patch", isDuringPatch: true },
    { clampLands: "before the redraw", isDuringPatch: false },
  ])(
    "returns to the bottom in the same frame after the browser clamps the view up $clampLands",
    async ({ isDuringPatch }) => {
      engine = await openFollowingChat(makeDataSource(), element);
      if (!isDuringPatch) element.scrollTop = BOTTOM_PX - 150;
      engine.computeRenderPlan();
      if (isDuringPatch) element.scrollTop = BOTTOM_PX - 150;
      engine.afterRender(element);
      await flushMicrotasks();

      expect(element.scrollTop).toBe(BOTTOM_PX);
    },
  );

  it("re-pins to the bottom as soon as the viewport gets shorter, before any redraw", async () => {
    engine = await openFollowingChat(makeDataSource(), element);
    // The composer grows a line: the transcript's viewport loses 20px at its
    // bottom while scrollTop stays put. The resize observer runs before paint;
    // the pin must happen there, not a frame later.
    layout.clientHeightPx = CLIENT_HEIGHT_PX - 20;
    fireResizeObserverOf(element);

    expect(element.scrollTop).toBe(SCROLL_HEIGHT_PX - (CLIENT_HEIGHT_PX - 20));
  });

  it("leaves a viewport resize alone while the reader's upward scroll has not been reported yet", async () => {
    engine = await openFollowingChat(makeDataSource(), element);

    // A Page Up moves the view before its scroll event arrives, and a key press
    // does not leave FOLLOW on its own.
    element.dispatchEvent(new KeyboardEvent("keydown", { key: "PageUp" }));
    element.scrollTop = BOTTOM_PX - 100;
    layout.clientHeightPx = CLIENT_HEIGHT_PX - 20;
    fireResizeObserverOf(element);

    expect(element.scrollTop).toBe(BOTTOM_PX - 100);
  });

  it("leaves a viewport resize alone once the reader has scrolled up", async () => {
    engine = await openFollowingChat(makeDataSource(200), element);

    element.dispatchEvent(new WheelEvent("wheel", { deltaY: -100 }));
    element.scrollTop = BOTTOM_PX - 100;
    element.dispatchEvent(new Event("scroll"));
    await flushMicrotasks();

    layout.clientHeightPx = CLIENT_HEIGHT_PX - 20;
    fireResizeObserverOf(element);

    expect(element.scrollTop).toBe(BOTTOM_PX - 100);
  });
});

describe("transcript scroll engine with an overlay over the transcript's end", () => {
  const OVERLAY_PX = 120;
  let engine: TranscriptScrollEngine | undefined;
  let layout: ScrollLayout;
  let element: HTMLElement;
  let overlay: HTMLElement;
  let overlayHeightPx: number;

  // The overlay ends the scroll content, so growing or shrinking it changes the scroll height by as much, in the
  // redraw's patch.
  function redrawWithOverlay(heightPx: number, scrollEngine: TranscriptScrollEngine): void {
    scrollEngine.computeRenderPlan();
    layout.scrollHeightPx += heightPx - overlayHeightPx;
    overlayHeightPx = heightPx;
    // The browser clamps the view when the content under it ends sooner.
    element.scrollTop = Math.min(element.scrollTop, layout.scrollHeightPx - layout.clientHeightPx);
    scrollEngine.afterRender(element);
  }

  async function open(rowCount: number): Promise<TranscriptScrollEngine> {
    const opened = createTranscriptScrollEngine({
      dataSource: makeDataSource(rowCount),
      getEndOverlayEl: () => overlay,
    });
    opened.setChat("agent-1");
    opened.computeRenderPlan();
    opened.afterRender(element);
    await flushMicrotasks();
    expect(element.scrollTop).toBe(BOTTOM_PX);
    return opened;
  }

  /** A reader who scrolls up by ``upPx`` with the wheel, which leaves FOLLOW. */
  async function wheelUp(upPx: number): Promise<void> {
    element.dispatchEvent(new WheelEvent("wheel", { deltaY: -upPx }));
    element.scrollTop = element.scrollTop - upPx;
    element.dispatchEvent(new Event("scroll"));
    await flushMicrotasks();
  }

  beforeEach(() => {
    // A chat opens where its reader left it, which an engine writes down as it goes.
    localStorage.clear();
    layout = { scrollHeightPx: SCROLL_HEIGHT_PX, clientHeightPx: CLIENT_HEIGHT_PX };
    element = makeScrollElement(layout);
    overlay = document.createElement("div");
    overlay.appendChild(document.createElement("button"));
    element.appendChild(overlay);
    overlayHeightPx = 0;
    Object.defineProperty(overlay, "offsetHeight", { configurable: true, get: () => overlayHeightPx });
  });

  afterEach(() => {
    engine?.detach();
    engine = undefined;
  });

  it("moves a following reader to the new end in the same frame as the overlay appears", async () => {
    engine = await open(0);

    redrawWithOverlay(OVERLAY_PX, engine);
    await flushMicrotasks();

    // No glide: the transcript's end would sit under the overlay for its frames.
    expect(element.scrollTop).toBe(BOTTOM_PX + OVERLAY_PX);
  });

  it("glides to the new end, as it does for a reply growing, when the content grows with no overlay change", async () => {
    engine = await open(0);

    engine.computeRenderPlan();
    layout.scrollHeightPx += OVERLAY_PX;
    engine.afterRender(element);
    await flushMicrotasks();

    expect(element.scrollTop).toBeGreaterThan(BOTTOM_PX);
    expect(element.scrollTop).toBeLessThan(BOTTOM_PX + OVERLAY_PX);
  });

  it("keeps a following reader at the end as the overlay goes", async () => {
    engine = await open(0);
    redrawWithOverlay(OVERLAY_PX, engine);
    await flushMicrotasks();

    redrawWithOverlay(0, engine);
    await flushMicrotasks();

    expect(element.scrollTop).toBe(BOTTOM_PX);
  });

  it("moves nothing for a reader who has scrolled up, as the overlay comes and goes", async () => {
    engine = await open(200);
    await wheelUp(1000);
    expect(element.scrollTop).toBe(BOTTOM_PX - 1000);

    redrawWithOverlay(OVERLAY_PX, engine);
    await flushMicrotasks();
    expect(element.scrollTop).toBe(BOTTOM_PX - 1000);

    redrawWithOverlay(0, engine);
    await flushMicrotasks();
    expect(element.scrollTop).toBe(BOTTOM_PX - 1000);
  });

  it("carries a reader who is at the end without following to the new end, and leaves them not following", async () => {
    engine = await open(200);
    // A press on the transcript leaves FOLLOW where the view stands: at the end.
    element.dispatchEvent(new Event("pointerdown"));
    window.dispatchEvent(new Event("pointerup"));
    await flushMicrotasks();

    redrawWithOverlay(OVERLAY_PX, engine);
    await flushMicrotasks();
    expect(element.scrollTop).toBe(BOTTOM_PX + OVERLAY_PX);

    // Content that grows now is not followed.
    engine.computeRenderPlan();
    layout.scrollHeightPx += 300;
    engine.afterRender(element);
    await flushMicrotasks();
    expect(element.scrollTop).toBe(BOTTOM_PX + OVERLAY_PX);
  });

  it("leaves a reader at the end alone while they hold the pointer down on the transcript", async () => {
    engine = await open(200);
    element.dispatchEvent(new Event("pointerdown"));

    redrawWithOverlay(OVERLAY_PX, engine);
    await flushMicrotasks();

    expect(element.scrollTop).toBe(BOTTOM_PX);
  });

  it("takes a press on the overlay for none on the transcript: the reader goes on following", async () => {
    engine = await open(200);
    redrawWithOverlay(OVERLAY_PX, engine);
    await flushMicrotasks();

    overlay.firstElementChild!.dispatchEvent(new Event("pointerdown", { bubbles: true }));
    window.dispatchEvent(new Event("pointerup"));
    await flushMicrotasks();
    redrawWithOverlay(0, engine);
    await flushMicrotasks();
    redrawWithOverlay(OVERLAY_PX, engine);
    await flushMicrotasks();

    expect(element.scrollTop).toBe(BOTTOM_PX + OVERLAY_PX);
    // Still following: content that grows is pinned to, which it is not for a reader who pressed on the transcript.
    engine.computeRenderPlan();
    layout.scrollHeightPx += 5000;
    engine.afterRender(element);
    await flushMicrotasks();
    expect(element.scrollTop).toBe(BOTTOM_PX + OVERLAY_PX + 5000);
  });

  it("redraws when the overlay resizes outside a redraw", async () => {
    engine = await open(0);
    const redraw = vi.spyOn(m, "redraw").mockImplementation(() => undefined);

    fireResizeObserverOf(overlay);

    expect(redraw).toHaveBeenCalled();
    redraw.mockRestore();
  });
});
