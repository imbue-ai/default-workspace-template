import { describe, expect, it, vi } from "vitest";
import type { EntryLook } from "../reducers/desktopState";
import { appRecord } from "../testing/records";
import type { ActionRow, MenuRow } from "@imbue/workspace-ui/src/components/menu";
import { taskbarEntryMenuRows, windowMenuRows } from "./WindowMenu";

function keysOf(rows: MenuRow[]): string[] {
  return rows.map((row) => (row.kind === "divider" ? "|" : (row.key ?? "")));
}

/** The action row `key` names, for a test that means to run it or read its label. */
function rowOf(rows: MenuRow[], key: string): ActionRow {
  const row = rows.find((candidate) => candidate.kind === "action" && candidate.key === key);
  if (row === undefined || row.kind !== "action") throw new Error(`no ${key} row`);
  return row;
}

const size = { setState: vi.fn(), setFrame: vi.fn() };

describe("windowMenuRows", () => {
  it("offers Size, Share, Stop or Start, and Close for an ordinary running app", () => {
    const app = appRecord("docs");
    const setAppLifecycle = vi.fn();
    const rows = windowMenuRows(app, {
      size,
      onSized: vi.fn(),
      share: vi.fn(),
      setAppLifecycle,
      close: vi.fn(),
    });
    expect(keysOf(rows)).toEqual(["size", "|", "share", "stop", "|", "close"]);
    const stop = rowOf(rows, "stop");
    expect(stop.label).toBe("Stop Docs");
    stop.onSelect();
    expect(setAppLifecycle).toHaveBeenCalledWith("stop");
  });

  it("offers Start instead of Stop for a stopped app", () => {
    const stopped = appRecord("docs", { is_running: false });
    expect(
      keysOf(
        windowMenuRows(stopped, { size, onSized: vi.fn(), share: null, setAppLifecycle: vi.fn(), close: vi.fn() }),
      ),
    ).toEqual(["size", "|", "start", "|", "close"]);
  });

  it("offers neither Share nor Stop where the caller gives none (a critical app, one the workspace cannot stop)", () => {
    expect(
      keysOf(
        windowMenuRows(appRecord("docs"), {
          size,
          onSized: vi.fn(),
          share: null,
          setAppLifecycle: null,
          close: vi.fn(),
        }),
      ),
    ).toEqual(["size", "|", "close"]);
  });

  it("drops the size section where the caller gives none (compact, where every window is maximized)", () => {
    expect(
      keysOf(
        windowMenuRows(appRecord("docs"), {
          size: null,
          onSized: vi.fn(),
          share: null,
          setAppLifecycle: null,
          close: vi.fn(),
        }),
      ),
    ).toEqual(["close"]);
  });

  it("offers Close whatever the caller does with it (a pinned window's minimizes)", () => {
    const close = vi.fn();
    const rows = windowMenuRows(appRecord("docs"), {
      size,
      onSized: vi.fn(),
      share: null,
      setAppLifecycle: null,
      close,
    });
    expect(keysOf(rows)).toEqual(["size", "|", "close"]);
    rowOf(rows, "close").onSelect();
    expect(close).toHaveBeenCalledTimes(1);
  });

  it("offers only Size and Close for a window of an app the shell no longer lists", () => {
    expect(
      keysOf(
        windowMenuRows(undefined, { size, onSized: vi.fn(), share: vi.fn(), setAppLifecycle: vi.fn(), close: vi.fn() }),
      ),
    ).toEqual(["size", "|", "close"]);
  });
});

describe("taskbarEntryMenuRows", () => {
  const actions = {
    restore: vi.fn(),
    minimize: vi.fn(),
    maximize: vi.fn(),
    unmaximize: vi.fn(),
    close: vi.fn(),
    presentation: null,
  };

  it("offers a pinned entry the float and style verbs in place of Close", () => {
    const setMode = vi.fn();
    const setStyle = vi.fn();
    const changeAvatar = vi.fn();
    const pinnedEntries = (look: EntryLook, options: { isMinimized?: boolean; isCompact?: boolean } = {}) =>
      taskbarEntryMenuRows(
        {
          ...actions,
          close: vi.fn(),
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
    expect(keysOf(inBar)).toEqual(["restore", "maximize", "|", "float", "style-plain", "change-avatar", "|", "close"]);
    expect(rowOf(inBar, "style-plain").label).toBe("Show as plain entry");
    expect(rowOf(inBar, "change-avatar").label).toBe("Change avatar...");
    rowOf(inBar, "change-avatar").onSelect();
    expect(changeAvatar).toHaveBeenCalledTimes(1);
    rowOf(inBar, "float").onSelect();
    expect(setMode).toHaveBeenCalledWith("floating");
    rowOf(inBar, "style-plain").onSelect();
    expect(setStyle).toHaveBeenCalledWith("plain");
    const floating = pinnedEntries({ mode: "floating", style: "plain", declaredStyle: "avatar", position: null });
    expect(keysOf(floating)).toEqual(["minimize", "maximize", "|", "move-to-taskbar", "style-avatar", "|", "close"]);
    expect(rowOf(floating, "style-avatar").label).toBe("Show as avatar");
    // A pin declaring no style offers no style row; compact mode offers no float row either.
    const plainPin = pinnedEntries({ mode: "bar", style: "plain", declaredStyle: "plain", position: null });
    expect(keysOf(plainPin)).toEqual(["minimize", "maximize", "|", "float", "|", "close"]);
    const compact = pinnedEntries(
      { mode: "floating", style: "plain", declaredStyle: "plain", position: null },
      { isCompact: true },
    );
    expect(keysOf(compact)).toEqual(["minimize", "|", "close"]);
  });

  it("offers Restore or Minimize, Maximize or Restore size, and Close", () => {
    expect(keysOf(taskbarEntryMenuRows({ ...actions, isMinimized: true, isMaximized: false }, false))).toEqual([
      "restore",
      "maximize",
      "|",
      "close",
    ]);
    expect(keysOf(taskbarEntryMenuRows({ ...actions, isMinimized: false, isMaximized: true }, false))).toEqual([
      "minimize",
      "unmaximize",
      "|",
      "close",
    ]);
  });

  it("offers Close for a pinned window's entry too (the caller minimizes it)", () => {
    expect(
      keysOf(taskbarEntryMenuRows({ ...actions, close: vi.fn(), isMinimized: true, isMaximized: false }, false)),
    ).toEqual(["restore", "maximize", "|", "close"]);
  });

  it("drops the maximize verbs in compact mode, where every window is maximized", () => {
    expect(keysOf(taskbarEntryMenuRows({ ...actions, isMinimized: false, isMaximized: false }, true))).toEqual([
      "minimize",
      "|",
      "close",
    ]);
  });
});
