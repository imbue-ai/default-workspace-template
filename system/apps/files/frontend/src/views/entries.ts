/**
 * What every view does with an entry the same way: how a click, a double-click, and a drag act on it, the field a
 * rename is typed into, and the menu button a phone-width row carries in place of a right-click.
 */

import m from "mithril";

import { createMenu, type Menu } from "@imbue/workspace-ui/src/components/menu";

import type { Entry } from "../models/server";
import type { SelectHow } from "../models/FilesModel";
import { stemLength } from "../models/paths";
import { menuRowsOf, type FilesController } from "./FilesController";
import { iconButton } from "./iconButton";

/** The drag payload of entries dragged within the app: their paths, as JSON. */
export const ENTRY_DRAG_TYPE = "application/x-files-paths";

/** Marks an element as standing for an entry; the context menu and the keyboard find entries by it. */
export const ENTRY_PATH_ATTR = "data-entry-path";
/** Marks an element a drop lands in: a folder's tile or row, a column, a shortcut. */
export const DROP_FOLDER_ATTR = "data-drop-folder";
/** Marks a folder's background, whose menu is the folder's own verbs. */
export const FOLDER_SURFACE_ATTR = "data-folder-surface";

export function selectHowOf(event: MouseEvent): SelectHow {
  if (event.shiftKey) return "extend";
  if (event.metaKey || event.ctrlKey) return "toggle";
  return "only";
}

export type EntryContext = "icons" | "list" | "column";

/** The attributes that make an element act as `entry` within `order`, the entries laid out beside it. */
export function entryAttrs(app: FilesController, entry: Entry, order: readonly Entry[], context: EntryContext) {
  const model = app.model;
  const isRenaming = model.rename?.path === entry.path;
  return {
    [ENTRY_PATH_ATTR]: entry.path,
    [DROP_FOLDER_ATTR]: entry.isFolder ? entry.path : undefined,
    "aria-selected": model.isSelected(entry.path) ? "true" : "false",
    role: "option",
    draggable: !isRenaming && !app.isNarrow,
    onclick: (event: MouseEvent) => {
      event.stopPropagation();
      if (isRenaming) return;
      if (app.isNarrow && context !== "column") {
        model.open(entry);
        return;
      }
      if (context === "column") model.selectInColumn(entry, selectHowOf(event));
      else model.select(entry.path, selectHowOf(event), order);
    },
    ondblclick: (event: MouseEvent) => {
      event.stopPropagation();
      if (isRenaming || app.isNarrow) return;
      // In the columns a folder is already open as the next column; a double-click opens files.
      if (context === "column" && entry.isFolder) return;
      model.open(entry);
    },
    oncontextmenu: () => {
      if (!model.isSelected(entry.path)) {
        if (context === "column") model.selectInColumn(entry, "only");
        else model.select(entry.path, "only", order);
      }
    },
    ondragstart: (event: DragEvent) => {
      if (!model.isSelected(entry.path)) model.select(entry.path, "only", order);
      const paths = model.selection;
      event.dataTransfer?.setData(ENTRY_DRAG_TYPE, JSON.stringify(paths));
      event.dataTransfer?.setData("text/plain", paths.join("\n"));
      if (event.dataTransfer !== null) event.dataTransfer.effectAllowed = "move";
    },
  };
}

export interface RenameFieldAttrs {
  app: FilesController;
  /** Classes that make the field sit where the name it replaces sat. */
  extraClass: string;
  multiline?: boolean;
}

/** The name, made editable: Enter or a click away keeps it, Escape puts it back. */
export const RenameField: m.Component<RenameFieldAttrs> = {
  oncreate({ dom }) {
    const field = dom.querySelector("input, textarea") as HTMLInputElement | HTMLTextAreaElement | null;
    if (field === null) return;
    field.focus();
    field.setSelectionRange(0, stemLength(field.value));
  },
  view({ attrs: { app, extraClass, multiline = false } }) {
    const rename = app.model.rename;
    if (rename === null) return null;
    const fieldAttrs = {
      class: `files-rename-field block w-full rounded-[4px] border border-accent bg-surface px-1 text-primary outline-none ${extraClass}`,
      value: rename.draft,
      spellcheck: false,
      disabled: rename.isSaving,
      "aria-label": "New name",
      "aria-invalid": rename.error === null ? "false" : "true",
      onclick: (event: MouseEvent) => event.stopPropagation(),
      ondblclick: (event: MouseEvent) => event.stopPropagation(),
      oninput: (event: InputEvent) => app.model.setRenameDraft((event.target as HTMLInputElement).value),
      onkeydown: (event: KeyboardEvent) => {
        event.stopPropagation();
        if (event.key === "Enter") {
          event.preventDefault();
          void app.model.commitRename();
        } else if (event.key === "Escape") {
          event.preventDefault();
          app.model.cancelRename();
        }
      },
      onblur: () => {
        if (app.model.rename !== null && !app.model.rename.isSaving) void app.model.commitRename();
      },
    };
    return m("div.files-rename.relative.w-full", [
      multiline ? m("textarea", { ...fieldAttrs, rows: 2 }) : m("input", fieldAttrs),
      rename.error === null
        ? null
        : m(
            "div.files-rename-error",
            {
              class:
                "absolute top-full left-0 z-(--z-sticky) mt-1 w-max max-w-[240px] rounded-md bg-danger px-2 py-1 text-left type-helper text-on-accent shadow-overlay",
            },
            rename.error,
          ),
    ]);
  },
};

/** The "..." button that opens an entry's menu, for a phone, where there is no right-click to reach it. */
export function EntryMenuButton(): m.Component<{ app: FilesController; entry: Entry }> {
  let menu: Menu | null = null;
  return {
    oninit() {
      menu = createMenu({ placement: "below", align: "end", minWidth: 200, extraClass: "files-entry-menu" });
    },
    onremove() {
      menu?.dispose();
    },
    view({ attrs: { app, entry } }) {
      if (menu === null) return null;
      const entries = app.model.isSelected(entry.path) ? app.model.selectedEntries() : [entry];
      return m("span.files-entry-menu-button.flex.shrink-0", [
        iconButton(
          `More for ${entry.name}`,
          "ellipsis",
          {
            ...menu.triggerAttrs(),
            onclick: (event: MouseEvent) => {
              event.stopPropagation();
              menu?.toggle(event.currentTarget as HTMLElement);
            },
          },
          { tooltip: false },
        ),
        menu.view(menuRowsOf(app.entryActions(entries))),
      ]);
    },
  };
}
