/**
 * The Desktops tray widget (concepts.md section 2.8).
 *
 * Parked: one grid button standing for the desktops, doing nothing when pressed. The widget used to
 * draw a glyph per desktop -- the squiggle in the desktop's colour, the active one marked, a click
 * switching -- with a kebab beside them for the new/settings/delete menu. All of that still exists
 * behind it (the switching, the menu, the settings dialog, ``desktopIdentityMarkup``, and the attrs
 * this still takes); what is undecided is how the desktops should come back into view, not whether.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import type { Desktop } from "../model/records";
import { SQUIGGLE_GLYPHS, monogramMarkup, squiggleMarkup } from "./squiggles";
import { glyph } from "./glyphs";

const TRAY_GLYPH_SIZE = 20;

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
  readonly onDesktopContextMenu: (desktopId: string, x: number, y: number, target: Element) => void;
}

export const DesktopsWidget: m.Component<DesktopsWidgetAttrs> = {
  view() {
    return m("div", { "data-tray-widget": "desktops", class: "tray-desktops flex items-center gap-0.5" }, [
      m(
        Button,
        {
          variant: "ghost",
          icon: true,
          sm: true,
          extra: "min-h-(--desk-touch-target) min-w-(--desk-touch-target)",
          // The hook everything addresses the widget by, kept through the parking so whatever brings
          // the desktops back finds the same button here.
          "data-desktops-menu": "",
          "aria-label": "Desktops",
          onclick: () => {},
        },
        m.trust(glyph("layout-grid", TRAY_GLYPH_SIZE)),
      ),
    ]);
  },
};
