/**
 * The Desktops tray widget (concepts.md section 2.8): one glyph per desktop (the squiggle in the
 * desktop's colour), the active one marked, a click switching; its menu offers a new desktop,
 * the active desktop's settings, and its deletion.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import type { Desktop } from "../model/records";
import { SQUIGGLE_GLYPHS, monogramMarkup, squiggleMarkup } from "./squiggles";
import { glyph } from "./glyphs";

const DESKTOP_GLYPH_SIZE = 18;
const KEBAB_GLYPH_SIZE = 14;

/** Full <svg> string for a desktop's identity: its squiggle in its colour, or its monogram. */
export function desktopIdentityMarkup(desktop: Pick<Desktop, "name" | "color" | "glyph">, size: number): string {
  const isDrawable = Number.isInteger(desktop.glyph) && desktop.glyph >= 0 && desktop.glyph < SQUIGGLE_GLYPHS.length;
  return isDrawable
    ? squiggleMarkup(desktop.glyph, desktop.color || null, size)
    : monogramMarkup(desktop.name, desktop.color, size);
}

export interface DesktopsWidgetAttrs {
  readonly desktops: readonly Desktop[];
  readonly activeDesktopId: string | null;
  readonly isMenuOpen: boolean;
  readonly onSwitch: (desktopId: string) => void;
  readonly onOpenMenu: (event: MouseEvent) => void;
  readonly onDesktopContextMenu: (desktopId: string, x: number, y: number) => void;
}

export const DesktopsWidget: m.Component<DesktopsWidgetAttrs> = {
  view(vnode) {
    const { desktops, activeDesktopId, isMenuOpen, onSwitch, onOpenMenu, onDesktopContextMenu } = vnode.attrs;
    return m("div", { "data-tray-widget": "desktops", class: "tray-desktops flex items-center gap-0.5" }, [
      desktops.map((desktop) => {
        const isActive = desktop.id === activeDesktopId;
        return m(
          Button,
          {
            key: desktop.id,
            variant: "ghost",
            icon: true,
            sm: true,
            selected: isActive,
            extra: "desktop-switch min-h-(--desk-touch-target) min-w-(--desk-touch-target)",
            "data-desktop-switch": desktop.id,
            "data-active": isActive ? "true" : "false",
            "aria-pressed": isActive ? "true" : "false",
            "aria-label": `Switch to ${desktop.name}`,
            ...hoverTooltipAttrs(desktop.name),
            onclick: () => onSwitch(desktop.id),
            oncontextmenu: (event: MouseEvent) => {
              event.preventDefault();
              onDesktopContextMenu(desktop.id, event.clientX, event.clientY);
            },
          },
          m.trust(desktopIdentityMarkup(desktop, DESKTOP_GLYPH_SIZE)),
        );
      }),
      m(
        Button,
        {
          variant: "ghost",
          icon: true,
          sm: true,
          extra: "desktops-menu min-h-(--desk-touch-target) min-w-(--desk-touch-target)",
          "data-desktops-menu": "",
          "aria-label": "Desktop options",
          "aria-haspopup": "menu",
          "aria-expanded": isMenuOpen ? "true" : "false",
          ...hoverTooltipAttrs("Desktop options"),
          onclick: onOpenMenu,
        },
        m.trust(glyph("kebab", KEBAB_GLYPH_SIZE)),
      ),
    ]);
  },
};
