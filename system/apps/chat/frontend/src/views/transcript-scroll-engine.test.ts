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

import { createTranscriptScrollEngine } from "./transcript-scroll-engine";
import type { TranscriptScrollDataSource, TranscriptScrollEngine } from "./transcript-scroll-engine";

const CLIENT_HEIGHT_PX = 655;
const SCROLL_HEIGHT_PX = 39280;
const BOTTOM_PX = SCROLL_HEIGHT_PX - CLIENT_HEIGHT_PX;

// A scroll element whose scrollTop is a real stored property (jsdom computes no
// layout, so its native scrollTop/scrollHeight/clientHeight are inert) -- the
// engine's FOLLOW pin writes scrollTop and reads it back, which this models.
function makeScrollElement(): HTMLElement {
  const element = document.createElement("div");
  let scrollTopPx = 0;
  Object.defineProperty(element, "scrollTop", {
    configurable: true,
    get: () => scrollTopPx,
    set: (value: number) => {
      scrollTopPx = value;
    },
  });
  Object.defineProperty(element, "scrollHeight", { configurable: true, get: () => SCROLL_HEIGHT_PX });
  Object.defineProperty(element, "clientHeight", { configurable: true, get: () => CLIENT_HEIGHT_PX });
  return element;
}

// No loaded rows: the FOLLOW pin positions from the element's own
// scrollHeight/clientHeight, so an empty window exercises it, and a null total
// keeps the fill planner idle -- this test is about positioning alone.
function makeDataSource(): TranscriptScrollDataSource {
  return {
    getRows: () => [],
    getWindowEventIds: () => [],
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

describe("transcript scroll engine repositions after a chat switch", () => {
  let engine: TranscriptScrollEngine;
  let element: HTMLElement;
  let originalResizeObserver: typeof globalThis.ResizeObserver;

  beforeEach(() => {
    originalResizeObserver = globalThis.ResizeObserver;
    globalThis.ResizeObserver = vi.fn(() => ({
      observe: vi.fn(),
      unobserve: vi.fn(),
      disconnect: vi.fn(),
    })) as unknown as typeof globalThis.ResizeObserver;
    element = makeScrollElement();
    engine = createTranscriptScrollEngine({ dataSource: makeDataSource() });
  });

  afterEach(() => {
    engine.detach();
    globalThis.ResizeObserver = originalResizeObserver;
  });

  it("re-pins FOLLOW to the bottom when the frame's scrollTop is reset (hidden then shown on switch)", async () => {
    // Open the chat: FOLLOW pins the viewport to the bottom.
    engine.setChat("agent-1");
    engine.computeRenderPlan();
    engine.afterRender(element);
    await flushMicrotasks();
    expect(element.scrollTop).toBe(BOTTOM_PX);

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
