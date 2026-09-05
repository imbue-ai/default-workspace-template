import { describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

import { duplicateLiveKeyPanelIds, liveKeyForPanel } from "./liveSurfaces";

describe("liveKeyForPanel", () => {
  it("files an instance panel under its address", () => {
    expect(liveKeyForPanel({ kind: "instance", address: "app:files", tabId: "tab-0000000000000001" })).toBe(
      "app:files",
    );
  });

  it("gives a launcher no key: it is a question about a pane, not an instance", () => {
    expect(liveKeyForPanel({ kind: "launcher" })).toBeNull();
    expect(liveKeyForPanel(undefined)).toBeNull();
  });
});

describe("duplicateLiveKeyPanelIds", () => {
  it("drops every occurrence of a key after the first, and never dedups launchers", () => {
    expect(
      duplicateLiveKeyPanelIds([
        { panelId: "a", key: "app:files" },
        { panelId: "b", key: null },
        { panelId: "c", key: "app:files" },
        { panelId: "d", key: null },
      ]),
    ).toEqual(["c"]);
  });
});
