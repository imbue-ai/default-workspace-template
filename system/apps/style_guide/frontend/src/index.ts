/**
 * The Style Guide page's root. Framed by the workspace shell, it reports where it is (``shell:location``) and
 * installs the element context menu, so any swatch or control on it can be handed to a chat as a reference.
 */

import m from "mithril";
import "./style.css";
import baseCss from "@imbue/workspace-ui/src/base.css?raw";

import { connectToShell } from "@imbue/workspace-ui/src/app_contract";
import type { ShellHandshake } from "@imbue/workspace-ui/src/app_contract";
import { createContextMenuOpener } from "@imbue/workspace-ui/src/components/contextMenuOpener";
import { installElementContextMenu } from "@imbue/workspace-ui/src/context_menu";
import { installCursorHidingWhileTyping } from "@imbue/workspace-ui/src/hideCursorWhileTyping";
import { wearThemeFromMessage } from "@imbue/workspace-ui/src/themes/themeClient";

import { readDesignTokens } from "./models/tokens";
import { StyleGuidePage } from "./views/StyleGuidePage";

const PAGE_TITLE = "Style Guide";

function bootstrap(): void {
  const root = document.getElementById("app");
  if (root === null) return;
  let handshake: ShellHandshake | null = null;
  const connection = connectToShell({
    onHandshake: (received) => (handshake = received),
    // Redraw once the theme's stylesheet has loaded: the catalog reads the values the page wears.
    onTheme: (theme, revision, isPreview) =>
      void wearThemeFromMessage({ theme, revision, isPreview }).then(() => m.redraw()),
  });
  window.addEventListener("focus", () => connection.focused());
  installElementContextMenu({ connection, handshake: () => handshake, open: createContextMenuOpener().open });
  installCursorHidingWhileTyping(document);

  const tokens = readDesignTokens(baseCss);
  document.title = PAGE_TITLE;
  connection.location("/", PAGE_TITLE);
  m.mount(root, { view: () => m(StyleGuidePage, { tokens }) });
}

window.addEventListener("load", bootstrap);
