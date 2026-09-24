/**
 * The verbs a window's menu, and a taskbar entry's context menu, offer: only what the shell
 * itself can do (concepts.md section 2.2). Move and resize opens the grid of backdrop zones the
 * maximize control opens on hover, as a submenu; Share asks the minds chrome
 * to open its share settings for the app (never for a critical app); Stop and Start act on the
 * app's supervised program where the workspace can honestly do so; Close removes the window for
 * everyone, and minimizes a pinned window, which is never closed (pinned-taskbar-entries plan
 * section 4.4). Defined once so both menus render the identical list off the identical rule.
 */

import type { MenuRow } from "@imbue/workspace-ui/src/components/menu";
import { windowSizeSubmenuRow } from "./WindowSizeRow";
import type { WindowSizeActions } from "./WindowSizeRow";
import type { AppRecord, EntryMode, PinStyle } from "../model/records";
import type { EntryLook } from "../reducers/desktopState";

export interface WindowMenuActions {
  /** The zone grid's actions, or null in compact mode, where every window renders maximized. */
  readonly size: WindowSizeActions | null;
  /** Leave the menu up after a size is picked, for the caller that wants to keep choosing. */
  readonly onSized: () => void;
  /** Null when there is no share surface for the app (a critical app). */
  readonly share: (() => void) | null;
  /** Null when the workspace cannot stop or start the app. */
  readonly setAppLifecycle: ((action: "stop" | "start") => void) | null;
  /** Close the window for everyone; for a pinned window, which is never closed, this minimizes it instead. */
  readonly close: () => void;
}

/** The window menu's rows, in display order. */
export function windowMenuRows(app: AppRecord | undefined, actions: WindowMenuActions): MenuRow[] {
  const rows: MenuRow[] = [];
  if (actions.size !== null) rows.push(windowSizeSubmenuRow(actions.size, actions.onSized), { kind: "divider" });
  if (actions.share !== null && app !== undefined) {
    rows.push({
      kind: "action",
      key: "share",
      label: `Share ${app.display_name}`,
      icon: "user-plus",
      onSelect: actions.share,
    });
  }
  if (actions.setAppLifecycle !== null && app !== undefined) {
    const action = app.is_running ? "stop" : "start";
    const setAppLifecycle = actions.setAppLifecycle;
    rows.push({
      kind: "action",
      key: action,
      label: `${action === "stop" ? "Stop" : "Start"} ${app.display_name}`,
      icon: "power",
      onSelect: () => setAppLifecycle(action),
    });
  }
  // A rule, not a second one, and none at all above the first row: with neither Share nor Stop
  // between them, the divider under Move and resize is already the one Close needs.
  if (rows.length > 0 && rows[rows.length - 1]?.kind !== "divider") rows.push({ kind: "divider" });
  rows.push({ kind: "action", key: "close", label: "Close", icon: "close", onSelect: actions.close });
  return rows;
}

/** The presentation verbs of a pinned entry's menu (pinned-taskbar-entries plan section 4.4). */
export interface EntryPresentationActions {
  readonly look: EntryLook;
  readonly setMode: (mode: EntryMode) => void;
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
  /** Close the window for everyone; for a pinned window, minimize it instead. */
  readonly close: () => void;
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
 *  avatar chooser while it shows the avatar, then Close. */
export function taskbarEntryMenuRows(actions: TaskbarEntryMenuActions, isCompact: boolean): MenuRow[] {
  const rows: MenuRow[] = [
    actions.isMinimized
      ? { kind: "action", key: "restore", label: "Restore", onSelect: actions.restore }
      : { kind: "action", key: "minimize", label: "Minimize", onSelect: actions.minimize },
  ];
  if (!isCompact) {
    rows.push(
      actions.isMaximized
        ? { kind: "action", key: "unmaximize", label: "Restore size", onSelect: actions.unmaximize }
        : { kind: "action", key: "maximize", label: "Maximize", onSelect: actions.maximize },
    );
  }
  const presentation = actions.presentation;
  if (presentation !== null) {
    const { look, setMode, setStyle, changeAvatar } = presentation;
    const presentationRows: MenuRow[] = [];
    if (!isCompact) {
      presentationRows.push(
        look.mode === "floating"
          ? { kind: "action", key: "move-to-taskbar", label: "Move to taskbar", onSelect: () => setMode("bar") }
          : { kind: "action", key: "float", label: "Float", onSelect: () => setMode("floating") },
      );
    }
    if (look.declaredStyle !== "plain") {
      const other: PinStyle = look.style === "plain" ? look.declaredStyle : "plain";
      presentationRows.push({
        kind: "action",
        key: `style-${other}`,
        label: styleLabel(other),
        onSelect: () => setStyle(other),
      });
    }
    if (look.style === "avatar") {
      presentationRows.push({
        kind: "action",
        key: "change-avatar",
        label: "Change avatar...",
        onSelect: changeAvatar,
      });
    }
    if (presentationRows.length > 0) rows.push({ kind: "divider" }, ...presentationRows);
  }
  rows.push(
    { kind: "divider" },
    { kind: "action", key: "close", label: "Close", icon: "close", onSelect: actions.close },
  );
  return rows;
}
