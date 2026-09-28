/**
 * One taskbar entry (plan section 4.10): a window of the active desktop, the app's icon and the
 * title (icon only in compact mode), dimmed while minimized, the focused one marked. A click
 * restores and raises, minimizes the focused window, or raises; a right click or long press opens
 * the entry's menu. A pinned entry in the ``avatar`` style draws the workspace's avatar in place of
 * the icon, wearing the current mood.
 */

import m from "mithril";
import { targetElementOf } from "@imbue/workspace-ui/src/context_menu_rows";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import type { AvatarState, TaskbarEntry as TaskbarEntryRecord } from "../reducers/desktopState";
import { entryStyleParts } from "./AvatarImage";

const ENTRY_GLYPH_SIZE = 16;
const DETACHED_GLYPH_SIZE = 12;

export interface TaskbarEntryAttrs {
  readonly entry: TaskbarEntryRecord;
  readonly avatar: AvatarState;
  readonly isCompact: boolean;
  readonly isMenuOpen: boolean;
  readonly onClick: () => void;
  readonly onContextMenu: (x: number, y: number, target: Element) => void;
}

export const TaskbarEntry: m.Component<TaskbarEntryAttrs> = {
  view(vnode) {
    const { entry, avatar, isCompact, isMenuOpen, onClick, onContextMenu } = vnode.attrs;
    // Out of sight here either way: minimized, or shown in a desktop window of the chrome's own.
    const isDimmed = entry.isMinimized || entry.isDetached;
    const look = entry.look;
    const { isAvatar, attrs, tooltip, image } = entryStyleParts(entry, avatar, ENTRY_GLYPH_SIZE, "size-7");
    return m(
      "button",
      {
        type: "button",
        "data-taskbar-entry": entry.window.id,
        "data-pinned": entry.isPinned ? "true" : "false",
        "data-pinned-entry": look === null ? undefined : entry.window.app,
        "data-entry-mode": look === null ? undefined : "bar",
        "data-entry-style": look === null ? undefined : look.style,
        ...attrs,
        "data-minimized": entry.isMinimized ? "true" : "false",
        "data-detached": entry.isDetached ? "true" : "false",
        "data-focused": entry.isFocused ? "true" : "false",
        "aria-pressed": entry.isFocused ? "true" : "false",
        // Icon only in compact mode, so the title names the button there.
        "aria-label": isCompact ? entry.title : undefined,
        class:
          "taskbar-entry flex h-9 min-w-(--desk-touch-target) shrink-0 items-center gap-2 rounded-md border px-2 " +
          "text-(length:--font-size-row) select-none touch-pan-x " +
          (isCompact ? "max-w-11 " : "max-w-48 ") +
          (entry.isFocused
            ? "border-default bg-surface text-primary shadow-raised "
            : "border-transparent hover:bg-fill-hover ") +
          (isDimmed ? "text-faint " : entry.isFocused ? "" : "text-secondary ") +
          (isMenuOpen ? "bg-fill-active" : ""),
        ...hoverTooltipAttrs(isCompact || (isAvatar && avatar.status.is_stale) ? tooltip : null),
        onclick: onClick,
        oncontextmenu: (event: MouseEvent) => {
          event.preventDefault();
          onContextMenu(event.clientX, event.clientY, targetElementOf(event));
        },
      },
      [
        m("span", { class: "flex shrink-0 items-center" + (isDimmed ? " opacity-60" : "") }, image),
        isCompact ? null : m("span", { class: "taskbar-entry-title min-w-0 truncate" }, entry.title),
        // The mark of a window shown in its own desktop window, so the dimmed entry is not read as minimized.
        entry.isDetached
          ? m(
              "span",
              { class: "flex shrink-0 items-center text-faint", "aria-label": "In its own window" },
              m.trust(icon("external-link", { size: DETACHED_GLYPH_SIZE })),
            )
          : null,
      ],
    );
  },
};
