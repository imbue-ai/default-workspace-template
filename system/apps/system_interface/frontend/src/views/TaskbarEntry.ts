/**
 * One taskbar entry (plan section 4.10): a window of the active desktop, drawn as the app's own
 * icon tile at the launcher field's height, dimmed while minimized, the focused one marked. A
 * click restores and raises, minimizes the focused window, or raises; a right click or long press
 * opens the entry's menu. A pinned entry in the ``avatar`` style draws the workspace's avatar in
 * place of the icon, wearing the current mood.
 *
 * The title is the hover tooltip rather than a label beside the icon, and it is the one tooltip in
 * the workspace that skips the hover-intent pause: nothing is written on the entry, so the bubble
 * is not an aside about a control that already names itself -- it IS the name, and a pause before
 * it is the name arriving late.
 */

import m from "mithril";
import { targetElementOf } from "@imbue/workspace-ui/src/context_menu_rows";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import type { AvatarState, TaskbarEntry as TaskbarEntryRecord } from "../reducers/desktopState";
import { entryStyleParts } from "./AvatarImage";

/** The intrinsic size the glyph markup carries; the drawing fills the tile (``[&>svg]:size-full``). */
const ENTRY_GLYPH_MARKUP_SIZE = 48;
const DETACHED_GLYPH_SIZE = 12;

export interface TaskbarEntryAttrs {
  readonly entry: TaskbarEntryRecord;
  readonly avatar: AvatarState;
  readonly isMenuOpen: boolean;
  readonly onClick: () => void;
  readonly onContextMenu: (x: number, y: number, target: Element) => void;
}

export const TaskbarEntry: m.Component<TaskbarEntryAttrs> = {
  view(vnode) {
    const { entry, avatar, isMenuOpen, onClick, onContextMenu } = vnode.attrs;
    // Out of sight here either way: minimized, or shown in a desktop window of the chrome's own.
    const isDimmed = entry.isMinimized || entry.isDetached;
    const look = entry.look;
    const { isAvatar, attrs, tooltip, image } = entryStyleParts(entry, avatar, ENTRY_GLYPH_MARKUP_SIZE, "size-full");
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
        // The entry carries no text, so the title is its accessible name as well as its tooltip.
        "aria-label": tooltip,
        class:
          // The box is the hit area, not the drawing: it keeps the touch target the rest of the
          // chrome uses while the tile inside stays square, which ``size-full`` on the art needs.
          "taskbar-entry relative flex h-(--desk-taskbar-entry-size) min-w-(--desk-touch-target) shrink-0 " +
          "items-center justify-center rounded-[32%] border-0 bg-transparent p-0 " +
          "outline-none select-none touch-pan-x focus-visible:ring-2 focus-visible:ring-accent " +
          (isMenuOpen ? "ring-2 ring-accent " : ""),
        ...hoverTooltipAttrs(tooltip, "above", "instant"),
        onclick: onClick,
        oncontextmenu: (event: MouseEvent) => {
          event.preventDefault();
          onContextMenu(event.clientX, event.clientY, targetElementOf(event));
        },
      },
      [
        m(
          "span",
          {
            // The icon brings its own tile (`docs/system/app-icons.md`), so this box paints nothing
            // and pads nothing: a surface behind it would frame the tile in a second one. The
            // corner is the tile's own 32 per cent, and depth says which window is focused, since a
            // tint behind an opaque tile cannot be seen. The text colour still matters: the
            // built-in glyph for a window whose app the registry no longer has is the one drawing
            // here that takes it. Nothing answers the hover: the tooltip does that, at once.
            class:
              "taskbar-entry-tile flex size-(--desk-taskbar-entry-size) items-center justify-center " +
              (isAvatar
                ? "rounded-2xl [&>img]:size-full "
                : "rounded-[32%] [&>svg]:size-full " +
                  // One step of elevation, not two. The bar leaves 6px around a tile, and the
                  // --shadow-overlay a floating entry wears reaches 13px below itself: on the
                  // wallpaper it has the whole backdrop to fall on, here it would be cut off by
                  // the strip that scrolls. So the focused one is raised and the rest lie flat,
                  // which is the same relation inside the room there is.
                  (entry.isFocused ? "text-primary shadow-raised " : "text-secondary ")) +
              (isDimmed ? "opacity-70" : ""),
          },
          image,
        ),
        // The mark of a window shown in its own desktop window, so the dimmed entry is not read as
        // minimized. A badge on the tile's corner rather than a glyph beside it: there is no room
        // beside it any more.
        entry.isDetached
          ? m(
              "span",
              {
                class:
                  "absolute right-0 bottom-0 flex items-center rounded-full bg-surface p-px text-faint shadow-raised",
                "aria-label": "In its own window",
              },
              m.trust(icon("external-link", { size: DETACHED_GLYPH_SIZE })),
            )
          : null,
      ],
    );
  },
};
