/**
 * The verbs a window's menu, and a taskbar entry's context menu, offer: only what the shell
 * itself can do (concepts.md section 2.2). Refresh reloads the page; Share asks the minds chrome
 * to open its share settings for the app (never for a critical app); Stop and Start act on the
 * app's supervised program where the workspace can honestly do so; Close removes the window for
 * everyone, and is absent for a pinned window, which is never closed (pinned-taskbar-entries plan
 * section 4.4). Defined once so both menus render the identical list off the identical rule.
 */

import type { AppRecord, PinStyle } from "../model/records";
import type { EntryLook } from "../reducers/desktopState";
import type { MenuEntry } from "./Menu";
import { MENU_DIVIDER } from "./Menu";

export interface WindowMenuActions {
  readonly refresh: () => void;
  /** Null when there is no share surface for the app (a critical app). */
  readonly share: (() => void) | null;
  /** Null when the workspace cannot stop or start the app. */
  readonly setAppLifecycle: ((action: "stop" | "start") => void) | null;
  /** Null for a pinned window, which is never closed. */
  readonly close: (() => void) | null;
}

/** The window menu's rows, in display order. */
export function windowMenuEntries(app: AppRecord | undefined, actions: WindowMenuActions): MenuEntry[] {
  const entries: MenuEntry[] = [{ key: "refresh", label: "Refresh", iconName: "refresh", run: actions.refresh }];
  if (actions.share !== null && app !== undefined) {
    entries.push({ key: "share", label: `Share ${app.display_name}`, iconName: "user-plus", run: actions.share });
  }
  if (actions.setAppLifecycle !== null && app !== undefined) {
    const action = app.is_running ? "stop" : "start";
    const setAppLifecycle = actions.setAppLifecycle;
    entries.push({
      key: action,
      label: `${action === "stop" ? "Stop" : "Start"} ${app.display_name}`,
      iconName: "power",
      run: () => setAppLifecycle(action),
    });
  }
  if (actions.close !== null) {
    entries.push(MENU_DIVIDER, { key: "close", label: "Close", iconName: "close", run: actions.close });
  }
  return entries;
}

/** The presentation verbs of a pinned entry's menu (pinned-taskbar-entries plan section 4.4). */
export interface EntryPresentationActions {
  readonly look: EntryLook;
  readonly setMode: (mode: "bar" | "floating") => void;
  readonly setStyle: (style: PinStyle) => void;
  /** Open the avatar chooser; offered while the entry shows the avatar. */
  readonly changeAvatar: () => void;
}

export interface TaskbarEntryMenuActions {
  readonly isMinimized: boolean;
  readonly isMaximized: boolean;
  readonly restore: () => void;
  readonly minimize: () => void;
  readonly maximize: () => void;
  readonly unmaximize: () => void;
  /** Null for a pinned window's entry, which offers no Close. */
  readonly close: (() => void) | null;
  /** Null for an ordinary window's entry, which has no presentation to choose. */
  readonly presentation: EntryPresentationActions | null;
}

/** What "Show as <style>" reads for a style the pin declares. */
function styleLabel(style: PinStyle): string {
  switch (style) {
    case "plain":
      return "Show as plain entry";
    case "avatar":
      return "Show as avatar";
  }
}

/** A taskbar entry's context menu: Restore or Minimize, Maximize or Restore size, then for a pinned entry Float
 *  or Move to taskbar (not in compact mode, where every entry is in the bar), the style to show it in, and the
 *  avatar chooser while it shows the avatar, then Close for an ordinary entry. */
export function taskbarEntryMenuEntries(actions: TaskbarEntryMenuActions, isCompact: boolean): MenuEntry[] {
  const entries: MenuEntry[] = [
    actions.isMinimized
      ? { key: "restore", label: "Restore", run: actions.restore }
      : { key: "minimize", label: "Minimize", run: actions.minimize },
  ];
  if (!isCompact) {
    entries.push(
      actions.isMaximized
        ? { key: "unmaximize", label: "Restore size", run: actions.unmaximize }
        : { key: "maximize", label: "Maximize", run: actions.maximize },
    );
  }
  const presentation = actions.presentation;
  if (presentation !== null) {
    const { look, setMode, setStyle, changeAvatar } = presentation;
    const rows: MenuEntry[] = [];
    if (!isCompact) {
      rows.push(
        look.mode === "floating"
          ? { key: "move-to-taskbar", label: "Move to taskbar", run: () => setMode("bar") }
          : { key: "float", label: "Float", run: () => setMode("floating") },
      );
    }
    if (look.declaredStyle !== "plain") {
      const other: PinStyle = look.style === "plain" ? look.declaredStyle : "plain";
      rows.push({ key: `style-${other}`, label: styleLabel(other), run: () => setStyle(other) });
    }
    if (look.style === "avatar") rows.push({ key: "change-avatar", label: "Change avatar...", run: changeAvatar });
    if (rows.length > 0) entries.push(MENU_DIVIDER, ...rows);
  }
  if (actions.close !== null) {
    entries.push(MENU_DIVIDER, { key: "close", label: "Close", iconName: "close", run: actions.close });
  }
  return entries;
}
