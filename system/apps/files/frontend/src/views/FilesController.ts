/**
 * The Files window's wiring between its model and the world around it: the address bar and the shell that frames
 * the page, the dialogs, the browser's file pickers and downloads, and the verbs every menu offers on entries.
 */

import m from "mithril";

import type { ShellConnection } from "@imbue/workspace-ui/src/app_contract";
import type { ActionRow, MenuRow } from "@imbue/workspace-ui/src/components/menu";
import type { IconName } from "@imbue/workspace-ui/src/components/icons";

import { downloadUrlOf, pageUrlOf, urlOf, type Entry, type Listing } from "../models/dufs";
import { FilesModel, type HistoryMode } from "../models/FilesModel";
import { isTextLike } from "../models/fileKinds";
import { decodePathname, nameOf, titleOf } from "../models/paths";
import { sourcesFromFileList } from "../models/uploads";
import { requestConnection, type DroppedItem, type PendingConnection } from "../models/connections";
import { Dialogs } from "./dialogs";

/** After asking to connect something, how often and how long the list is read again for the approval. */
const CONNECTION_POLL_MS = 3000;
const CONNECTION_POLLS = 40;

/** Below this width the window lays itself out for a phone: one tap opens, and there is no column view. */
export const NARROW_QUERY = "(max-width: 700px)";

/** One verb on entries or a folder, drawn as a row of whichever menu offers it. */
export interface FilesAction {
  key: string;
  label: string;
  icon: IconName;
  tone?: "danger";
  run: () => void;
}

export function menuRowsOf(groups: readonly (readonly FilesAction[])[]): MenuRow[] {
  const rows: MenuRow[] = [];
  for (const group of groups) {
    if (group.length === 0) continue;
    if (rows.length > 0) rows.push({ kind: "divider" });
    for (const action of group) {
      const row: ActionRow = {
        kind: "action",
        key: action.key,
        label: action.label,
        icon: action.icon,
        tone: action.tone === "danger" ? "danger" : "default",
        onSelect: action.run,
      };
      rows.push(row);
    }
  }
  return rows;
}

export class FilesController {
  readonly model: FilesModel;
  readonly dialogs = new Dialogs();
  isNarrow: boolean;
  /** Set when the keyboard moved the selection, so the next draw scrolls it into view. */
  revealSelection = false;
  /** On a small screen, whether the sidebar is floated open over the folder. */
  isSidebarOpen = false;

  constructor(
    folder: string,
    initial: Listing | undefined,
    private readonly connection: ShellConnection,
    private readonly win: Window = window,
  ) {
    const narrowQuery = win.matchMedia(NARROW_QUERY);
    this.isNarrow = narrowQuery.matches;
    narrowQuery.addEventListener("change", (event) => {
      this.isNarrow = event.matches;
      m.redraw();
    });
    this.model = new FilesModel(
      {
        redraw: () => m.redraw(),
        redrawNow: () => m.redraw.sync(),
        storage: win.localStorage,
        showLocation: (target, mode, index) => this.showLocation(target, mode, index),
        openFile: (path) => this.openPage(path, "view"),
        confirmDelete: (entries) => this.dialogs.confirmDelete(entries),
        confirmReplace: (names) => this.dialogs.confirmReplace(names),
      },
      folder,
      initial,
    );
    win.history.replaceState({ index: 0 }, "", win.location.pathname);
    win.addEventListener("popstate", (event) => {
      const index = typeof event.state?.index === "number" ? (event.state.index as number) : 0;
      this.model.restore(decodePathname(win.location.pathname), index);
      this.reportLocation(this.model.folder);
    });
    this.reportLocation(folder);
    // Shares change on the user's computer: read them again whenever the window comes back to the front.
    win.addEventListener("focus", () => void this.model.loadConnections());
  }

  /** The column view needs room for at least a couple of columns. */
  get viewMode(): FilesModel["viewMode"] {
    const mode = this.model.viewMode;
    return mode === "columns" && this.isNarrow ? "list" : mode;
  }

  get isFramed(): boolean {
    return this.connection.isFramed;
  }

  reportLocation(folder: string): void {
    this.win.document.title = titleOf(folder);
    this.connection.location(urlOf(folder), titleOf(folder));
  }

  private showLocation(folder: string, mode: HistoryMode, index: number): void {
    if (mode === "push") this.win.history.pushState({ index }, "", urlOf(folder));
    else this.win.history.replaceState({ index }, "", urlOf(folder));
    this.reportLocation(folder);
  }

  /** A file's page, in a window of its own that is closed when done with; in place only outside the workspace. */
  openPage(path: string, page: "view" | "edit"): void {
    const url = pageUrlOf(path, page);
    if (this.connection.isFramed) this.connection.openPath(url, "focus");
    else this.win.location.assign(url);
  }

  openInNewWindow(folder: string): void {
    this.connection.openPath(urlOf(folder), "new");
  }

  download(entries: readonly Entry[]): void {
    for (const entry of entries) {
      const anchor = this.win.document.createElement("a");
      anchor.href = downloadUrlOf(entry.path, entry.isFolder);
      anchor.download = entry.isFolder ? `${entry.name}.zip` : entry.name;
      this.win.document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
    }
  }

  async copyPaths(paths: readonly string[]): Promise<void> {
    try {
      await this.win.navigator.clipboard.writeText(paths.join("\n"));
    } catch {
      this.model.toasts.show("Couldn't copy to the clipboard.");
    }
  }

  /** Ask the user what to connect, then ask their computer: Imbue Studio shows its sharing dialog there. The list
   *  is read again for a while after, so an approval shows up without a reload. */
  /** Connections asked for and not yet approved, each shown as a waiting row in the sidebar. */
  pending: PendingConnection[] = [];
  /** Whether something from the user's computer is being dragged over the Connected section. */
  isConnectDropping = false;

  /** Ask a computer to share something, from the connect entry or an item dropped on Connected. */
  async connectLocal(dropped: DroppedItem | null = null): Promise<void> {
    const ask = await this.dialogs.askConnection(this.model.connections?.devices ?? [], dropped);
    if (ask === null) return;
    try {
      await requestConnection(ask);
    } catch (error) {
      this.model.toasts.show(error instanceof Error ? error.message : String(error));
      return;
    }
    const name = nameOf(ask.path) || ask.path;
    const waiting: PendingConnection = { key: `${Date.now()}:${ask.path}`, name, isFolder: dropped?.isFolder ?? true };
    this.pending = [...this.pending, waiting];
    this.model.toasts.show("Approve it in Imbue Studio on your computer.");
    void this.watchForApproval(waiting);
  }

  /** Look for the new share for a while, then drop the waiting row whether or not it came. */
  private async watchForApproval(waiting: PendingConnection): Promise<void> {
    const before = this.model.connections?.shares.length ?? 0;
    for (let attempt = 0; attempt < CONNECTION_POLLS; attempt += 1) {
      await new Promise((resolve) => this.win.setTimeout(resolve, CONNECTION_POLL_MS));
      await this.model.loadConnections();
      if ((this.model.connections?.shares.length ?? 0) !== before) break;
    }
    this.pending = this.pending.filter((item) => item !== waiting);
    m.redraw();
  }

  /** Each item dropped on Connected, asked about one after another. */
  async connectDropped(items: readonly DroppedItem[]): Promise<void> {
    for (const item of items) await this.connectLocal(item);
  }

  /** Let the user pick files (or a whole folder) to upload into `folder`. */
  pickUploads(folder: string, wholeFolder: boolean): void {
    const input = this.win.document.createElement("input");
    input.type = "file";
    input.multiple = true;
    if (wholeFolder) input.webkitdirectory = true;
    input.addEventListener("change", () => {
      if (input.files !== null) void this.model.uploadInto(folder, sourcesFromFileList(input.files));
    });
    input.click();
  }

  /** The verbs for the entries a menu was opened on, grouped by the dividers between them. */
  entryActions(entries: readonly Entry[]): FilesAction[][] {
    if (entries.length === 0) return [];
    const model = this.model;
    const single = entries.length === 1 ? entries[0] : null;
    const opening: FilesAction[] = [];
    if (single !== null) {
      opening.push({
        key: "open",
        label: "Open",
        icon: single.isFolder ? "folder" : "file",
        run: () => model.open(single),
      });
      if (single.isFolder && this.connection.isFramed && !this.isNarrow) {
        opening.push({
          key: "open-new-window",
          label: "Open in New Window",
          icon: "external-link",
          run: () => this.openInNewWindow(single.path),
        });
      }
      if (!single.isFolder && isTextLike(single.name)) {
        opening.push({ key: "edit", label: "Edit", icon: "edit", run: () => this.openPage(single.path, "edit") });
      }
    }
    const handling: FilesAction[] = [
      {
        key: "download",
        label: single?.isFolder === true ? "Download as ZIP" : "Download",
        icon: "download",
        run: () => this.download(entries),
      },
    ];
    if (single !== null) {
      handling.push({ key: "rename", label: "Rename", icon: "edit", run: () => model.startRename(single.path) });
    }
    handling.push({
      key: "copy-path",
      label: entries.length === 1 ? "Copy Path" : "Copy Paths",
      icon: "copy",
      run: () => void this.copyPaths(entries.map((entry) => entry.path)),
    });
    const removing: FilesAction[] = [
      {
        key: "delete",
        label: entries.length === 1 ? "Delete" : `Delete ${entries.length} Items`,
        icon: "trash",
        tone: "danger",
        run: () => void model.deleteEntries(entries.map((entry) => entry.path)),
      },
    ];
    return [opening, handling, removing];
  }

  /** The verbs for a folder's own background. */
  folderActions(folder: string): FilesAction[][] {
    const model = this.model;
    const inThisFolder = (run: () => void) => () => {
      if (model.folder !== folder) model.navigate(folder, { history: "replace" });
      run();
    };
    return [
      [
        {
          key: "new-folder",
          label: "New Folder",
          icon: "folder-plus",
          run: inThisFolder(() => void model.createFolder()),
        },
        { key: "new-file", label: "New File", icon: "file-plus", run: inThisFolder(() => void model.createFile()) },
      ],
      [
        { key: "upload-files", label: "Upload Files…", icon: "upload", run: () => this.pickUploads(folder, false) },
        { key: "upload-folder", label: "Upload Folder…", icon: "upload", run: () => this.pickUploads(folder, true) },
      ],
    ];
  }
}
