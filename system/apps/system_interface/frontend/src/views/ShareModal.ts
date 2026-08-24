/**
 * Informational modal for the per-service Share button.
 *
 * Sharing is configured from the Minds desktop app's workspace settings
 * page: TLS terminates inside this workspace (the share-gateway service)
 * and access is granted per email address. The system_interface no
 * longer routes share-button clicks back to minds via request events --
 * this modal just tells the user where to go.
 */

import m from "mithril";
import { backdropDismissAttrs } from "./modalBackdrop";
import { hoverTooltipAttrs } from "./hoverTooltip";

interface ShareModalAttrs {
  serviceName: string;
  onClose: () => void;
}

export const ShareModal: m.Component<ShareModalAttrs> = {
  view(vnode) {
    const { serviceName, onClose } = vnode.attrs;
    return m("div.share-modal-overlay", backdropDismissAttrs(onClose), [
      m("div.share-modal", [
        m("div.share-modal-header", [
          m("h3.share-modal-title", `Share "${serviceName}"`),
          m("button.share-modal-close-x", { onclick: onClose, ...hoverTooltipAttrs("Close") }, "x"),
        ]),
        m("div", { style: "padding: 8px 0; color: #444; font-size: 14px; line-height: 1.5;" }, [
          m("p", { style: "margin: 0 0 12px 0;" }, [
            "To share this service externally, open the Minds desktop app, go to ",
            m("strong", "workspace settings"),
            ", and enable sharing for the ",
            m("strong", `"${serviceName}"`),
            " service.",
          ]),
          m(
            "p",
            { style: "margin: 0; color: #666;" },
            "Shared traffic is encrypted end-to-end into this workspace, and access is granted per email address.",
          ),
        ]),
        m("div.share-modal-footer", [
          m("button.share-modal-btn.share-modal-btn-secondary", { onclick: onClose }, "Close"),
        ]),
      ]),
    ]);
  },
};
