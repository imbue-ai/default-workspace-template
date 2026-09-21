import { describe, expect, it, vi } from "vitest";
import type { EntryLook } from "../reducers/desktopState";
import { appRecord } from "../testing/records";
import { MENU_DIVIDER } from "./Menu";
import type { MenuEntry, MenuItem } from "./Menu";
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
    presentation: null,
  };

  function rowOf(entries: MenuEntry[], key: string): MenuItem {
    const row = entries.find((entry) => entry !== MENU_DIVIDER && entry.key === key);
    if (row === undefined || row === MENU_DIVIDER) throw new Error(`no ${key} row`);
    return row;
  }

  it("offers a pinned entry the float and style verbs in place of Close", () => {
    const setMode = vi.fn();
    const setStyle = vi.fn();
    const changeAvatar = vi.fn();
    const pinnedEntries = (look: EntryLook, options: { isMinimized?: boolean; isCompact?: boolean } = {}) =>
      taskbarEntryMenuEntries(
        {
          ...actions,
          close: null,
          isMinimized: options.isMinimized ?? false,
          isMaximized: false,
          presentation: { look, setMode, setStyle, changeAvatar },
        },
        options.isCompact ?? false,
      );
    const inBar = pinnedEntries(
      { mode: "bar", style: "avatar", declaredStyle: "avatar", position: null },
      { isMinimized: true },
    );
    expect(keysOf(inBar)).toEqual(["restore", "maximize", "|", "float", "style-plain", "change-avatar"]);
    expect(rowOf(inBar, "style-plain").label).toBe("Show as plain entry");
    expect(rowOf(inBar, "change-avatar").label).toBe("Change avatar...");
    rowOf(inBar, "change-avatar").run();
    expect(changeAvatar).toHaveBeenCalledTimes(1);
    rowOf(inBar, "float").run();
    expect(setMode).toHaveBeenCalledWith("floating");
    rowOf(inBar, "style-plain").run();
    expect(setStyle).toHaveBeenCalledWith("plain");
    const floating = pinnedEntries({ mode: "floating", style: "plain", declaredStyle: "avatar", position: null });
    expect(keysOf(floating)).toEqual(["minimize", "maximize", "|", "move-to-taskbar", "style-avatar"]);
    expect(rowOf(floating, "style-avatar").label).toBe("Show as avatar");
    // A pin declaring no style offers no style row; compact mode offers no float row either.
    const plainPin = pinnedEntries({ mode: "bar", style: "plain", declaredStyle: "plain", position: null });
    expect(keysOf(plainPin)).toEqual(["minimize", "maximize", "|", "float"]);
    const compact = pinnedEntries(
      { mode: "floating", style: "plain", declaredStyle: "plain", position: null },
      { isCompact: true },
    );
    expect(keysOf(compact)).toEqual(["minimize"]);
  });

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
