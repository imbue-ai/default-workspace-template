import { describe, expect, it, vi } from "vitest";
import { closeSettings, isSettingsOpen, openSettings } from "./Settings";

// Mithril captures `requestAnimationFrame` at import time to schedule the
// redraws that open/closeSettings trigger; the node test env has no such
// global, so the calls would throw without this polyfill. vi.hoisted runs
// before the imports above, so the polyfill is in place when Mithril loads
// (same dance as PendingMessages.test.ts).
vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

describe("Settings open state", () => {
  it("starts closed and toggles open/closed", () => {
    // Normalize in case a prior test left it open (module state is shared).
    closeSettings();
    expect(isSettingsOpen()).toBe(false);

    openSettings();
    expect(isSettingsOpen()).toBe(true);

    // Re-opening is idempotent, not an error.
    openSettings();
    expect(isSettingsOpen()).toBe(true);

    closeSettings();
    expect(isSettingsOpen()).toBe(false);

    // Re-closing is idempotent too.
    closeSettings();
    expect(isSettingsOpen()).toBe(false);
  });
});
