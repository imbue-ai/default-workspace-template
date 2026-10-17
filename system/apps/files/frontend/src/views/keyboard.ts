/**
 * The keyboard in a Files window, the way Finder and Explorer answer it: arrows move the selection (shift extends
 * it), Enter opens, F2 renames, Delete deletes, Cmd/Ctrl+A selects everything, Cmd/Alt+Up goes up a folder, and
 * typing a name jumps to the first entry it starts.
 */

import type { Entry } from "../models/dufs";
import { parentOf } from "../models/paths";
import type { FilesController } from "./FilesController";

/** How long the letters of a typed name keep adding up before a new name starts. */
const TYPE_AHEAD_MS = 900;

let typedName = "";
let typedAt = 0;

function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || target.closest("input, textarea, select") !== null;
}

/** How many tiles the icon grid fits on a row, read off where the tiles actually sit. */
function tilesPerRow(): number {
  const tiles = Array.from(document.querySelectorAll<HTMLElement>(".files-tile"));
  if (tiles.length === 0) return 1;
  const top = tiles[0].offsetTop;
  const perRow = tiles.findIndex((tile) => tile.offsetTop !== top);
  return perRow === -1 ? tiles.length : perRow;
}

/** The entries the arrows move through, and which of them the selection's end is at. */
function arrowOrder(app: FilesController): { order: Entry[]; at: number } {
  const model = app.model;
  const last = model.selection[model.selection.length - 1];
  let order: Entry[];
  if (app.viewMode === "columns" && model.search === null) {
    const column = last === undefined ? model.folder : (parentOf(last) ?? model.folder);
    order = model.visibleEntries(column);
  } else {
    order = model.shownEntries();
  }
  return { order, at: last === undefined ? -1 : order.findIndex((entry) => entry.path === last) };
}

function moveTo(app: FilesController, entry: Entry | undefined, extend: boolean, order: readonly Entry[]): void {
  if (entry === undefined) return;
  app.revealSelection = true;
  if (app.viewMode === "columns" && app.model.search === null)
    app.model.selectInColumn(entry, extend ? "extend" : "only");
  else app.model.select(entry.path, extend ? "extend" : "only", order);
}

function moveBy(app: FilesController, step: number, extend: boolean): void {
  const { order, at } = arrowOrder(app);
  if (order.length === 0) return;
  const next = at === -1 ? (step > 0 ? 0 : order.length - 1) : Math.min(order.length - 1, Math.max(0, at + step));
  moveTo(app, order[next], extend, order);
}

/** Right in the columns: into the chosen folder's first entry. */
function enterColumn(app: FilesController): void {
  const model = app.model;
  const selected = model.selectedEntries();
  if (selected.length !== 1 || !selected[0].isFolder) return;
  const children = model.visibleEntries(selected[0].path);
  moveTo(app, children[0], false, children);
}

/** Left in the columns: back to the folder the chosen entry is in. */
function leaveColumn(app: FilesController): void {
  const model = app.model;
  const last = model.selection[model.selection.length - 1];
  const parent = last === undefined ? model.folder : parentOf(last);
  if (parent === null || !model.columnFolders().slice(1).includes(parent)) return;
  const entry = model.entryFor(parent);
  if (entry !== undefined) moveTo(app, entry, false, model.visibleEntries(parentOf(parent) ?? "/"));
}

function typeAhead(app: FilesController, key: string): void {
  const now = Date.now();
  typedName = now - typedAt > TYPE_AHEAD_MS ? key : typedName + key;
  typedAt = now;
  const { order } = arrowOrder(app);
  const wanted = typedName.toLowerCase();
  const match = order.find((entry) => entry.name.toLowerCase().startsWith(wanted));
  moveTo(app, match, false, order);
}

export function handleKeydown(app: FilesController, event: KeyboardEvent): void {
  if (event.defaultPrevented || isTyping(event.target) || app.dialogs.current !== null) return;
  if (document.querySelector('[data-menu-part="menu"]') !== null) return;
  const model = app.model;
  const command = event.metaKey || event.ctrlKey;
  const handled = (): void => event.preventDefault();

  if (command && event.key.toLowerCase() === "a") {
    handled();
    model.selectAll();
    return;
  }
  if ((command || event.altKey) && event.key === "ArrowUp") {
    handled();
    model.goUp();
    return;
  }
  if ((command && event.key === "[") || (event.altKey && event.key === "ArrowLeft")) {
    handled();
    if (model.canGoBack) window.history.back();
    return;
  }
  if ((command && event.key === "]") || (event.altKey && event.key === "ArrowRight")) {
    handled();
    if (model.canGoForward) window.history.forward();
    return;
  }
  if (command && event.shiftKey && event.key.toLowerCase() === "n") {
    handled();
    void model.createFolder();
    return;
  }
  if (
    (command && event.key === "ArrowDown") ||
    (command && event.key.toLowerCase() === "o") ||
    event.key === "Enter"
  ) {
    const selected = model.selectedEntries();
    if (selected.length !== 1) return;
    handled();
    model.open(selected[0]);
    return;
  }
  if (event.key === "F2") {
    const selected = model.selection;
    if (selected.length !== 1) return;
    handled();
    model.startRename(selected[0]);
    return;
  }
  if (event.key === "Delete" || (command && event.key === "Backspace")) {
    if (model.selection.length === 0) return;
    handled();
    void model.deleteEntries(model.selection);
    return;
  }
  if (event.key === "Escape") {
    if (model.search !== null) model.setSearchQuery("");
    else model.clearSelection();
    return;
  }

  const columns = app.viewMode === "columns" && model.search === null;
  const perRow = app.viewMode === "icons" ? tilesPerRow() : 1;
  switch (event.key) {
    case "ArrowDown":
      handled();
      moveBy(app, columns ? 1 : perRow, event.shiftKey);
      return;
    case "ArrowUp":
      handled();
      moveBy(app, columns ? -1 : -perRow, event.shiftKey);
      return;
    case "ArrowRight":
      if (columns) {
        handled();
        enterColumn(app);
      } else if (app.viewMode === "icons") {
        handled();
        moveBy(app, 1, event.shiftKey);
      }
      return;
    case "ArrowLeft":
      if (columns) {
        handled();
        leaveColumn(app);
      } else if (app.viewMode === "icons") {
        handled();
        moveBy(app, -1, event.shiftKey);
      }
      return;
    default:
      break;
  }
  if (!command && !event.altKey && event.key.length === 1 && event.key !== " ") {
    handled();
    typeAhead(app, event.key);
  }
}
