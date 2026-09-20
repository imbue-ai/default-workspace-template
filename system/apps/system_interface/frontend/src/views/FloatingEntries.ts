/**
 * The floating entries (pinned-taskbar-entries plan section 4.2): a pinned entry a client popped out of
 * the bar, drawn in a layer above every window and live page inside the backdrop's stacking context,
 * at the theme's sticky level. The layer is inert; only each entry's own box takes a press. An entry is a
 * square button at the client's stored position (the drag is the gesture layer's, bound by
 * ``data-pinned-entry``) showing whether its window is shown or minimized the way the bar entry does. In
 * ``plain`` style it draws the app's icon on a raised tile.
 */

import m from "mithril";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import type { PixelRect } from "../geometry/frames";
import type { TaskbarEntry } from "../reducers/desktopState";
import { appGlyph } from "./glyphs";

/** The intrinsic size the glyph markup carries; the drawing fills the tile (``[&>svg]:size-full``). */
const FLOATING_GLYPH_MARKUP_SIZE = 32;

export interface FloatingEntriesAttrs {
  readonly entries: readonly TaskbarEntry[];
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
        const isMenuOpen = attrs.openMenuWindowId === entry.window.id;
        return m(
          "button",
          {
            key: entry.window.id,
            type: "button",
            "data-pinned-entry": entry.window.app,
            "data-entry-mode": "floating",
            "data-entry-style": style,
            "data-minimized": entry.isMinimized ? "true" : "false",
            "data-focused": entry.isFocused ? "true" : "false",
            "aria-pressed": entry.isFocused ? "true" : "false",
            "aria-label": entry.title,
            class:
              "floating-entry pointer-events-auto absolute flex cursor-pointer items-center justify-center " +
              "rounded-2xl border p-2 touch-none select-none outline-none focus-visible:ring-2 focus-visible:ring-accent " +
              "[&>svg]:size-full " +
              (entry.isFocused
                ? "border-default bg-surface text-primary shadow-overlay "
                : "border-subtle bg-surface/90 text-secondary shadow-raised hover:bg-fill-hover ") +
              (entry.isMinimized ? "opacity-70 " : "") +
              (isMenuOpen ? "bg-fill-active" : ""),
            style: {
              left: `${rect.x}px`,
              top: `${rect.y}px`,
              width: `${rect.width}px`,
              height: `${rect.height}px`,
            },
            ...hoverTooltipAttrs(entry.title),
            onclick: () => attrs.onClick(entry.window.id),
            oncontextmenu: (event: MouseEvent) => {
              event.preventDefault();
              attrs.onContextMenu(entry.window.id, event.clientX, event.clientY);
            },
          },
          m.trust(appGlyph(entry.app, FLOATING_GLYPH_MARKUP_SIZE)),
        );
      }),
    );
  },
};
