/**
 * The floating entries (pinned-taskbar-entries plan section 4.2): a pinned entry a client popped out of
 * the bar, drawn in a layer above every window and live page inside the backdrop's stacking context,
 * at the theme's sticky level. The layer is inert; only each entry's own box takes a press. An entry is a
 * square button at the client's stored position (the drag is the gesture layer's, bound by
 * ``data-pinned-entry``) showing whether its window is shown or minimized the way the bar entry does. In
 * ``plain`` style it draws the app's icon, which brings its own tile; in ``avatar`` style, the workspace's
 * avatar wearing the current mood. Neither is given a tile of the chrome's own.
 */

import m from "mithril";
import { targetElementOf } from "@imbue/workspace-ui/src/context_menu_rows";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import type { PixelRect } from "../geometry/frames";
import type { AvatarState, TaskbarEntry } from "../reducers/desktopState";
import { entryStyleParts } from "./AvatarImage";
import { rectStyle } from "./pixelStyle";

/** The intrinsic size the glyph markup carries; the drawing fills the tile (``[&>svg]:size-full``). */
const FLOATING_GLYPH_MARKUP_SIZE = 32;

export interface FloatingEntriesAttrs {
  readonly entries: readonly TaskbarEntry[];
  readonly avatar: AvatarState;
  /** Where each entry's box is, in backdrop pixels (the drag's rectangle while one moves it). */
  readonly rectOf: (entry: TaskbarEntry) => PixelRect;
  readonly openMenuWindowId: string | null;
  readonly onClick: (windowId: string) => void;
  readonly onContextMenu: (windowId: string, x: number, y: number, target: Element) => void;
}

export const FloatingEntries: m.Component<FloatingEntriesAttrs> = {
  view(vnode) {
    const attrs = vnode.attrs;
    return m(
      "div",
      { "data-floating-entries": "", class: "floating-entries pointer-events-none absolute inset-0 z-(--z-sticky)" },
      attrs.entries.map((entry) => {
        const rect = attrs.rectOf(entry);
        const style = entry.look?.style ?? "plain";
        const isMenuOpen = attrs.openMenuWindowId === entry.window.id;
        const parts = entryStyleParts(entry, attrs.avatar, FLOATING_GLYPH_MARKUP_SIZE, "size-full");
        const { isAvatar, tooltip } = parts;
        return m(
          "button",
          {
            key: entry.window.id,
            type: "button",
            "data-pinned-entry": entry.window.app,
            "data-entry-mode": "floating",
            "data-entry-style": style,
            ...parts.attrs,
            "data-minimized": entry.isMinimized ? "true" : "false",
            "data-detached": entry.isDetached ? "true" : "false",
            "data-focused": entry.isFocused ? "true" : "false",
            "aria-pressed": entry.isFocused ? "true" : "false",
            "aria-label": tooltip,
            class:
              "floating-entry pointer-events-auto absolute flex cursor-pointer items-center justify-center " +
              "touch-none select-none outline-none focus-visible:ring-2 focus-visible:ring-accent " +
              (isAvatar
                ? "rounded-2xl border-0 bg-transparent p-0 [&>img]:size-full "
                : // The icon brings its own tile (`docs/system/app-icons.md`), so this box paints nothing and
                  // pads nothing, the way a backdrop shortcut does not: a surface and 8px of padding around a
                  // tile framed it in a second one, which is the pale edge the avatar loses when it is
                  // switched to the icon. The corner is the tile's own 32 per cent rather than a fixed
                  // radius, so the shadow traces the drawing at whatever size the entry is. The hover is the
                  // tile growing and the open menu is a ring, since a tint behind the tile cannot be seen.
                  // The text colour still matters: the built-in glyph for a window whose app the registry no
                  // longer has is the one drawing here that takes it.
                  "rounded-[32%] border-0 bg-transparent p-0 [&>svg]:size-full transition-transform " +
                  "hover:scale-110 " +
                  (entry.isFocused ? "text-primary shadow-overlay " : "text-secondary shadow-raised ")) +
              // Out of sight here either way: minimized, or shown in a desktop window of the chrome's own.
              // Not the avatar, though: dimming a character reads as unwell rather than as put away.
              (!isAvatar && (entry.isMinimized || entry.isDetached) ? "opacity-70 " : "") +
              (isMenuOpen && !isAvatar ? "ring-2 ring-accent" : ""),
            style: rectStyle(rect),
            // Above for the avatar: it floats just over the taskbar, and the shared flip only fires
            // on viewport overflow, so the below placement would land the bubble on the bar.
            ...hoverTooltipAttrs(tooltip, isAvatar ? "above" : "below"),
            onclick: () => attrs.onClick(entry.window.id),
            oncontextmenu: (event: MouseEvent) => {
              event.preventDefault();
              attrs.onContextMenu(entry.window.id, event.clientX, event.clientY, targetElementOf(event));
            },
          },
          parts.image,
        );
      }),
    );
  },
};
