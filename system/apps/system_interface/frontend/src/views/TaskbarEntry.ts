/**
 * One taskbar entry (plan section 4.10): a window of the active desktop, drawn as the app's own icon
 * tile with the window's title beside it, dimmed while minimized, the focused one marked. A click
 * restores and raises, minimizes the focused window, or raises; a right click or long press opens
 * the entry's menu. A pinned entry in the ``avatar`` style draws the workspace's avatar in place of
 * the icon, wearing the current mood.
 *
 * The icon says which app and the title says which window, so neither has to do both: the icon is
 * smaller than the entry, and the title takes the room that leaves, truncated where the bar runs
 * out. The hover bubble carries the whole of a truncated one.
 */

import m from "mithril";
import { partAttrs } from "@imbue/workspace-ui/src/themes/parts";
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
        ...partAttrs("taskbar-entry"),
        "aria-pressed": entry.isFocused ? "true" : "false",
        // The visible title names it; this is the same string, with the avatar's staleness note when
        // there is one, so a reader is told what a looker can see.
        "aria-label": tooltip,
        class:
          // A labelled chip: the icon and the name sit in it, and the chip is what carries the
          // states. Body text -- this is a name being read, not a dense list.
          "taskbar-entry group relative flex h-(--desk-taskbar-entry-size) min-w-(--desk-touch-target) max-w-48 " +
          // 4px all round, but 8 on the right: the name ends where the chip does otherwise, and a
          // title cut off mid-word wants somewhere to stop.
          "shrink-0 items-center gap-1.5 rounded-md p-1 pr-2 text-(length:--font-size-body) " +
          "outline-none select-none touch-pan-x focus-visible:ring-2 focus-visible:ring-accent " +
          // The chrome's own two control tints and nothing else: the hover one under the pointer, the
          // active one for the window you are in (and for the entry whose menu is open, which is the
          // same statement about the same chip). No border, no elevation -- a tint is the whole of it.
          (entry.isFocused || isMenuOpen ? "bg-fill-active " : "hover:bg-fill-hover ") +
          // A window's name is a name whichever window you are in: the tint says which is focused and
          // the text stays out of it. Faint is a different thing -- the window is out of sight, not
          // merely not in front -- and it lifts under the pointer, which is the one moment the entry
          // is being read rather than scanned.
          (isDimmed ? "text-faint hover:text-primary " : "text-primary "),
        // The bubble carries the whole of a title the chip had to cut short.
        ...hoverTooltipAttrs(tooltip, "above"),
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
            // and pads nothing: a surface behind it would frame the tile in a second one, and the
            // corner is the tile's own 32 per cent. The chip around it carries the states; what is
            // left here is the fade for a window that is out of sight.
            class:
              "flex size-(--desk-taskbar-entry-icon) shrink-0 items-center " +
              // The bar's own quieter cast, the same in every state, selected included: the tint
              // behind the icon is what says selected, and the icon has no reason to sit
              // differently on it.
              "justify-center shadow-(--desk-taskbar-entry-shadow) " +
              (isAvatar ? "rounded-2xl [&>img]:size-full " : "rounded-[32%] [&>svg]:size-full ") +
              // Not the avatar, though: the fade reads as "not on screen" on an app's icon and as
              // unwell on a character.
              (isDimmed && !isAvatar ? "opacity-70 group-hover:opacity-100" : ""),
          },
          image,
        ),
        m("span", { class: "taskbar-entry-title min-w-0 truncate" }, entry.title),
        // The mark of a window shown in its own desktop window, so the dimmed entry is not read as
        // minimized.
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
