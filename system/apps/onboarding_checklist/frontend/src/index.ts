/**
 * The Onboarding checklist page's root: it connects to the shell that frames it (reporting ``/`` and its title
 * once greeted; it has one page, so it declares no navigation), keeps the items current (``models/ChecklistItems``),
 * and mounts the page. A click on an entry asks, through the shell or the Imbue Studio chrome above it, for that
 * item's pointer (``models/pointers``); the page names no app.
 */

import m from "mithril";
import "./style.css";
import { connectToShell } from "@imbue/workspace-ui/src/app_contract";
import type { ShellHandshake } from "@imbue/workspace-ui/src/app_contract";
import { createContextMenuOpener } from "@imbue/workspace-ui/src/components/contextMenuOpener";
import { installElementContextMenu } from "@imbue/workspace-ui/src/context_menu";
import {
  EMBEDDER_CAPABILITIES,
  OPEN_SHARE_SETTINGS,
  sendToEmbedder,
  setEmbedderMessageHandler,
} from "@imbue/workspace-ui/src/embed";
import { wearThemeFromMessage } from "@imbue/workspace-ui/src/themes/themeClient";
import { ChecklistReader, ITEMS_READ_INTERVAL_MS, fetchChecklistItems } from "./models/ChecklistItems";
import { showPointer } from "./models/pointers";
import type { PointerChannels } from "./models/pointers";
import { ChecklistPage, PAGE_TITLE } from "./views/ChecklistPage";

const PAGE_PATH = "/";

function bootstrap(): void {
  const rootElement = document.getElementById("app");
  if (rootElement === null) return;
  let handshake: ShellHandshake | null = null;
  // Whether the chrome said it draws coach marks on its own controls: read off the handshake by a page greeted
  // after the chrome spoke, and off the chrome's own word, which the shell passes on, by one already open.
  let showsCoachMarks = false;
  const reader = new ChecklistReader({ fetchItems: fetchChecklistItems, redraw: () => m.redraw() });
  const connection = connectToShell({
    onTheme: (theme, revision, isPreview) => void wearThemeFromMessage({ theme, revision, isPreview }),
    onHandshake: (received) => {
      handshake = received;
      showsCoachMarks = received.showsCoachMarks;
      connection.location(PAGE_PATH, PAGE_TITLE);
      m.redraw();
    },
    onShown: () => reader.noteShownByShell(true),
    onHidden: () => reader.noteShownByShell(false),
  });
  setEmbedderMessageHandler(EMBEDDER_CAPABILITIES, (message) => {
    showsCoachMarks = message.showsCoachMarks === true;
    m.redraw();
  });
  window.addEventListener("focus", () => {
    connection.focused();
    reader.noteFocused();
  });
  document.addEventListener("visibilitychange", () =>
    reader.noteDocumentVisible(document.visibilityState === "visible"),
  );
  setInterval(() => reader.tick(), ITEMS_READ_INTERVAL_MS);
  installElementContextMenu({ connection, handshake: () => handshake, open: createContextMenuOpener().open });
  const channels: PointerChannels = {
    sendMessage: (type, fields) => connection.sendMessage(type, fields),
    showCoachCard: (text, place) => connection.showCoachCard(text, place),
    openShareSettings: (coachMark) => sendToEmbedder(OPEN_SHARE_SETTINGS, { coachMark }),
  };
  m.mount(rootElement, {
    view: () =>
      m(ChecklistPage, {
        items: reader.getItems(),
        showsCoachMarks,
        onPick: (itemId) => showPointer(itemId, channels),
      }),
  });
  void reader.read();
}

window.addEventListener("load", bootstrap);
