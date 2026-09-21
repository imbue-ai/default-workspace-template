/**
 * The taskbar (plan section 4.10), left to right: the launcher field, one entry per window of
 * the active desktop in opening order, the system tray. Always visible in V1; in compact mode
 * it takes the compact height and shows icons only.
 */

import m from "mithril";
import type { AvatarState, TaskbarEntry as TaskbarEntryRecord } from "../reducers/desktopState";
import { LauncherField } from "./LauncherField";
import type { LauncherFieldAttrs } from "./LauncherField";
import { SystemTray } from "./SystemTray";
import type { SystemTrayAttrs } from "./SystemTray";
import { TaskbarEntry } from "./TaskbarEntry";

export interface TaskbarAttrs {
  readonly entries: readonly TaskbarEntryRecord[];
  readonly avatar: AvatarState;
  readonly isCompact: boolean;
  readonly openEntryMenuWindowId: string | null;
  readonly launcher: LauncherFieldAttrs;
  readonly tray: SystemTrayAttrs;
  readonly onEntryClick: (windowId: string) => void;
  readonly onEntryContextMenu: (windowId: string, x: number, y: number) => void;
}

export const Taskbar: m.Component<TaskbarAttrs> = {
  view(vnode) {
    const attrs = vnode.attrs;
    return m(
      "div",
      {
        "data-taskbar": "",
        class:
          "taskbar relative flex h-(--desk-taskbar-height) shrink-0 items-center gap-2 border-t border-default " +
          "bg-(--desk-taskbar-surface) px-2 backdrop-blur",
      },
      [
        m(LauncherField, attrs.launcher),
        m(
          "div",
          {
            "data-taskbar-entries": "",
            class: "taskbar-entries flex min-w-0 flex-1 items-center gap-1 overflow-x-auto",
          },
          attrs.entries.map((entry) =>
            m(TaskbarEntry, {
              key: entry.window.id,
              entry,
              avatar: attrs.avatar,
              isCompact: attrs.isCompact,
              isMenuOpen: attrs.openEntryMenuWindowId === entry.window.id,
              onClick: () => attrs.onEntryClick(entry.window.id),
              onContextMenu: (x, y) => attrs.onEntryContextMenu(entry.window.id, x, y),
            }),
          ),
        ),
        m(SystemTray, attrs.tray),
      ],
    );
  },
};
