/**
 * The keyboard shortcuts page, served at `/keyboard-shortcuts`: the Keyboard settings section on a page of its own,
 * which Imbue Studio's Workspace settings frames. The page tells Imbue Studio it has loaded, so the settings pane
 * can tell a workspace that serves the page from one too old to (whose shell answers the path with the desktop).
 */

import "../style.css";
import m from "mithril";
import { KEYBOARD_SHORTCUTS_READY, sendToEmbedder } from "@imbue/workspace-ui/src/embed";
import { KeyboardSettingsSection } from "../views/KeyboardSettingsSection";

function start(): void {
  const root = document.getElementById("app");
  if (root === null) return;
  m.mount(root, {
    view: () =>
      m("main", { class: "keyboard-shortcuts-page min-h-screen bg-page px-6 py-5 text-primary" }, [
        m(KeyboardSettingsSection),
      ]),
  });
  sendToEmbedder(KEYBOARD_SHORTCUTS_READY);
}

start();
