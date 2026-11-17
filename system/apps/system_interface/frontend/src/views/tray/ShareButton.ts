/**
 * The tray's share button (the multiplayer spec, section 6.1): a dashed circle with a plus that asks the Imbue
 * Studio chrome to open the share panel on the whole workspace (``minds:open-share-settings`` with no service
 * named, embed contract v13). Drawn only under a chrome that has said what it can do, never on the web share.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import { OPEN_SHARE_SETTINGS, sendToEmbedder } from "@imbue/workspace-ui/src/embed";

const PLUS_GLYPH_SIZE = 14;

export function openWorkspaceShareSettings(): void {
  sendToEmbedder(OPEN_SHARE_SETTINGS, {});
}

export const ShareButton: m.Component = {
  view() {
    return m(
      Button,
      {
        icon: true,
        round: true,
        extra: "tray-share shrink-0 border-dashed",
        "data-tray-share": "",
        "aria-label": "Share this workspace",
        ...hoverTooltipAttrs("Share this workspace", "above"),
        onclick: openWorkspaceShareSettings,
      },
      m.trust(icon("plus", { size: PLUS_GLYPH_SIZE })),
    );
  },
};
