/**
 * One taskbar entry (plan section 4.10): a window of the active desktop, the app's icon and the
 * title (icon only in compact mode), dimmed while minimized or while settling on another
 * client's open, the focused one marked. A click restores and raises, minimizes the focused
 * window, or raises; a right click or long press opens the entry's menu.
 */

import m from "mithril";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import type { TaskbarEntry as TaskbarEntryRecord } from "../reducers/desktopState";
import { appGlyph } from "./glyphs";

const ENTRY_GLYPH_SIZE = 16;

export interface TaskbarEntryAttrs {
  readonly entry: TaskbarEntryRecord;
  readonly isCompact: boolean;
  readonly isMenuOpen: boolean;
  readonly onClick: () => void;
  readonly onContextMenu: (x: number, y: number) => void;
}

export const TaskbarEntry: m.Component<TaskbarEntryAttrs> = {
  view(vnode) {
    const { entry, isCompact, isMenuOpen, onClick, onContextMenu } = vnode.attrs;
    const isDimmed = entry.isMinimized || entry.window.is_settling;
    return m(
      "button",
      {
        type: "button",
        "data-taskbar-entry": entry.window.id,
        "data-minimized": entry.isMinimized ? "true" : "false",
        "data-focused": entry.isFocused ? "true" : "false",
        "data-settling": entry.window.is_settling ? "true" : "false",
        "aria-pressed": entry.isFocused ? "true" : "false",
        class:
          "taskbar-entry flex h-9 min-w-(--desk-touch-target) shrink-0 items-center gap-2 rounded-md border px-2 " +
          "text-(length:--font-size-row) select-none touch-none " +
          (isCompact ? "max-w-11 " : "max-w-48 ") +
          (entry.isFocused
            ? "border-default bg-surface text-primary shadow-raised "
            : "border-transparent hover:bg-fill-hover ") +
          (isDimmed ? "text-faint " : entry.isFocused ? "" : "text-secondary ") +
          (isMenuOpen ? "bg-fill-active" : ""),
        ...hoverTooltipAttrs(isCompact ? entry.title : null),
        onclick: onClick,
        oncontextmenu: (event: MouseEvent) => {
          event.preventDefault();
          onContextMenu(event.clientX, event.clientY);
        },
      },
      [
        m(
          "span",
          { class: "flex shrink-0 items-center" + (isDimmed ? " opacity-60" : "") },
          m.trust(appGlyph(entry.app, ENTRY_GLYPH_SIZE)),
        ),
        isCompact ? null : m("span", { class: "taskbar-entry-title min-w-0 truncate" }, entry.title),
      ],
    );
  },
};
