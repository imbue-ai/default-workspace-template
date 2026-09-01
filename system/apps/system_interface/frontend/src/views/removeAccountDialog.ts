/**
 * The confirmation dialog for removing (signing out of) a provider account, shared by the
 * provider chooser and the account flyouts so the wording and the weight of the decision
 * stay identical everywhere.
 *
 * A layered modal rather than an armed two-step button: the armed "Remove?" morph put its
 * consequences text wherever the list had room -- easy to miss below the rows -- and a
 * destructive decision deserves a screen of its own with an explicit Cancel.
 */

import m from "mithril";
import type { ProviderAccount } from "../models/Providers";
import { DestroyConfirmDialog } from "./DestroyConfirmDialog";

export function removeAccountDialog(
  account: ProviderAccount,
  onConfirm: () => void,
  onCancel: () => void,
): m.Children {
  return m(DestroyConfirmDialog, {
    agentName: account.label,
    title: "Remove account",
    question: ["Remove ", m("strong", account.label), "?"],
    details:
      "New chats can't be started on it. A chat already running may keep going until it " +
      "next restarts, since its harness is already holding the credential.",
    confirmLabel: "Remove",
    onConfirm,
    onCancel,
  });
}
