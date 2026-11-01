/**
 * The overview button (the multiplayer spec, section 8.2): four squares between the launcher field and the window
 * entries, tooltip "Manage Desktops", which opens and closes the overview over the backdrop. Hidden while a desktop
 * that is not the viewer's own is being viewed (section 8.3).
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { icon } from "@imbue/workspace-ui/src/components/icons";

const GRID_GLYPH_SIZE = 18;
const OVERVIEW_BUTTON_LABEL = "Manage Desktops";

export interface OverviewButtonAttrs {
  readonly isOpen: boolean;
  readonly onToggle: () => void;
}

export const OverviewButton: m.Component<OverviewButtonAttrs> = {
  view(vnode) {
    const { isOpen, onToggle } = vnode.attrs;
    return m(
      Button,
      {
        variant: "ghost",
        icon: true,
        sm: true,
        selected: isOpen,
        extra: "overview-button min-h-(--desk-touch-target) min-w-(--desk-touch-target)",
        "data-tray-widget": "overview",
        "aria-label": OVERVIEW_BUTTON_LABEL,
        "aria-pressed": isOpen ? "true" : "false",
        // No tooltip over the open overview: the button is pressed, and the overview says what it is.
        ...hoverTooltipAttrs(isOpen ? null : OVERVIEW_BUTTON_LABEL, "above"),
        onclick: onToggle,
      },
      m.trust(icon("layout-grid", { size: GRID_GLYPH_SIZE })),
    );
  },
};
