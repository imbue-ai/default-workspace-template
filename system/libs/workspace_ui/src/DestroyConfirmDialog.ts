/**
 * Confirmation dialog for an irreversible verb: deleting an instance off the
 * machine, or removing something that cannot simply be put back.
 */

import m from "mithril";
import { MODAL_MESSAGE_CLASS, Modal } from "./components/Modal";
import { Button } from "./components/Button";

interface DestroyConfirmDialogAttrs {
  agentName: string;
  // Dialog heading. Defaults to "Delete chat".
  title?: string;
  // Extra copy under the main question, for consequences the caller has to
  // spell out.
  details?: string;
  // The question itself, for a verb whose consequences are not the default's
  // "cannot be undone".
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

    return m(
      Modal,
      {
        onDismiss: onCancel,
        title,
        actions: [
          m(Button, { extra: "destroy-dialog-btn-cancel", onclick: onCancel }, "Cancel"),
          m(Button, { variant: "destructive", extra: "destroy-dialog-btn-destroy", onclick: onConfirm }, confirmLabel),
        ],
      },
      [
        m("p", { class: MODAL_MESSAGE_CLASS }, question),
        details === undefined ? null : m("p", { class: MODAL_MESSAGE_CLASS }, details),
      ],
    );
  },
};
