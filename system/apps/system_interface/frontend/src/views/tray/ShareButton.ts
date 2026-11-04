/**
 * The tray's share button (the multiplayer spec, section 6.1): a dashed circle with a plus that asks the Imbue
 * Studio chrome to open the share panel on the whole workspace (``minds:open-share-settings`` with no service
 * named, embed contract v13). Drawn only under a chrome that has said what it can do, never on the web share.
 */

import m from "mithril";
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
      "button",
      {
        type: "button",
        "data-tray-share": "",
        class:
          "tray-share flex size-8 shrink-0 cursor-pointer items-center justify-center rounded-full border border-dashed " +
          "border-strong bg-transparent p-0 text-secondary outline-none hover:bg-fill-hover hover:text-primary " +
          "focus-visible:ring-2 focus-visible:ring-accent",
        "aria-label": "Share this workspace",
        ...hoverTooltipAttrs("Share this workspace", "above"),
        onclick: openWorkspaceShareSettings,
      },
      m.trust(icon("plus", { size: PLUS_GLYPH_SIZE })),
    );
  },
};
