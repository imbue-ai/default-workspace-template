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
import { Modal } from "./Modal";
import { hoverTooltipAttrs } from "./hoverTooltip";

interface ShareModalAttrs {
  serviceName: string;
  onClose: () => void;
}

export const ShareModal: m.Component<ShareModalAttrs> = {
  view(vnode) {
    const { serviceName, onClose } = vnode.attrs;
    return m(
      Modal,
      {
        onDismiss: onClose,
        width: 480,
        header: [
          m("h3.modal-title", `Share "${serviceName}"`),
          // ml-auto pins the close button to the right of the title row.
          m(
            "button.btn.btn--icon.btn--sm.ml-auto",
            { type: "button", "aria-label": "Close", onclick: onClose, ...hoverTooltipAttrs("Close") },
            "×",
          ),
        ],
        actions: [m("button.btn.btn--secondary", { onclick: onClose }, "Close")],
      },
      [
        m("p.modal-message", [
          "To share this service externally, open the Minds desktop app, go to ",
          m("strong", "workspace settings"),
          ", and enable sharing for the ",
          m("strong", `"${serviceName}"`),
          " service.",
        ]),
        m(
          "p.modal-message",
          "Shared traffic is encrypted end-to-end into this workspace, and access is granted per email address.",
        ),
      ],
    );
  },
};
