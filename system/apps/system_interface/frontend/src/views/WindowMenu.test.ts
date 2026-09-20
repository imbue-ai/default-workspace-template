import { describe, expect, it, vi } from "vitest";
import { appRecord } from "../testing/records";
import { MENU_DIVIDER } from "./Menu";
import type { MenuEntry } from "./Menu";
import { taskbarEntryMenuEntries, windowMenuEntries } from "./WindowMenu";

function keysOf(entries: MenuEntry[]): string[] {
  return entries.map((entry) => (entry === MENU_DIVIDER ? "|" : entry.key));
}

describe("windowMenuEntries", () => {
  it("offers Refresh, Share, Stop or Start, and Close for an ordinary running app", () => {
    const app = appRecord("docs");
    const setAppLifecycle = vi.fn();
    const entries = windowMenuEntries(app, {
      refresh: vi.fn(),
      share: vi.fn(),
      setAppLifecycle,
      close: vi.fn(),
    });
    expect(keysOf(entries)).toEqual(["refresh", "share", "stop", "|", "close"]);
    const stop = entries.find((entry) => entry !== MENU_DIVIDER && entry.key === "stop");
    if (stop === undefined || stop === MENU_DIVIDER) throw new Error("no stop row");
    expect(stop.label).toBe("Stop Docs");
    stop.run();
    expect(setAppLifecycle).toHaveBeenCalledWith("stop");
  });

  it("offers Start instead of Stop for a stopped app", () => {
    const stopped = appRecord("docs", { is_running: false });
    expect(
      keysOf(windowMenuEntries(stopped, { refresh: vi.fn(), share: null, setAppLifecycle: vi.fn(), close: vi.fn() })),
    ).toEqual(["refresh", "start", "|", "close"]);
  });

  it("offers neither Share nor Stop where the caller gives none (a critical app, one the workspace cannot stop)", () => {
    expect(
      keysOf(
        windowMenuEntries(appRecord("docs"), { refresh: vi.fn(), share: null, setAppLifecycle: null, close: vi.fn() }),
      ),
    ).toEqual(["refresh", "|", "close"]);
  });

  it("offers no Close for a pinned window", () => {
    expect(
      keysOf(
        windowMenuEntries(appRecord("docs"), { refresh: vi.fn(), share: null, setAppLifecycle: null, close: null }),
      ),
    ).toEqual(["refresh"]);
  });

  it("offers only Refresh and Close for a window of an app the shell no longer lists", () => {
    expect(
      keysOf(
        windowMenuEntries(undefined, { refresh: vi.fn(), share: vi.fn(), setAppLifecycle: vi.fn(), close: vi.fn() }),
      ),
    ).toEqual(["refresh", "|", "close"]);
  });
});

describe("taskbarEntryMenuEntries", () => {
  const actions = {
    restore: vi.fn(),
    minimize: vi.fn(),
    maximize: vi.fn(),
    unmaximize: vi.fn(),
    close: vi.fn(),
  };

  it("offers Restore or Minimize, Maximize or Restore size, and Close", () => {
    expect(keysOf(taskbarEntryMenuEntries({ ...actions, isMinimized: true, isMaximized: false }, false))).toEqual([
      "restore",
      "maximize",
      "|",
      "close",
    ]);
    expect(keysOf(taskbarEntryMenuEntries({ ...actions, isMinimized: false, isMaximized: true }, false))).toEqual([
      "minimize",
      "unmaximize",
      "|",
      "close",
    ]);
  });

  it("offers no Close for a pinned window's entry", () => {
    expect(
      keysOf(taskbarEntryMenuEntries({ ...actions, close: null, isMinimized: true, isMaximized: false }, false)),
    ).toEqual(["restore", "maximize"]);
  });

  it("drops the maximize verbs in compact mode, where every window is maximized", () => {
    expect(keysOf(taskbarEntryMenuEntries({ ...actions, isMinimized: false, isMaximized: false }, true))).toEqual([
      "minimize",
      "|",
      "close",
    ]);
  });
});
