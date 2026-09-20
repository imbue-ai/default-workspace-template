/**
 * The floating entries (pinned-taskbar-entries plan section 4.2): a pinned entry a client popped out of
 * the bar, drawn in a layer above every window and live page inside the backdrop's stacking context,
 * at the theme's sticky level. The layer is inert; only each entry's own box takes a press. An entry is a
 * square button at the client's stored position (the drag is the gesture layer's, bound by
 * ``data-pinned-entry``) showing whether its window is shown or minimized the way the bar entry does. In
 * ``plain`` style it draws the app's icon on a raised tile; in ``avatar`` style, the workspace's avatar
 * wearing the current mood, on no tile at all.
 */

import m from "mithril";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import type { PixelRect } from "../geometry/frames";
import type { AvatarState, TaskbarEntry } from "../reducers/desktopState";
import { AvatarImage, avatarTooltip } from "./AvatarImage";
import { appGlyph } from "./glyphs";

/** The intrinsic size the glyph markup carries; the drawing fills the tile (``[&>svg]:size-full``). */
const FLOATING_GLYPH_MARKUP_SIZE = 32;

export interface FloatingEntriesAttrs {
  readonly entries: readonly TaskbarEntry[];
  readonly avatar: AvatarState;
  /** Where each entry's box is, in backdrop pixels (the drag's rectangle while one moves it). */
  readonly rectOf: (entry: TaskbarEntry) => PixelRect;
  readonly openMenuWindowId: string | null;
  readonly onClick: (windowId: string) => void;
  readonly onContextMenu: (windowId: string, x: number, y: number) => void;
}

export const FloatingEntries: m.Component<FloatingEntriesAttrs> = {
  view(vnode) {
    const attrs = vnode.attrs;
    return m(
      "div",
      { "data-floating-entries": "", class: "floating-entries pointer-events-none absolute inset-0 z-(--z-sticky)" },
      attrs.entries.map((entry) => {
        const rect = attrs.rectOf(entry);
        const look = entry.look;
        const style = look?.style ?? "plain";
        const isAvatar = style === "avatar";
        const isMenuOpen = attrs.openMenuWindowId === entry.window.id;
        const tooltip = isAvatar ? avatarTooltip(entry.title, attrs.avatar.status) : entry.title;
        return m(
          "button",
          {
            key: entry.window.id,
            type: "button",
            "data-pinned-entry": entry.window.app,
            "data-entry-mode": "floating",
            "data-entry-style": style,
            "data-mood": isAvatar ? attrs.avatar.status.mood : undefined,
            "data-stale": isAvatar ? (attrs.avatar.status.is_stale ? "true" : "false") : undefined,
            "data-minimized": entry.isMinimized ? "true" : "false",
            "data-focused": entry.isFocused ? "true" : "false",
            "aria-pressed": entry.isFocused ? "true" : "false",
            "aria-label": tooltip,
            class:
              "floating-entry pointer-events-auto absolute flex cursor-pointer items-center justify-center " +
              "rounded-2xl touch-none select-none outline-none focus-visible:ring-2 focus-visible:ring-accent " +
              (isAvatar
                ? "border-0 bg-transparent p-0 [&>img]:size-full "
                : "border p-2 [&>svg]:size-full " +
                  (entry.isFocused
                    ? "border-default bg-surface text-primary shadow-overlay "
                    : "border-subtle bg-surface/90 text-secondary shadow-raised hover:bg-fill-hover ")) +
              (entry.isMinimized ? "opacity-70 " : "") +
              (isMenuOpen && !isAvatar ? "bg-fill-active" : ""),
            style: {
              left: `${rect.x}px`,
              top: `${rect.y}px`,
              width: `${rect.width}px`,
              height: `${rect.height}px`,
            },
            ...hoverTooltipAttrs(tooltip),
            onclick: () => attrs.onClick(entry.window.id),
            oncontextmenu: (event: MouseEvent) => {
              event.preventDefault();
              attrs.onContextMenu(entry.window.id, event.clientX, event.clientY);
            },
          },
          isAvatar
            ? m(AvatarImage, {
                design: attrs.avatar.design,
                defaultDesign: attrs.avatar.defaultDesign,
                mood: attrs.avatar.status.mood,
                class: "size-full",
              })
            : m.trust(appGlyph(entry.app, FLOATING_GLYPH_MARKUP_SIZE)),
        );
      }),
    );
  },
};
