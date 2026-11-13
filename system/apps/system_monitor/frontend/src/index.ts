/**
 * The System Monitor page's root: one document for the overview and each resource's view, which reads its path.
 * Framed by the workspace shell, it wears the theme the shell names, reports where it is (``shell:location``) once
 * greeted, so the shell reopens a window on the view it was on, and installs the element context menu. A page
 * visited outside the shell runs it harmlessly: nothing arrives, and the menu's Explain and Modify rows grey out.
 */

import m from "mithril";
import "./style.css";

import { connectToShell } from "@imbue/workspace-ui/src/app_contract";
import type { ShellHandshake } from "@imbue/workspace-ui/src/app_contract";
import { createContextMenuOpener } from "@imbue/workspace-ui/src/components/contextMenuOpener";
import { installElementContextMenu } from "@imbue/workspace-ui/src/context_menu";
import { wearThemeFromMessage } from "@imbue/workspace-ui/src/themes/themeClient";

import { MonitorModel, titleOf } from "./models/monitor";
import { App } from "./views/App";

function bootstrap(): void {
  const root = document.getElementById("app");
  if (root === null) return;
  const model = new MonitorModel(window.location.pathname, () => m.redraw());
  document.title = titleOf(model.view);
  let handshake: ShellHandshake | null = null;
  const connection = connectToShell({
    onTheme: (theme, revision, isPreview, workspaceColor) =>
      void wearThemeFromMessage({ theme, revision, isPreview, workspaceColor }),
    onHandshake: (received) => {
      handshake = received;
      connection.location(window.location.pathname + window.location.search, document.title);
    },
  });
  window.addEventListener("focus", () => connection.focused());
  installElementContextMenu({ connection, handshake: () => handshake, open: createContextMenuOpener().open });
  m.mount(root, { view: () => m(App, { model }) });
  model.start();
}

window.addEventListener("load", bootstrap);
