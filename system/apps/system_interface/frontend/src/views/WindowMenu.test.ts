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

  it("offers Start for a stopped app, and neither Share nor Stop where the caller gives none", () => {
    const stopped = appRecord("docs", { is_running: false });
    expect(
      keysOf(windowMenuEntries(stopped, { refresh: vi.fn(), share: null, setAppLifecycle: vi.fn(), close: vi.fn() })),
    ).toEqual(["refresh", "start", "|", "close"]);
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

  it("drops the maximize verbs in compact mode, where every window is maximized", () => {
    expect(keysOf(taskbarEntryMenuEntries({ ...actions, isMinimized: false, isMaximized: false }, true))).toEqual([
      "minimize",
      "|",
      "close",
    ]);
  });
});
