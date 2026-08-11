/**
 * Confirmation dialog for destroying an agent.
 */

import m from "mithril";

interface DestroyConfirmDialogAttrs {
  agentName: string;
  // Dialog heading. Defaults to "Destroy Agent"; terminal tabs pass
  // "Destroy terminal" so the same dialog serves both.
  title?: string;
  // Extra copy under the main question, for consequences the caller has to
  // spell out. Tab destroys use it to say that the tab leaves every project
  // (unlike closing it, which only affects the project on screen) and, for a
  // chat, that the agent's transcript stays readable afterwards.
  details?: string;
  onConfirm: () => void;
  onCancel: () => void;
}

export const DestroyConfirmDialog: m.Component<DestroyConfirmDialogAttrs> = {
  view(vnode) {
    const { agentName, details, onConfirm, onCancel } = vnode.attrs;
    const title = vnode.attrs.title ?? "Destroy Agent";

    return m(
      "div.destroy-dialog-overlay",
      {
        onclick: (e: Event) => {
          if (e.target === e.currentTarget) onCancel();
        },
      },
      [
        m("div.destroy-dialog", [
          m("h3.destroy-dialog-title", title),
          m("p.destroy-dialog-message", [
            `Are you sure you want to destroy `,
            m("strong", agentName),
            `? This cannot be undone.`,
          ]),
          details === undefined ? null : m("p.destroy-dialog-message", details),
          m("div.destroy-dialog-actions", [
            m("button.destroy-dialog-btn.destroy-dialog-btn-cancel", { onclick: onCancel }, "Cancel"),
            m("button.destroy-dialog-btn.destroy-dialog-btn-destroy", { onclick: onConfirm }, "Destroy"),
          ]),
        ]),
      ],
    );
  },
};
