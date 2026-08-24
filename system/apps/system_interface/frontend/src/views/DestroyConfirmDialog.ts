/**
 * Confirmation dialog for a tab's irreversible verb: deleting a chat agent, a
 * terminal, or a browser session off the machine.
 */

import m from "mithril";
import { backdropDismissAttrs } from "./modalBackdrop";

interface DestroyConfirmDialogAttrs {
  agentName: string;
  // Dialog heading. Defaults to "Delete chat"; terminal tabs pass
  // "Delete terminal" so the same dialog serves both.
  title?: string;
  // Extra copy under the main question, for consequences the caller has to
  // spell out. Tab destroys use it to say that the tab leaves every project
  // (unlike closing it, which only affects the project on screen) and, for a
  // chat, that the agent's transcript stays readable afterwards.
  details?: string;
  // The question itself, for a verb that is not "destroy". An app is only ever
  // unregistered -- the workspace has no way to stop the program behind it, and
  // registering it again is one command -- so neither the destroy wording nor
  // the "cannot be undone" the default carries is true of one.
  question?: m.Children;
  // Label on the confirming button, likewise defaulting to the destroy verb.
  confirmLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
}

export const DestroyConfirmDialog: m.Component<DestroyConfirmDialogAttrs> = {
  view(vnode) {
    const { agentName, details, onConfirm, onCancel } = vnode.attrs;
    const title = vnode.attrs.title ?? "Delete chat";
    const question = vnode.attrs.question ?? [
      `Are you sure you want to delete `,
      m("strong", agentName),
      `? This cannot be undone.`,
    ];
    const confirmLabel = vnode.attrs.confirmLabel ?? "Delete";

    return m("div.destroy-dialog-overlay", backdropDismissAttrs(onCancel), [
      m("div.destroy-dialog", [
        m("h3.destroy-dialog-title", title),
        m("p.destroy-dialog-message", question),
        details === undefined ? null : m("p.destroy-dialog-message", details),
        m("div.destroy-dialog-actions", [
          m("button.destroy-dialog-btn.destroy-dialog-btn-cancel", { onclick: onCancel }, "Cancel"),
          m("button.destroy-dialog-btn.destroy-dialog-btn-destroy", { onclick: onConfirm }, confirmLabel),
        ]),
      ]),
    ]);
  },
};
