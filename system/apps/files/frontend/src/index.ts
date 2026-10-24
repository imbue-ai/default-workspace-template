/**
 * The Files app's page. The Files server serves this one document for every folder and every file page, with the
 * folder's listing or the file's facts filled in (see models/server.ts); this decides which of the two it is and
 * mounts it.
 *
 * A launch opens at the workspace folder with an optional `?path=<folder>`, which the page honours in place before
 * anything draws. Framed by the workspace shell, the page reports where it is (`shell:location`) on every move, so
 * the window keeps its place and is titled after what it shows.
 */

import m from "mithril";
import "./style.css";

import { connectToShell } from "@imbue/workspace-ui/src/app_contract";
import type { ShellHandshake } from "@imbue/workspace-ui/src/app_contract";
import { createContextMenuOpener } from "@imbue/workspace-ui/src/components/contextMenuOpener";
import { installElementContextMenu } from "@imbue/workspace-ui/src/context_menu";
import type { ContextMenuRow } from "@imbue/workspace-ui/src/context_menu_rows";
import { installCursorHidingWhileTyping } from "@imbue/workspace-ui/src/hideCursorWhileTyping";
import { wearThemeFromMessage } from "@imbue/workspace-ui/src/themes/themeClient";

import { entryOf, pageUrlOf, readPageData } from "./models/server";
import { decodePathname, isFolderPath, titleOf } from "./models/paths";
import { ENTRY_PATH_ATTR, FOLDER_SURFACE_ATTR } from "./views/entries";
import { FilePage, FilePageModel } from "./views/FilePage";
import { FilesApp } from "./views/FilesApp";
import { FilesController, type FilesAction } from "./views/FilesController";

/** A `?path=` that is a rooted path on this origin (one leading slash: `//host` and `/\host` would leave it). */
export function rootedPathParam(search: string): string | null {
  const path = new URLSearchParams(search).get("path");
  if (path === null || !path.startsWith("/") || /^\/[/\\]/.test(path)) return null;
  return path;
}

/** The element menu's rows for a group of actions, dividers between the groups. */
function contextRowsOf(groups: readonly (readonly FilesAction[])[]): ContextMenuRow[] {
  const rows: ContextMenuRow[] = [];
  for (const group of groups) {
    if (group.length === 0) continue;
    if (rows.length > 0) rows.push({ kind: "divider" });
    for (const action of group)
      rows.push({ kind: "action", key: action.key, label: action.label, onSelect: action.run });
  }
  return rows;
}

function bootstrap(): void {
  const root = document.getElementById("app");
  const data = readPageData(document);
  if (root === null || data === null) return;

  const requested = rootedPathParam(window.location.search);
  // A path without its slash is a file's (to its page) or a folder named loosely, which the server lists at `?view`.
  if (requested !== null && !isFolderPath(requested)) {
    window.location.replace(pageUrlOf(requested, "view"));
    return;
  }

  let handshake: ShellHandshake | null = null;
  const connection = connectToShell({
    onTheme: (theme, revision, isPreview) => void wearThemeFromMessage({ theme, revision, isPreview }),
    onHandshake: (received) => (handshake = received),
  });
  window.addEventListener("focus", () => connection.focused());
  const opener = createContextMenuOpener();
  // The pointer hides while text is typed into the editor or a name being changed.
  installCursorHidingWhileTyping(document);

  const path = decodePathname(window.location.pathname);
  if (data.kind === "file") {
    const page = new FilePageModel({
      path,
      page: new URLSearchParams(window.location.search).has("edit") ? "edit" : "view",
      isText: data.is_text,
    });
    document.title = titleOf(path);
    connection.location(window.location.pathname + window.location.search, titleOf(path));
    installElementContextMenu({ connection, handshake: () => handshake, open: opener.open });
    m.mount(root, { view: () => m(FilePage, { page }) });
    void page.load();
    return;
  }

  // A `?path=` folder is fetched rather than taken from the page, which lists the folder the launch landed on.
  const folder = requested ?? (isFolderPath(path) ? path : `${path}/`);
  const initial =
    requested === null ? { exists: true, entries: data.entries.map((record) => entryOf(folder, record)) } : undefined;
  const app = new FilesController(folder, initial, connection);

  installElementContextMenu({
    connection,
    handshake: () => handshake,
    open: opener.open,
    extraRows: (target) => {
      const entryElement = target.element.closest(`[${ENTRY_PATH_ATTR}]`);
      if (entryElement !== null) {
        const entryPath = entryElement.getAttribute(ENTRY_PATH_ATTR) ?? "";
        const entries = app.model.isSelected(entryPath)
          ? app.model.selectedEntries()
          : [app.model.entryFor(entryPath)].filter((entry) => entry !== undefined);
        return contextRowsOf(app.entryActions(entries));
      }
      const surface = target.element.closest(`[${FOLDER_SURFACE_ATTR}]`);
      if (surface !== null)
        return contextRowsOf(app.folderActions(surface.getAttribute(FOLDER_SURFACE_ATTR) ?? folder));
      return [];
    },
  });
  m.mount(root, { view: () => m(FilesApp, { app }) });
  app.model.start();
}

window.addEventListener("load", bootstrap);
