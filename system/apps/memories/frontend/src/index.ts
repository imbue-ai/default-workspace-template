/**
 * The memories page's root: it connects to the shell that frames it, reads the notes when the window is shown
 * (and when it regains focus, since a chat may have written one meanwhile), and mounts the page. It reads nothing
 * on a timer.
 */

import m from "mithril";
import "./style.css";
import { connectToShell } from "@imbue/workspace-ui/src/app_contract";
import type { ShellHandshake } from "@imbue/workspace-ui/src/app_contract";
import { createContextMenuOpener } from "@imbue/workspace-ui/src/components/contextMenuOpener";
import { installElementContextMenu } from "@imbue/workspace-ui/src/context_menu";
import { deleteNote, getNotesState, refreshNotes, saveNote } from "./models/notes";
import { MemoriesPage } from "./views/MemoriesPage";

export const PAGE_PATH = "/";
export const PAGE_TITLE = "Agent Memory";

function bootstrap(): void {
  let handshake: ShellHandshake | null = null;
  const connection = connectToShell({
    onHandshake: (received) => {
      handshake = received;
      connection.location(PAGE_PATH, PAGE_TITLE);
    },
    onShown: () => void refreshNotes(),
  });
  window.addEventListener("focus", () => {
    connection.focused();
    void refreshNotes();
  });
  installElementContextMenu({ connection, handshake: () => handshake, open: createContextMenuOpener().open });
  void refreshNotes();

  const rootElement = document.getElementById("app");
  if (rootElement === null) return;
  m.mount(rootElement, {
    view: () =>
      m("div", { class: "memories-page h-screen w-full overflow-y-auto bg-page" }, [
        m("div", { class: "mx-auto flex max-w-[760px] flex-col gap-6 px-6 py-6" }, [
          m(MemoriesPage, { state: getNotesState(), onSave: saveNote, onDelete: deleteNote }),
        ]),
      ]),
  });
}

window.addEventListener("load", bootstrap);
