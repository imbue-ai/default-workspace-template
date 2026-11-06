/**
 * The Routines pages' root: one document for the list, a routine, a run and a run's whole log, which reads its
 * path. Framed by
 * the workspace shell, it reports where it is (``shell:location``) once each page's data is known, hands the
 * list's "describe a routine" text to the shell as a new chat (``shell:start-with-text``), and follows the
 * link rule for every link it draws (a routine's page, a run's page, a chat's page on the chat app's origin).
 */

import m from "mithril";
import "./style.css";

import { connectToShell } from "@imbue/workspace-ui/src/app_contract";
import type { ShellHandshake } from "@imbue/workspace-ui/src/app_contract";
import { createContextMenuOpener } from "@imbue/workspace-ui/src/components/contextMenuOpener";
import { installElementContextMenu } from "@imbue/workspace-ui/src/context_menu";
import { DestroyConfirmDialog } from "@imbue/workspace-ui/src/DestroyConfirmDialog";
import { installCursorHidingWhileTyping } from "@imbue/workspace-ui/src/hideCursorWhileTyping";

import { App, AppModel } from "./views/App";

/** A removal awaiting the user's answer in the confirm dialog. */
interface PendingRemoval {
  title: string;
  answer: (isConfirmed: boolean) => void;
}

function bootstrap(): void {
  const root = document.getElementById("app");
  if (root === null) return;
  let handshake: ShellHandshake | null = null;
  const connection = connectToShell({ onHandshake: (received) => (handshake = received) });
  window.addEventListener("focus", () => connection.focused());
  installElementContextMenu({ connection, handshake: () => handshake, open: createContextMenuOpener().open });
  installCursorHidingWhileTyping(document);

  let pendingRemoval: PendingRemoval | null = null;
  const model = new AppModel(
    window.location.pathname,
    {
      reportLocation: (path, title) => {
        document.title = title;
        connection.location(path, title);
      },
      startWithText: (text) => connection.startWithText(text),
      confirmRemove: (title) =>
        new Promise<boolean>((resolve) => {
          pendingRemoval = {
            title,
            answer: (isConfirmed) => {
              pendingRemoval = null;
              resolve(isConfirmed);
              m.redraw();
            },
          };
          m.redraw();
        }),
      navigate: (path) => window.location.assign(path),
    },
    () => m.redraw(),
  );
  m.mount(root, {
    view: () => [
      m(App, { model }),
      pendingRemoval === null
        ? null
        : m(DestroyConfirmDialog, {
            agentName: pendingRemoval.title,
            title: "Remove routine",
            question: [`Remove `, m("strong", pendingRemoval.title), `?`],
            details: "Its definition and state go; its runs stay in the ledger and its chat stays in your chat list.",
            confirmLabel: "Remove",
            onConfirm: () => pendingRemoval?.answer(true),
            onCancel: () => pendingRemoval?.answer(false),
          }),
    ],
  });
  void model.load();
}

window.addEventListener("load", bootstrap);
