/**
 * Everything a Files window knows: the folder on show and the listings it has fetched, what is selected, how
 * entries are laid out and ordered, the search under way, the rename being typed, and the history of folders
 * visited. Views read it and call its verbs; it talks to the server through `dufs` and to its surroundings (the
 * address bar, the shell, the dialogs) through a `FilesHost`.
 */

import { ToastQueue } from "@imbue/workspace-ui/src/models/toasts";

import * as dufs from "./dufs";
import type { Entry, Listing } from "./dufs";
import { kindLabelOf } from "./fileKinds";
import {
  ROOT,
  ancestorsOf,
  childPath,
  invalidNameReason,
  isFolderPath,
  isHiddenName,
  isWithin,
  nameOf,
  parentOf,
  uniqueName,
} from "./paths";
import {
  clampSidebarWidth,
  loadPreferences,
  savePreferences,
  type Preferences,
  type SortOrder,
  type ViewMode,
} from "./preferences";
import { UploadQueue, type UploadSource } from "./uploads";
import { fetchConnections, targetOf, type Connections } from "./connections";

export const WORKSPACE = "/home/user/workspace/";

export interface Shortcut {
  key: string;
  label: string;
  path: string;
}

/** The sidebar's shortcuts, shown when they exist. */
export const SHORTCUTS: readonly Shortcut[] = [
  { key: "workspace", label: "Workspace", path: WORKSPACE },
  { key: "uploads", label: "Uploads", path: `${WORKSPACE}data/uploads/` },
];

export interface FolderState {
  status: "loading" | "ready" | "error";
  /** False for a folder that is not there. */
  exists: boolean;
  entries: Entry[];
  error: string | null;
}

export interface SearchState {
  folder: string;
  query: string;
  status: "searching" | "ready" | "error";
  results: Entry[];
  error: string | null;
}

export interface RenameState {
  path: string;
  draft: string;
  error: string | null;
  isSaving: boolean;
}

/** What to do about uploads whose names are already taken in the folder they go to. */
export type ConflictChoice = "replace" | "keep-both" | "cancel";

export type HistoryMode = "push" | "replace";

export interface FilesHost {
  redraw(): void;
  /** Draw before returning, for a change the next keystroke must already find on the page. */
  redrawNow(): void;
  storage: Storage;
  /** The folder on show changed: put it in the address bar (as history entry `index`) and tell the shell. */
  showLocation(folder: string, mode: HistoryMode, index: number): void;
  /** Open a file on its own page. */
  openFile(path: string): void;
  confirmDelete(entries: readonly Entry[]): Promise<boolean>;
  confirmReplace(names: readonly string[]): Promise<ConflictChoice>;
}

/** How a click changes the selection: to just this, this added or taken away, or the run from the last click. */
export type SelectHow = "only" | "toggle" | "extend";

const SEARCH_DELAY_MS = 250;
const TOAST_MS = 6000;
const REFRESH_DELAY_MS = 300;

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

/** Folders first, then by the chosen key, ties by name. */
export function compareEntries(a: Entry, b: Entry, sort: SortOrder): number {
  if (a.isFolder !== b.isFolder) return a.isFolder ? -1 : 1;
  let order = 0;
  switch (sort.key) {
    case "modified":
      order = a.mtime - b.mtime;
      break;
    case "size":
      order = a.size - b.size;
      break;
    case "kind":
      order = collator.compare(kindLabelOf(a.name, a.isFolder), kindLabelOf(b.name, b.isFolder));
      break;
    case "name":
      order = 0;
      break;
  }
  if (order === 0) order = collator.compare(a.name, b.name);
  return sort.descending ? -order : order;
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

export class FilesModel {
  folder: string;
  readonly folders = new Map<string, FolderState>();
  selection: string[] = [];
  /** Where a shift-click's run starts. */
  anchor: string | null = null;
  preferences: Preferences;
  search: SearchState | null = null;
  rename: RenameState | null = null;
  /** The notes a failed operation leaves at the foot of the window. */
  readonly toasts = new ToastQueue(() => this.host.redraw(), TOAST_MS);
  /** The folder a drag is over. */
  dropTarget: string | null = null;
  /** Where the column view starts. It holds while the folder stays under it, so drilling into a folder that is
   *  also a shortcut keeps the columns already open to its left. */
  private columnRoot: string | null = null;
  /** The shortcuts found on this machine. */
  shortcuts: Shortcut[] = [];
  /** What the user has shared from their computers; null until it has been read. */
  connections: Connections | null = null;
  connectionsError: string | null = null;
  historyIndex = 0;
  historyLength = 1;
  readonly uploads: UploadQueue;

  private readonly loads = new Map<string, AbortController>();
  private readonly refreshTimers = new Map<string, number>();
  private searchTimer: number | null = null;
  private searchRequest: AbortController | null = null;

  constructor(
    private readonly host: FilesHost,
    folder: string,
    initial?: Listing,
  ) {
    this.folder = folder;
    this.preferences = loadPreferences(host.storage);
    this.uploads = new UploadQueue({
      redraw: () => host.redraw(),
      onUploaded: (target) => this.scheduleRefresh(target),
    });
    if (initial !== undefined) {
      this.folders.set(folder, { status: "ready", exists: initial.exists, entries: initial.entries, error: null });
    }
  }

  /** Fetch what the window needs to draw: the folder (unless it came with the page), its columns, the sidebar. */
  start(): void {
    if (!this.folders.has(this.folder)) void this.load(this.folder);
    this.loadColumns();
    void this.findShortcuts();
    void this.loadConnections();
  }

  // Reading

  get viewMode(): ViewMode {
    return this.preferences.viewMode;
  }

  folderState(folder: string): FolderState | undefined {
    return this.folders.get(folder);
  }

  /** A folder's entries as shown: system files filtered unless asked for, in the chosen order. */
  visibleEntries(folder: string): Entry[] {
    return this.arrange(this.folders.get(folder)?.entries ?? []);
  }

  /** What the main view lays out: the search's results while searching, otherwise the folder's entries. */
  shownEntries(): Entry[] {
    return this.search === null ? this.visibleEntries(this.folder) : this.arrange(this.search.results);
  }

  arrange(entries: readonly Entry[]): Entry[] {
    const shown = this.preferences.showHidden ? [...entries] : entries.filter((entry) => !isHiddenName(entry.name));
    return shown.sort((a, b) => compareEntries(a, b, this.preferences.sort));
  }

  /** The folders the column view lays side by side: from where the columns start down to the folder. */
  columnFolders(): string[] {
    const root =
      this.columnRoot !== null && isWithin(this.folder, this.columnRoot) ? this.columnRoot : this.rootFor(this.folder);
    return ancestorsOf(this.folder).filter((folder) => isWithin(folder, root));
  }

  /** Where columns showing `folder` start afresh: the shortcut or connected folder it is under, else the root. */
  private rootFor(folder: string): string {
    return (
      [...SHORTCUTS.map((shortcut) => shortcut.path), ...this.connectedFolderPaths()]
        .filter((candidate) => isWithin(folder, candidate))
        .sort((a, b) => b.length - a.length)[0] ?? ROOT
    );
  }

  /** Where each shared folder is opened: its synced copy, or its live view. */
  connectedFolderPaths(): string[] {
    return (this.connections?.shares ?? []).filter((share) => share.kind !== "file").map(targetOf);
  }

  /** Keep the columns' start while `folder` is under it; start afresh when it is not, or when asked to. */
  private settleColumnRoot(folder: string, startAfresh: boolean): void {
    if (startAfresh || this.columnRoot === null || !isWithin(folder, this.columnRoot)) {
      this.columnRoot = this.rootFor(folder);
    }
  }

  entryFor(path: string): Entry | undefined {
    const found = this.search?.results.find((entry) => entry.path === path);
    if (found !== undefined) return found;
    const parent = parentOf(path);
    if (parent === null) return undefined;
    return this.folders.get(parent)?.entries.find((entry) => entry.path === path);
  }

  selectedEntries(): Entry[] {
    return this.selection.map((path) => this.entryFor(path)).filter((entry): entry is Entry => entry !== undefined);
  }

  isSelected(path: string): boolean {
    return this.selection.includes(path);
  }

  get canGoBack(): boolean {
    return this.historyIndex > 0;
  }

  get canGoForward(): boolean {
    return this.historyIndex < this.historyLength - 1;
  }

  // Moving around

  /** Show `folder`. `startColumns` begins the column view afresh there, as a jump from the sidebar does. */
  navigate(folder: string, options: { select?: string[]; history?: HistoryMode; startColumns?: boolean } = {}): void {
    const target = isFolderPath(folder) ? folder : `${folder}/`;
    const mode = options.history ?? "push";
    this.settleColumnRoot(this.folder, false);
    this.settleColumnRoot(target, options.startColumns === true);
    this.folder = target;
    this.selection = options.select ?? [];
    this.anchor = this.selection[0] ?? null;
    this.rename = null;
    this.clearSearch();
    if (mode === "push") {
      this.historyIndex += 1;
      this.historyLength = this.historyIndex + 1;
    }
    this.host.showLocation(target, mode, this.historyIndex);
    void this.load(target);
    this.loadColumns();
    this.host.redraw();
  }

  /** The browser moved through its history (back, forward) to `folder`. */
  restore(folder: string, index: number): void {
    this.settleColumnRoot(folder, false);
    this.folder = folder;
    this.historyIndex = index;
    this.selection = [];
    this.anchor = null;
    this.rename = null;
    this.clearSearch();
    void this.load(folder);
    this.loadColumns();
    this.host.redraw();
  }

  goUp(): void {
    const parent = parentOf(this.folder);
    if (parent !== null) this.navigate(parent, { select: [this.folder] });
  }

  /** Open an entry: a folder in this window, a file on its own page. */
  open(entry: Entry): void {
    if (entry.isFolder) this.navigate(entry.path);
    else this.host.openFile(entry.path);
  }

  // Selecting

  select(path: string, how: SelectHow, order: readonly Entry[]): void {
    if (this.rename !== null && this.rename.path !== path) this.rename = null;
    if (how === "toggle") {
      this.selection = this.isSelected(path)
        ? this.selection.filter((selected) => selected !== path)
        : [...this.selection, path];
      this.anchor = path;
    } else if (how === "extend" && this.anchor !== null) {
      const paths = order.map((entry) => entry.path);
      const from = paths.indexOf(this.anchor);
      const to = paths.indexOf(path);
      if (from === -1 || to === -1) {
        this.selection = [path];
        this.anchor = path;
      } else {
        this.selection = paths.slice(Math.min(from, to), Math.max(from, to) + 1);
      }
    } else {
      this.selection = [path];
      this.anchor = path;
    }
    this.host.redraw();
  }

  /** A click in the column view: a folder opens as the next column; a file is selected and previewed. */
  selectInColumn(entry: Entry, how: SelectHow): void {
    const parent = parentOf(entry.path) ?? ROOT;
    if (how === "only" && entry.isFolder) {
      this.navigate(entry.path, { select: [entry.path], history: "replace" });
      return;
    }
    if (parent !== this.folder) {
      const order = this.visibleEntries(parent);
      const keep = this.selection.filter((path) => parentOf(path) === parent);
      this.navigate(parent, { select: keep, history: "replace" });
      this.select(entry.path, keep.length === 0 ? "only" : how, order);
      return;
    }
    this.select(entry.path, how, this.visibleEntries(parent));
  }

  selectAll(): void {
    this.selection = this.shownEntries().map((entry) => entry.path);
    this.host.redraw();
  }

  clearSelection(): void {
    this.selection = [];
    this.anchor = null;
    this.host.redraw();
  }

  // How things are shown

  setViewMode(viewMode: ViewMode): void {
    this.updatePreferences({ viewMode });
    if (viewMode === "columns") this.loadColumns();
  }

  setSort(sort: SortOrder): void {
    this.updatePreferences({ sort });
  }

  /** Sort by `key`, or flip the order when it already is. */
  sortBy(key: SortOrder["key"]): void {
    const current = this.preferences.sort;
    this.setSort(current.key === key ? { key, descending: !current.descending } : { key, descending: false });
  }

  /** Resize the sidebar; `persist` false while a drag is under way, true when it lands. */
  setSidebarWidth(width: number, persist: boolean): void {
    const sidebarWidth = clampSidebarWidth(width);
    if (persist) {
      this.updatePreferences({ sidebarWidth });
      return;
    }
    this.preferences = { ...this.preferences, sidebarWidth };
    this.host.redraw();
  }

  toggleHidden(): void {
    const showHidden = !this.preferences.showHidden;
    if (!showHidden) this.selection = this.selection.filter((path) => !isHiddenName(nameOf(path)));
    this.updatePreferences({ showHidden });
  }

  private updatePreferences(change: Partial<Preferences>): void {
    this.preferences = { ...this.preferences, ...change };
    savePreferences(this.host.storage, this.preferences);
    this.host.redraw();
  }

  // Searching

  setSearchQuery(query: string): void {
    if (this.searchTimer !== null) window.clearTimeout(this.searchTimer);
    this.searchRequest?.abort();
    if (query.trim() === "") {
      this.clearSearch();
      this.host.redraw();
      return;
    }
    this.search = {
      folder: this.search?.folder ?? this.folder,
      query,
      status: "searching",
      results: this.search?.results ?? [],
      error: null,
    };
    this.selection = [];
    this.searchTimer = window.setTimeout(() => void this.runSearch(), SEARCH_DELAY_MS);
    this.host.redraw();
  }

  clearSearch(): void {
    if (this.searchTimer !== null) window.clearTimeout(this.searchTimer);
    this.searchTimer = null;
    this.searchRequest?.abort();
    this.searchRequest = null;
    this.search = null;
  }

  private async runSearch(): Promise<void> {
    const search = this.search;
    if (search === null) return;
    const request = new AbortController();
    this.searchRequest = request;
    try {
      const results = await dufs.searchFolder(search.folder, search.query.trim(), request.signal);
      if (this.search !== search) return;
      this.search = { ...search, status: "ready", results, error: null };
    } catch (error) {
      if (isAbort(error) || this.search !== search) return;
      this.search = { ...search, status: "error", error: messageOf(error) };
    }
    this.host.redraw();
  }

  // Changing things

  startRename(path: string): void {
    if (this.entryFor(path) === undefined) return;
    this.selection = [path];
    this.anchor = path;
    this.rename = { path, draft: nameOf(path), error: null, isSaving: false };
    // The name field must be there to catch the first letter typed after F2.
    this.host.redrawNow();
  }

  setRenameDraft(draft: string): void {
    if (this.rename === null) return;
    this.rename = { ...this.rename, draft, error: null };
  }

  cancelRename(): void {
    this.rename = null;
    this.host.redraw();
  }

  async commitRename(): Promise<void> {
    const rename = this.rename;
    if (rename === null || rename.isSaving) return;
    const name = rename.draft.trim();
    const entry = this.entryFor(rename.path);
    const parent = parentOf(rename.path);
    if (name === nameOf(rename.path) || entry === undefined || parent === null) {
      this.cancelRename();
      return;
    }
    const target = childPath(parent, name, entry.isFolder);
    const problem = invalidNameReason(name) ?? (this.isNameTaken(parent, name) ? `"${name}" is already taken.` : null);
    if (problem !== null) {
      this.rename = { ...rename, error: problem };
      this.host.redraw();
      return;
    }
    this.rename = { ...rename, isSaving: true };
    this.host.redraw();
    try {
      if (await dufs.exists(target)) throw new Error(`"${name}" is already taken.`);
      await dufs.movePath(rename.path, target);
    } catch (error) {
      this.rename = { ...rename, isSaving: false, error: messageOf(error) };
      this.host.redraw();
      return;
    }
    this.rename = null;
    this.selection = [target];
    this.anchor = target;
    if (this.search !== null) void this.runSearch();
    if (isWithin(this.folder, rename.path)) {
      // The folder on show, or one above it in the columns, was renamed: follow it.
      this.navigate(`${target}${this.folder.slice(rename.path.length)}`, { select: [target], history: "replace" });
    }
    await this.load(parent);
  }

  async deleteEntries(paths: readonly string[]): Promise<void> {
    const entries = paths.map((path) => this.entryFor(path)).filter((entry): entry is Entry => entry !== undefined);
    if (entries.length === 0 || !(await this.host.confirmDelete(entries))) return;
    const failures: string[] = [];
    for (const entry of entries) {
      try {
        await dufs.deletePath(entry.path);
      } catch (error) {
        failures.push(messageOf(error));
      }
    }
    const deleted = new Set(entries.map((entry) => entry.path));
    this.selection = this.selection.filter((path) => !deleted.has(path));
    const enclosing = entries.find((entry) => isWithin(this.folder, entry.path));
    if (enclosing !== undefined) {
      this.navigate(parentOf(enclosing.path) ?? ROOT, { history: "replace" });
    }
    if (this.search !== null) void this.runSearch();
    await Promise.all([...new Set(entries.map((entry) => parentOf(entry.path) ?? ROOT))].map((f) => this.load(f)));
    if (failures.length > 0) this.toasts.show(failures.join("\n"));
  }

  /** Make an "untitled folder" in the folder on show and start naming it. */
  async createFolder(): Promise<void> {
    await this.createAndName("untitled folder", true);
  }

  /** Make an empty "untitled.txt" in the folder on show and start naming it. */
  async createFile(): Promise<void> {
    await this.createAndName("untitled.txt", false);
  }

  private async createAndName(baseName: string, isFolder: boolean): Promise<void> {
    this.clearSearch();
    const folder = this.folder;
    const name = uniqueName(baseName, (candidate) => this.isNameTaken(folder, candidate));
    const path = childPath(folder, name, isFolder);
    try {
      if (isFolder) await dufs.createFolder(path);
      else await dufs.writeFile(path, "");
    } catch (error) {
      this.toasts.show(messageOf(error));
      return;
    }
    await this.load(folder);
    this.startRename(path);
  }

  /** Move entries into a folder, as a drag onto it asks. One that would land on a name already there stays put. */
  async moveInto(paths: readonly string[], folder: string): Promise<void> {
    const moving = paths.filter((path) => parentOf(path) !== folder && !isWithin(folder, path));
    if (moving.length === 0) return;
    const failures: string[] = [];
    for (const path of moving) {
      const target = childPath(folder, nameOf(path), isFolderPath(path));
      try {
        if (await dufs.exists(target))
          throw new Error(`${nameOf(folder)} already has an item named "${nameOf(path)}".`);
        await dufs.movePath(path, target);
      } catch (error) {
        failures.push(messageOf(error));
      }
    }
    this.selection = this.selection.filter((path) => !moving.includes(path));
    const touched = new Set([folder, ...moving.map((path) => parentOf(path) ?? ROOT)]);
    await Promise.all([...touched].filter((f) => this.folders.has(f)).map((f) => this.load(f)));
    if (failures.length > 0) this.toasts.show(failures.join("\n"));
  }

  /** Send files into a folder, asking first about names that are already taken there. */
  async uploadInto(folder: string, sources: readonly UploadSource[]): Promise<void> {
    if (sources.length === 0) return;
    if (this.folders.get(folder)?.status !== "ready") await this.load(folder);
    const topNames = [...new Set(sources.map((source) => source.relativePath.split("/")[0]))];
    const taken = topNames.filter((name) => this.isNameTaken(folder, name));
    let renames = new Map<string, string>();
    if (taken.length > 0) {
      const choice = await this.host.confirmReplace(taken);
      if (choice === "cancel") return;
      if (choice === "keep-both") renames = this.freshNames(folder, taken, topNames);
    }
    const placed = sources.map((source) => {
      const [top, ...rest] = source.relativePath.split("/");
      const name = renames.get(top) ?? top;
      return { file: source.file, relativePath: [name, ...rest].join("/") };
    });
    this.uploads.add(folder, placed);
  }

  /** New names for `taken`, clear of what the folder holds and of everything else arriving with them. */
  private freshNames(folder: string, taken: readonly string[], arriving: readonly string[]): Map<string, string> {
    const claimed = new Set(arriving);
    const renames = new Map<string, string>();
    for (const name of taken) {
      const fresh = uniqueName(name, (candidate) => claimed.has(candidate) || this.isNameTaken(folder, candidate));
      claimed.add(fresh);
      renames.set(name, fresh);
    }
    return renames;
  }

  private isNameTaken(folder: string, name: string): boolean {
    return (this.folders.get(folder)?.entries ?? []).some((entry) => entry.name === name);
  }

  // Fetching

  /** Fetch a folder's listing. What was there stays on show while the new one comes. */
  async load(folder: string): Promise<void> {
    this.loads.get(folder)?.abort();
    const request = new AbortController();
    this.loads.set(folder, request);
    const previous = this.folders.get(folder);
    if (previous === undefined) {
      this.folders.set(folder, { status: "loading", exists: true, entries: [], error: null });
    }
    try {
      const listing = await dufs.listFolder(folder, request.signal);
      this.folders.set(folder, { status: "ready", exists: listing.exists, entries: listing.entries, error: null });
    } catch (error) {
      if (isAbort(error)) return;
      this.folders.set(folder, {
        status: "error",
        exists: previous?.exists ?? true,
        entries: previous?.entries ?? [],
        error: messageOf(error),
      });
    } finally {
      if (this.loads.get(folder) === request) this.loads.delete(folder);
    }
    this.host.redraw();
  }

  /** Refresh a folder shortly, once however many uploads land in it in the meantime. */
  scheduleRefresh(folder: string): void {
    const pending = this.refreshTimers.get(folder);
    if (pending !== undefined) window.clearTimeout(pending);
    this.refreshTimers.set(
      folder,
      window.setTimeout(() => {
        this.refreshTimers.delete(folder);
        if (this.folders.has(folder)) void this.load(folder);
      }, REFRESH_DELAY_MS),
    );
  }

  /** Fetch every column's folder the column view will draw that has not been fetched. */
  private loadColumns(): void {
    if (this.viewMode !== "columns") return;
    for (const folder of this.columnFolders()) {
      if (!this.folders.has(folder)) void this.load(folder);
    }
  }

  private async findShortcuts(): Promise<void> {
    const found = await Promise.all(
      SHORTCUTS.map(async (shortcut) => {
        try {
          return (await dufs.listFolder(shortcut.path)).exists;
        } catch {
          return false;
        }
      }),
    );
    this.shortcuts = SHORTCUTS.filter((_, index) => found[index]);
    this.host.redraw();
  }

  /** Read what is shared from the user's computers again. */
  async loadConnections(): Promise<void> {
    try {
      this.connections = await fetchConnections();
      this.connectionsError = null;
    } catch (error) {
      this.connectionsError = messageOf(error);
    }
    this.host.redraw();
  }
}
