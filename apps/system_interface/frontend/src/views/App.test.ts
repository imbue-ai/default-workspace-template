import { describe, expect, it, vi } from "vitest";

// Mithril captures `requestAnimationFrame` at import time so it can schedule
// redraws. Vitest's default (node) environment has no such global, which
// makes the `m.redraw()` calls inside the model setters throw. Provide a
// polyfill before any import is evaluated (same setup as
// ClaudeLoginModal.test.ts).
vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

import { App } from "./App";

// App's view() ignores its vnode argument; mithril's Component type still
// requires one, so pass a minimal stand-in.
function renderApp(): unknown {
  const component = App();
  return component.view({} as Parameters<typeof component.view>[0]);
}

describe("App", () => {
  it("renders its view", () => {
    expect(() => renderApp()).not.toThrow();
  });
});
