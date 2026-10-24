/**
 * The taskbar (plan section 4.10), left to right: the launcher field, the app widgets this client
 * put in the bar's row (``TrayWidgets.ts``), one entry per window of the active desktop in opening
 * order, the system tray. Always visible on the desktop layout; a phone draws its own bar instead.
 */

import m from "mithril";
import type { AvatarState, TaskbarEntry as TaskbarEntryRecord } from "../reducers/desktopState";
import { LauncherField } from "./LauncherField";
import type { LauncherFieldAttrs } from "./LauncherField";
import { SystemTray } from "./SystemTray";
import type { SystemTrayAttrs } from "./SystemTray";
import { TaskbarEntry } from "./TaskbarEntry";
import { TrayWidgetPopover, TrayWidgetSlots } from "./TrayWidgets";
import type { TrayWidget, TrayWidgetPopoverAttrs } from "./TrayWidgets";

export interface TaskbarAttrs {
  readonly entries: readonly TaskbarEntryRecord[];
  readonly avatar: AvatarState;
  readonly openEntryMenuWindowId: string | null;
  readonly launcher: LauncherFieldAttrs;
  readonly tray: SystemTrayAttrs;
  readonly onEntryClick: (windowId: string) => void;
  readonly onEntryContextMenu: (windowId: string, x: number, y: number, target: Element) => void;
  /** The app widgets in the bar's row. */
  readonly toolbarWidgets: readonly TrayWidget[];
  /** The open popover of a widget in the taskbar, or null. */
  readonly widgetPopover: TrayWidgetPopoverAttrs | null;
  /** The host the tray widgets' frames live in was made, or is gone. */
  readonly onTrayFramesHostCreated: (host: HTMLElement) => void;
  readonly onTrayFramesHostRemoved: (host: HTMLElement) => void;
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
        attrs.toolbarWidgets.length === 0
          ? null
          : m(
              "div",
              {
                "data-toolbar-widgets": "",
                // As tall as the bar, so a popover opened from here stands on the bar's top edge.
                class: "toolbar-widgets relative flex shrink-0 items-center gap-2 self-stretch",
              },
              [
                m(TrayWidgetSlots, {
                  spot: "toolbar",
                  widgets: attrs.toolbarWidgets,
                  popoverApp: attrs.widgetPopover?.app ?? null,
                  onPress: attrs.tray.onWidgetPress,
                  onContextMenu: attrs.tray.onWidgetContextMenu,
                }),
                attrs.widgetPopover?.spot === "toolbar" ? m(TrayWidgetPopover, attrs.widgetPopover) : null,
              ],
            ),
        m(
          "div",
          {
            "data-taskbar-entries": "",
            // Wider than the chrome's usual gap: every entry carries a full-bleed icon tile of
            // its own colour, and at 4px two bright ones beside each other read as one block.
            //
            // The padding is the room the icons' casts are drawn into. ``overflow-x`` makes this a
            // scroll container, which the CSS spec then clips on BOTH axes (there is no scrolling
            // on one axis and overflow on the other), so without that room every cast is cut off
            // square at the strip's edge. The vertical room is the bar's own; the horizontal is
            // borrowed back with a negative margin, so the first entry keeps its distance from the
            // field beside it.
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
      attrs.widgetPopover?.spot === "tray" ? m(TrayWidgetPopover, attrs.widgetPopover) : null,
      // The tray widgets' frames' host, over the bar and its popover; its frames lie over their content boxes.
      m("div", {
        "data-tray-widget-frames": "",
        class: "tray-widget-frames pointer-events-none absolute inset-0 z-(--z-sticky) [&>*]:pointer-events-auto",
        oncreate: (created: m.VnodeDOM) => attrs.onTrayFramesHostCreated(created.dom as HTMLElement),
        onremove: (removed: m.VnodeDOM) => attrs.onTrayFramesHostRemoved(removed.dom as HTMLElement),
        onbeforeupdate: () => false,
      }),
    );
  },
};
