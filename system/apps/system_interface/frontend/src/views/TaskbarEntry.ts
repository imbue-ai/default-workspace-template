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
  /** Whether this entry's window is being peeked at right now: its picture stands where the bubble would. */
  readonly isPeeked: boolean;
  readonly onClick: () => void;
  readonly onContextMenu: (x: number, y: number, target: Element) => void;
  /** The pointer arriving on (true) or leaving (false) a MINIMIZED entry, which peeks at its window. */
  readonly onPeek: (isPeeking: boolean) => void;
}

export const TaskbarEntry: m.Component<TaskbarEntryAttrs> = {
  view(vnode) {
    const { entry, avatar, isMenuOpen, isPeeked, onClick, onContextMenu, onPeek } = vnode.attrs;
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
        // The visible title names it; this is the same string, with the avatar's staleness note when
        // there is one, so a reader is told what a looker can see.
        "aria-label": tooltip,
        class:
          // A labelled chip rather than a bare tile: the icon and the name sit in it, and the chip is
          // what carries the states, since a tint behind an icon that no longer fills the entry has
          // somewhere to show. Body text -- this is a name being read, not a dense list.
          "taskbar-entry relative flex h-(--desk-taskbar-entry-size) min-w-(--desk-touch-target) max-w-48 " +
          "shrink-0 items-center gap-2 rounded-xl border p-1 text-(length:--font-size-body) " +
          "outline-none select-none touch-pan-x focus-visible:ring-2 focus-visible:ring-accent " +
          (entry.isFocused ? "border-default bg-surface shadow-raised " : "border-transparent hover:bg-fill-hover ") +
          // A window's name is a name whichever window you are in: the chip's surface and its
          // elevation say which one is focused, and the text stays out of it. Faint is a different
          // thing -- the window is out of sight, not merely not in front.
          (isDimmed ? "text-faint " : "text-primary ") +
          (isMenuOpen ? "bg-fill-active " : ""),
        // The peek stands in the bubble's place and says more than it does, so the two never show at
        // once. The pause is the shared one again: the entry names itself now, so the bubble is an
        // aside (the whole of a truncated title) rather than the only thing saying what this is.
        ...hoverTooltipAttrs(isPeeked ? null : tooltip, "above"),
        // Only a minimized window peeks: a window already on the desktop is its own preview. Entering
        // any other entry ends the peek the one beside it started, so a sweep along the bar is clean.
        onmouseenter: () => onPeek(entry.isMinimized),
        onmouseleave: () => onPeek(false),
        onclick: () => {
          // The pointer is already resting here, so no enter or leave will fire for what the click
          // changes: a click that puts the window away has to start the peek itself, and one that
          // brings it back has to end it.
          const willMinimize = !entry.isDetached && !entry.isMinimized && entry.isFocused;
          onClick();
          onPeek(willMinimize);
        },
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
            // corner is the tile's own 32 per cent. The chip around it carries the states now; what
            // is left here is the fade for a window that is out of sight.
            class:
              "taskbar-entry-tile flex size-(--desk-taskbar-entry-icon) shrink-0 items-center justify-center " +
              (isAvatar ? "rounded-2xl [&>img]:size-full " : "rounded-[32%] [&>svg]:size-full ") +
              (isDimmed ? "opacity-70" : ""),
          },
          image,
        ),
        m("span", { class: "taskbar-entry-title min-w-0 truncate" }, entry.title),
        // The mark of a window shown in its own desktop window, so the dimmed entry is not read as
        // minimized. Beside the name, where there is room for it again.
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
