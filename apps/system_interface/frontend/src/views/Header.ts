/**
 * Top header bar for the workspace UI.
 *
 * Occupies the strip that used to be the desktop app's OS titlebar (the
 * `--minds-titlebar-height` region reserved in `App.ts`). In the hosted web
 * deployment there is no OS titlebar, so this is the app's own chrome and the
 * home for the Settings entry point -- previously the UI had no persistent
 * place to reach mind-global settings from.
 */

import m from "mithril";
import { openSettings } from "../models/Settings";
import { icon } from "./icons";

export const Header: m.Component = {
  view() {
    return m("header.minds-header", [
      m("div.minds-header-brand", "minds"),
      m("div.minds-header-actions", [
        m(
          "button.minds-header-btn",
          { onclick: () => openSettings(), title: "Workspace settings", "aria-label": "Workspace settings" },
          [m.trust(icon("settings", { size: 18 })), m("span.minds-header-btn-label", "Settings")],
        ),
      ]),
    ]);
  },
};
