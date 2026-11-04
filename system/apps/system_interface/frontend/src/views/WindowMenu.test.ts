import { describe, expect, it, vi } from "vitest";
import type { EntryLook } from "../reducers/desktopState";
import { appRecord } from "../testing/records";
import type { ActionRow, MenuRow } from "@imbue/workspace-ui/src/components/menu";
import { enableThemingPrompt, taskbarEntryMenuRows, windowMenuRows } from "./WindowMenu";

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
const fullscreen = { isFullscreen: false, toggleFullscreen: vi.fn(), isImmersive: false, immersive: null };

describe("windowMenuRows", () => {
  it("offers Move and resize, Share, Quit, and Close for an ordinary running app", () => {
    const app = appRecord("docs");
    const quit = vi.fn();
    const rows = windowMenuRows(app, {
      size,
      onSized: vi.fn(),
      fullscreen,
      share: vi.fn(),
      quit,
      popOut: null,
      close: vi.fn(),
      theme: null,
    });
    expect(keysOf(rows)).toEqual(["size", "fullscreen", "|", "share", "quit", "|", "close"]);
    const quitRow = rowOf(rows, "quit");
    expect(quitRow.label).toBe("Quit Docs");
    quitRow.onSelect();
    expect(quit).toHaveBeenCalledTimes(1);
  });

  it("offers Quit for a stopped app too: it still closes the app's windows", () => {
    const stopped = appRecord("docs", { is_running: false });
    expect(
      keysOf(
        windowMenuRows(stopped, {
          size,
          onSized: vi.fn(),
          fullscreen,
          share: null,
          quit: vi.fn(),
          popOut: null,
          close: vi.fn(),
          theme: null,
        }),
      ),
    ).toEqual(["size", "fullscreen", "|", "quit", "|", "close"]);
  });

  it("offers neither Share nor Quit where the caller gives none (a critical app, one the workspace cannot stop)", () => {
    expect(
      keysOf(
        windowMenuRows(appRecord("docs"), {
          size,
          onSized: vi.fn(),
          fullscreen,
          share: null,
          quit: null,
          popOut: null,
          close: vi.fn(),
          theme: null,
        }),
      ),
    ).toEqual(["size", "fullscreen", "|", "close"]);
  });

  it("drops Move and resize where the caller gives none (a window since closed)", () => {
    expect(
      keysOf(
        windowMenuRows(appRecord("docs"), {
          size: null,
          onSized: vi.fn(),
          fullscreen: null,
          share: null,
          quit: null,
          popOut: null,
          close: vi.fn(),
          theme: null,
        }),
      ),
    ).toEqual(["close"]);
  });

  it("offers Close whatever the caller does with it (a pinned window's minimizes)", () => {
    const close = vi.fn();
    const rows = windowMenuRows(appRecord("docs"), {
      size,
      onSized: vi.fn(),
      fullscreen,
      share: null,
      quit: null,
      popOut: null,
      close,
      theme: null,
    });
    expect(keysOf(rows)).toEqual(["size", "fullscreen", "|", "close"]);
    rowOf(rows, "close").onSelect();
    expect(close).toHaveBeenCalledTimes(1);
  });

  it("offers only Move and resize and Close for a window of an app the shell no longer lists", () => {
    expect(
      keysOf(
        windowMenuRows(undefined, {
          size,
          onSized: vi.fn(),
          fullscreen,
          share: vi.fn(),
          quit: vi.fn(),
          popOut: null,
          close: vi.fn(),
          theme: null,
        }),
      ),
    ).toEqual(["size", "fullscreen", "|", "close"]);
  });
});

describe("taskbarEntryMenuRows", () => {
  const actions = {
    isDetached: false,
    show: vi.fn(),
    showGhost: vi.fn(),
    bringBack: vi.fn(),
    restore: vi.fn(),
    minimize: vi.fn(),
    maximize: vi.fn(),
    unmaximize: vi.fn(),
    close: vi.fn(),
    presentation: null,
    desktopShortcut: null,
  };

  it("offers a pinned entry the float verb and, when the pin declares the avatar, its chooser", () => {
    const setMode = vi.fn();
    const changeAvatar = vi.fn();
    const pinnedEntries = (look: EntryLook, options: { isMinimized?: boolean } = {}) =>
      taskbarEntryMenuRows({
        ...actions,
        close: vi.fn(),
        isMinimized: options.isMinimized ?? false,
        isMaximized: false,
        presentation: { look, setMode, changeAvatar },
      });
    const inBar = pinnedEntries(
      { mode: "bar", style: "avatar", declaredStyle: "avatar", position: null, isWindowShown: true },
      { isMinimized: true },
    );
    expect(keysOf(inBar)).toEqual(["restore", "maximize", "|", "float", "change-avatar", "|", "close"]);
    expect(rowOf(inBar, "change-avatar").label).toBe("Change avatar...");
    rowOf(inBar, "change-avatar").onSelect();
    expect(changeAvatar).toHaveBeenCalledTimes(1);
    rowOf(inBar, "float").onSelect();
    expect(setMode).toHaveBeenCalledWith("floating");
    const floating = pinnedEntries({
      mode: "floating",
      style: "plain",
      declaredStyle: "avatar",
      position: null,
      isWindowShown: true,
    });
    expect(keysOf(floating)).toEqual(["minimize", "maximize", "|", "move-to-taskbar", "change-avatar", "|", "close"]);
    rowOf(floating, "move-to-taskbar").onSelect();
    expect(setMode).toHaveBeenCalledWith("bar");
    // A pin declaring no avatar offers no chooser row.
    const plainPin = pinnedEntries({
      mode: "bar",
      style: "plain",
      declaredStyle: "plain",
      position: null,
      isWindowShown: true,
    });
    expect(keysOf(plainPin)).toEqual(["minimize", "maximize", "|", "float", "|", "close"]);
  });

  it("offers Restore or Minimize, Maximize or Restore size, and Close", () => {
    expect(keysOf(taskbarEntryMenuRows({ ...actions, isMinimized: true, isMaximized: false }))).toEqual([
      "restore",
      "maximize",
      "|",
      "close",
    ]);
    expect(keysOf(taskbarEntryMenuRows({ ...actions, isMinimized: false, isMaximized: true }))).toEqual([
      "minimize",
      "unmaximize",
      "|",
      "close",
    ]);
  });

  it("offers the app's desktop row, when there is one, between the other verbs and Close", () => {
    const onSelect = vi.fn();
    const rows = taskbarEntryMenuRows({
      ...actions,
      isMinimized: false,
      isMaximized: false,
      desktopShortcut: { kind: "action", key: "add-to-desktop", label: "Add to desktop", onSelect },
    });
    expect(keysOf(rows)).toEqual(["minimize", "maximize", "|", "add-to-desktop", "|", "close"]);
    rowOf(rows, "add-to-desktop").onSelect();
    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  it("offers Close for a pinned window's entry too (the caller minimizes it)", () => {
    expect(
      keysOf(taskbarEntryMenuRows({ ...actions, close: vi.fn(), isMinimized: true, isMaximized: false })),
    ).toEqual(["restore", "maximize", "|", "close"]);
  });

  it("offers a pulled-out window's entry Show, Hide placeholder, and Bring back in place of the arrangement verbs", () => {
    const show = vi.fn();
    const minimize = vi.fn();
    const bringBack = vi.fn();
    const rows = taskbarEntryMenuRows({
      ...actions,
      isDetached: true,
      show,
      minimize,
      bringBack,
      isMinimized: false,
      isMaximized: false,
    });
    expect(keysOf(rows)).toEqual(["show", "hide-ghost", "bring-back", "|", "close"]);
    rowOf(rows, "show").onSelect();
    rowOf(rows, "hide-ghost").onSelect();
    rowOf(rows, "bring-back").onSelect();
    expect(show).toHaveBeenCalledTimes(1);
    expect(minimize).toHaveBeenCalledTimes(1);
    expect(bringBack).toHaveBeenCalledTimes(1);
    expect(rowOf(rows, "hide-ghost").label).toBe("Hide placeholder");
    expect(rowOf(rows, "bring-back").label).toBe("Bring back to desktop");
  });

  it("offers Show placeholder instead once a pulled-out window's ghost is hidden", () => {
    const showGhost = vi.fn();
    const rows = taskbarEntryMenuRows({
      ...actions,
      isDetached: true,
      showGhost,
      isMinimized: true,
      isMaximized: false,
    });
    expect(keysOf(rows)).toEqual(["show", "show-ghost", "bring-back", "|", "close"]);
    rowOf(rows, "show-ghost").onSelect();
    expect(showGhost).toHaveBeenCalledTimes(1);
    expect(rowOf(rows, "show-ghost").label).toBe("Show placeholder");
  });
});

describe("the pull-out row", () => {
  it("offers Open in its own window ahead of Share where the chrome can pull a window out, and nothing otherwise", () => {
    const popOut = vi.fn();
    const rows = windowMenuRows(appRecord("docs"), {
      size,
      onSized: vi.fn(),
      fullscreen,
      share: vi.fn(),
      quit: vi.fn(),
      popOut,
      close: vi.fn(),
      theme: null,
    });
    expect(keysOf(rows)).toEqual(["size", "fullscreen", "|", "pop-out", "share", "quit", "|", "close"]);
    expect(rowOf(rows, "pop-out").label).toBe("Open in its own window");
    rowOf(rows, "pop-out").onSelect();
    expect(popOut).toHaveBeenCalledTimes(1);
    const without = windowMenuRows(appRecord("docs"), {
      size,
      onSized: vi.fn(),
      fullscreen,
      share: null,
      quit: null,
      popOut: null,
      close: vi.fn(),
      theme: null,
    });
    expect(keysOf(without)).toEqual(["size", "fullscreen", "|", "close"]);
  });
});

describe("the fullscreen rows", () => {
  it("offers Fullscreen after Move and resize, reading Exit fullscreen while the window fills the viewport", () => {
    const toggleFullscreen = vi.fn();
    const rows = windowMenuRows(appRecord("docs"), {
      size,
      onSized: vi.fn(),
      fullscreen: { isFullscreen: false, toggleFullscreen, isImmersive: false, immersive: null },
      share: null,
      quit: null,
      popOut: null,
      close: vi.fn(),
      theme: null,
    });
    expect(keysOf(rows)).toEqual(["size", "fullscreen", "|", "close"]);
    expect(rowOf(rows, "fullscreen").label).toBe("Fullscreen");
    rowOf(rows, "fullscreen").onSelect();
    expect(toggleFullscreen).toHaveBeenCalledTimes(1);
    const filled = windowMenuRows(appRecord("docs"), {
      size,
      onSized: vi.fn(),
      fullscreen: { isFullscreen: true, toggleFullscreen, isImmersive: false, immersive: null },
      share: null,
      quit: null,
      popOut: null,
      close: vi.fn(),
      theme: null,
    });
    expect(rowOf(filled, "fullscreen").label).toBe("Exit fullscreen");
  });

  it("offers Immersive only where the chrome can do it, reading Leave immersive while it is on", () => {
    const immersive = vi.fn();
    const rows = windowMenuRows(appRecord("docs"), {
      size,
      onSized: vi.fn(),
      fullscreen: { isFullscreen: false, toggleFullscreen: vi.fn(), isImmersive: false, immersive },
      share: null,
      quit: null,
      popOut: null,
      close: vi.fn(),
      theme: null,
    });
    expect(keysOf(rows)).toEqual(["size", "fullscreen", "immersive", "|", "close"]);
    expect(rowOf(rows, "immersive").label).toBe("Immersive");
    rowOf(rows, "immersive").onSelect();
    expect(immersive).toHaveBeenCalledTimes(1);
    const on = windowMenuRows(appRecord("docs"), {
      size,
      onSized: vi.fn(),
      fullscreen: { isFullscreen: false, toggleFullscreen: vi.fn(), isImmersive: true, immersive },
      share: null,
      quit: null,
      popOut: null,
      close: vi.fn(),
      theme: null,
    });
    expect(rowOf(on, "immersive").label).toBe("Leave immersive");
  });
});

describe("the window menu's Theming submenu", () => {
  function themingRows(theme: Parameters<typeof windowMenuRows>[1]["theme"]) {
    const rows = windowMenuRows(appRecord("notes"), {
      size,
      onSized: vi.fn(),
      fullscreen: null,
      share: null,
      quit: null,
      popOut: null,
      close: vi.fn(),
      theme,
    });
    const submenu = rows.find((row) => row.kind === "submenu" && row.key === "theme");
    if (submenu?.kind !== "submenu" || submenu.rows === undefined) throw new Error("no Theming submenu");
    return { submenu, children: submenu.rows() };
  }

  it("turns a themeable app's theming on and off, with the current one checked", () => {
    const setOn = vi.fn();
    const { submenu, children } = themingRows({ isThemeable: true, isOn: true, setOn, enable: vi.fn() });
    expect(submenu.label).toBe("Theming");
    expect(submenu.value).toBe("On");
    expect(children.map((row) => (row.kind === "divider" ? "|" : "key" in row ? row.key : ""))).toEqual([
      "theme-on",
      "theme-off",
    ]);
    const [on, off] = children;
    if (on.kind !== "action" || off.kind !== "action") throw new Error("no On and Off rows");
    expect(on.trailing).not.toBeUndefined();
    expect(off.trailing).toBeUndefined();
    off.onSelect();
    expect(setOn).toHaveBeenCalledWith(false);
  });

  it("greys On and Off for an app that is not themeable yet, and offers Enable theming", () => {
    const enable = vi.fn();
    const { submenu, children } = themingRows({ isThemeable: false, isOn: false, setOn: vi.fn(), enable });
    expect(submenu.value).toBe("Off");
    expect(children.map((row) => (row.kind === "divider" ? "|" : "key" in row ? row.key : ""))).toEqual([
      "theme-on",
      "theme-off",
      "|",
      "theme-enable",
    ]);
    expect(children.slice(0, 2).every((row) => row.kind === "action" && row.isDisabled === true)).toBe(true);
    const enableRow = children[3];
    if (enableRow.kind !== "action") throw new Error("no Enable theming row");
    enableRow.onSelect();
    expect(enable).toHaveBeenCalled();
  });

  it("drafts a request to make the app themeable by the workspace's guide", () => {
    expect(enableThemingPrompt("Notes")).toContain(
      "Make the Notes app themeable, following docs/system/themeable-apps.md",
    );
  });
});
