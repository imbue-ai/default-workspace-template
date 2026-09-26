/**
 * The Getting Started page's root: it connects to the shell that frames it (reporting ``/`` and its
 * title once greeted; it has one page, so it declares no navigation and a navigate reloads it),
 * requests the template catalog once, and mounts the page. What a tile or a detail action starts
 * goes to the shell as ``shell:start-with-text``: the shell runs its launcher's primary text action
 * with it, so this page never names the app that takes it.
 */

import m from "mithril";
import "./style.css";
import { connectToShell } from "@imbue/workspace-ui/src/app_contract";
import { ensureTemplateCatalogRequested, getTemplateCatalogState } from "./models/TemplateCatalog";
import { GettingStartedPage } from "./views/GettingStartedPage";

export const PAGE_PATH = "/";
export const PAGE_TITLE = "Getting Started";

function bootstrap(): void {
  const connection = connectToShell({
    onHandshake: () => connection.location(PAGE_PATH, PAGE_TITLE),
  });
  window.addEventListener("focus", () => connection.focused());
  ensureTemplateCatalogRequested();
  const rootElement = document.getElementById("app");
  if (rootElement === null) return;
  m.mount(rootElement, {
    view: () =>
      m(GettingStartedPage, {
        catalog: getTemplateCatalogState(),
        onStartWithText: (text) => connection.startWithText(text),
      }),
  });
}

window.addEventListener("load", bootstrap);
