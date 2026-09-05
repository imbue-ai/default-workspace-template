import { describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

import {
  actionKey,
  equalTabWidth,
  isTitleTruncated,
  mostRecentAddressOfApp,
  primaryActionForApp,
} from "./DockviewWorkspace";
import type { AppRecord } from "../models/Inventory";

describe("equalTabWidth", () => {
  it("shares what is left of a strip once the '+' is accounted for", () => {
    expect(equalTabWidth([{ width: 644, tabCount: 4 }])).toBe(150);
  });

  it("takes the narrowest strip's ideal so no strip has to scroll", () => {
    expect(
      equalTabWidth([
        { width: 644, tabCount: 4 },
        { width: 908, tabCount: 6 },
      ]),
    ).toBe(144);
  });

  it("clamps to the floor and the ceiling", () => {
    expect(equalTabWidth([{ width: 400, tabCount: 12 }])).toBe(140);
    expect(equalTabWidth([{ width: 1200, tabCount: 1 }])).toBe(220);
    expect(equalTabWidth([{ width: 20, tabCount: 1 }])).toBe(140);
  });

  it("ignores strips that hold no tabs, and answers with the ceiling for none at all", () => {
    expect(
      equalTabWidth([
        { width: 644, tabCount: 4 },
        { width: 300, tabCount: 0 },
      ]),
    ).toBe(150);
    expect(equalTabWidth([])).toBe(220);
  });
});

describe("isTitleTruncated", () => {
  it("tolerates a sub-pixel overhang so an exact fit stays crisp", () => {
    expect(isTitleTruncated(80, 140)).toBe(false);
    expect(isTitleTruncated(140.4, 140)).toBe(false);
    expect(isTitleTruncated(260, 140)).toBe(true);
  });
});

describe("mostRecentAddressOfApp", () => {
  const candidates = [
    { address: "app:chat?instance=a", appName: "chat", lastActiveMs: 1_000 },
    { address: "app:chat?instance=b", appName: "chat", lastActiveMs: 5_000 },
    { address: "app:terminal?instance=t", appName: "terminal", lastActiveMs: 9_000 },
  ];

  it("finds nothing to focus in a view with no instance of the app", () => {
    expect(mostRecentAddressOfApp(candidates, "browser", {})).toBeNull();
  });

  it("prefers the tab this client focused most recently, then the app's own recency", () => {
    expect(mostRecentAddressOfApp(candidates, "chat", {})).toBe("app:chat?instance=b");
    expect(mostRecentAddressOfApp(candidates, "chat", { "app:chat?instance=a": 7_000 })).toBe("app:chat?instance=a");
  });

  it("ignores other apps' recency", () => {
    expect(mostRecentAddressOfApp(candidates, "chat", { "app:terminal?instance=t": 99_000 })).toBe(
      "app:chat?instance=b",
    );
  });

  it("takes the first listed when nothing has recency", () => {
    const bare = [
      { address: "app:chat?instance=a", appName: "chat", lastActiveMs: null },
      { address: "app:chat?instance=b", appName: "chat", lastActiveMs: null },
    ];
    expect(mostRecentAddressOfApp(bare, "chat", {})).toBe("app:chat?instance=a");
  });
});

describe("primaryActionForApp", () => {
  function app(overrides: Partial<AppRecord>): AppRecord {
    return {
      name: "chat",
      display_name: "Chat",
      icon: "",
      label: "",
      url: "http://127.0.0.1:8000",
      internal: false,
      program: "",
      critical: true,
      instances_url: "",
      has_instances: true,
      actions: [
        { id: "new", label: "New Chat" },
        { id: "subagent", label: "Open subagent" },
      ],
      default_shortcut: null,
      is_running: true,
      instances: [],
      ...overrides,
    };
  }

  it("takes the declared default shortcut's action, else the first action, else nothing", () => {
    expect(primaryActionForApp(app({ default_shortcut: { action: "subagent", mode: "new" } }))?.id).toBe("subagent");
    expect(primaryActionForApp(app({}))?.id).toBe("new");
    expect(primaryActionForApp(app({ default_shortcut: { action: "gone", mode: "new" } }))?.id).toBe("new");
    expect(primaryActionForApp(app({ actions: [] }))).toBeNull();
  });

  it("keys an in-flight action by app and action", () => {
    expect(actionKey("chat", "new")).toBe("chat:new");
  });
});
