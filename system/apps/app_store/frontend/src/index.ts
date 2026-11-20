/**
 * The App Store page's root: it connects to the shell that frames it (reporting ``/`` and its title once greeted),
 * starts polling the store, requests the template catalog once, and mounts the page. Opening an app goes to the
 * shell as ``shell:open-link`` with the app's URL (the shell opens it as the app's window); installing a template
 * goes as ``shell:start-with-text`` with ``/use-template <url>``, the same adopt flow Getting Started starts.
 */

import m from "mithril";
import "./style.css";
import { connectToShell } from "@imbue/workspace-ui/src/app_contract";
import type { ShellHandshake } from "@imbue/workspace-ui/src/app_contract";
import { createContextMenuOpener } from "@imbue/workspace-ui/src/components/contextMenuOpener";
import { installElementContextMenu } from "@imbue/workspace-ui/src/context_menu";
import { installCursorHidingWhileTyping } from "@imbue/workspace-ui/src/hideCursorWhileTyping";
import { wearThemeFromMessage } from "@imbue/workspace-ui/src/themes/themeClient";
import {
  ensureTemplateCatalogRequested,
  getTemplateCatalogState,
} from "getting-started-frontend/src/models/TemplateCatalog";
import { getStoreState, startStorePolling } from "./models/Store";
import { StorePage } from "./views/StorePage";

export const PAGE_PATH = "/";
export const PAGE_TITLE = "App Store";

function bootstrap(): void {
  let handshake: ShellHandshake | null = null;
  const connection = connectToShell({
    onTheme: (theme, revision, isPreview) => void wearThemeFromMessage({ theme, revision, isPreview }),
    onHandshake: (received) => {
      handshake = received;
      connection.location(PAGE_PATH, PAGE_TITLE);
    },
  });
  window.addEventListener("focus", () => connection.focused());
  installElementContextMenu({ connection, handshake: () => handshake, open: createContextMenuOpener().open });
  installCursorHidingWhileTyping(document);
  startStorePolling();
  ensureTemplateCatalogRequested();
  const rootElement = document.getElementById("app");
  if (rootElement === null) return;
  m.mount(rootElement, {
    view: () =>
      m(StorePage, {
        store: getStoreState(),
        templates: getTemplateCatalogState(),
        onOpenLink: (url) => connection.openLink(url),
        onStartWithText: (text) => connection.startWithText(text),
      }),
  });
}

window.addEventListener("load", bootstrap);
