/**
 * The taskbar (plan section 4.10), left to right: the launcher field, one entry per window of
 * the active desktop in opening order, the system tray. Always visible in V1; in compact mode
 * it takes the compact height.
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
  readonly openEntryMenuWindowId: string | null;
  readonly launcher: LauncherFieldAttrs;
  readonly tray: SystemTrayAttrs;
  readonly onEntryClick: (windowId: string) => void;
  readonly onEntryContextMenu: (windowId: string, x: number, y: number, target: Element) => void;
}

export const Taskbar: m.Component<TaskbarAttrs> = {
  view(vnode) {
    const attrs = vnode.attrs;
    return m(
      "div",
      {
        "data-taskbar": "",
        // No line along the top and no blur behind it: the surface alone.
        class: "taskbar relative h-(--desk-taskbar-height) shrink-0 bg-(--desk-taskbar-surface)",
      },
      // One element around everything in the bar, holding the row and nothing else -- no colour, no
      // border, no corner. It is the handle for dressing the bar's contents: give this a background,
      // a radius, an inset, and the bar's box stays where the geometry expects it.
      m("div", { "data-taskbar-content": "", class: "taskbar-content flex h-full items-center gap-2 px-2" }, [
        m(LauncherField, attrs.launcher),
        m(
          "div",
          {
            "data-taskbar-entries": "",
            // Wider than the chrome's usual gap: the entries are full-bleed tiles of their own
            // colour, and at 4px two bright ones beside each other read as one block.
            //
            // The padding is what the tiles' shadows are drawn into. ``overflow-x`` makes this a
            // scroll container, which the CSS spec then clips on BOTH axes (there is no scrolling
            // on one axis and overflow on the other), and the strip's box is exactly a tile -- so
            // without room inside it every shadow is cut off square at the tile's own edge. The
            // vertical room it takes is the bar's own; the horizontal is borrowed back with a
            // negative margin, so the first tile keeps its distance from the field beside it.
            class: "taskbar-entries -mx-1 flex min-w-0 flex-1 items-center gap-2 overflow-x-auto px-1 py-1.5",
          },
          attrs.entries.map((entry) =>
            m(TaskbarEntry, {
              key: entry.window.id,
              entry,
              avatar: attrs.avatar,
              isMenuOpen: attrs.openEntryMenuWindowId === entry.window.id,
              onClick: () => attrs.onEntryClick(entry.window.id),
              onContextMenu: (x, y, target) => attrs.onEntryContextMenu(entry.window.id, x, y, target),
            }),
          ),
        ),
        m(SystemTray, attrs.tray),
      ]),
    );
  },
};
