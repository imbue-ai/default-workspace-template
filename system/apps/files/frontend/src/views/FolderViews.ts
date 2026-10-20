/**
 * The three ways a folder is laid out: icons in a grid, a list with sortable columns, and columns that open to the
 * right as folders are chosen, ending in a preview of the file chosen.
 */

import m from "mithril";

import { alertDotIcon, icon } from "@imbue/workspace-ui/src/components/icons";

import { shareBadgeOf, shareRootAt, stuckSyncOf } from "../models/connections";
import type { Entry } from "../models/server";
import { formatModified, formatSize } from "../models/format";
import { kindLabelOf } from "../models/fileKinds";
import type { FolderState } from "../models/FilesModel";
import type { SortKey } from "../models/preferences";
import { isWithin, parentOf } from "../models/paths";
import { DROP_FOLDER_ATTR, EntryMenuButton, FOLDER_SURFACE_ATTR, RenameField, entryAttrs } from "./entries";
import { FileIcon, type IconBadge } from "./FileIcon";
import type { FilesController } from "./FilesController";
import { PreviewPane } from "./PreviewPane";

interface ViewAttrs {
  app: FilesController;
  entries: readonly Entry[];
}

/** The badge of a shared folder's or file's own entry, so it reads as linked or synced wherever it is listed. */
function shareBadgeFor(app: FilesController, entry: Entry): IconBadge | undefined {
  const share = shareRootAt(app.model.connections, entry.path);
  return share === undefined ? undefined : shareBadgeOf(share);
}

function stateClass(app: FilesController, entry: Entry): string {
  const model = app.model;
  if (model.dropTarget === entry.path) return "bg-accent-light outline-2 -outline-offset-2 outline-accent";
  if (model.isSelected(entry.path)) return "bg-accent-light";
  return "hover:bg-fill-hover";
}

function sizeText(entry: Entry): string {
  if (entry.isFolder) return entry.size === 1 ? "1 item" : `${entry.size} items`;
  return formatSize(entry.size);
}

// Icons

export const IconView: m.Component<ViewAttrs> = {
  view({ attrs: { app, entries } }) {
    return m(
      "div.files-icon-view",
      {
        role: "listbox",
        "aria-multiselectable": "true",
        class: "grid grid-cols-[repeat(auto-fill,minmax(108px,1fr))] content-start gap-1 p-3",
      },
      entries.map((entry) => {
        const isRenaming = app.model.rename?.path === entry.path;
        return m(
          "div.files-tile",
          {
            key: entry.path,
            class: `relative flex cursor-default flex-col items-center gap-1.5 rounded-lg px-1.5 pt-2 pb-1.5 ${stateClass(app, entry)}`,
            title: entry.name,
            ...entryAttrs(app, entry, entries, "icons"),
          },
          [
            m(
              "div",
              { class: "flex h-16 w-16 items-center justify-center" },
              m(FileIcon, { entry, size: 64, thumbnail: true, shareBadge: shareBadgeFor(app, entry) }),
            ),
            isRenaming
              ? m(RenameField, {
                  app,
                  multiline: true,
                  extraClass: "resize-none text-center text-(length:--font-size-row) leading-tight",
                })
              : m(
                  "div.files-name",
                  {
                    class:
                      "line-clamp-2 w-full text-center text-(length:--font-size-row) leading-tight break-words text-primary",
                  },
                  entry.name,
                ),
            app.isNarrow
              ? m("div", { class: "absolute top-0.5 right-0.5" }, m(EntryMenuButton, { app, entry }))
              : null,
          ],
        );
      }),
    );
  },
};

// List

const LIST_COLUMNS = "grid-cols-[minmax(180px,1fr)_minmax(150px,190px)_88px_minmax(110px,160px)]";

const HEADERS: readonly { key: SortKey; label: string; extra: string }[] = [
  { key: "name", label: "Name", extra: "pl-[26px]" },
  { key: "modified", label: "Date Modified", extra: "" },
  { key: "size", label: "Size", extra: "justify-end" },
  { key: "kind", label: "Kind", extra: "" },
];

function ListHeader(): m.Component<{ app: FilesController }> {
  return {
    view({ attrs: { app } }) {
      const sort = app.model.preferences.sort;
      return m(
        "div.files-list-header",
        {
          role: "row",
          class: `sticky top-0 z-(--z-content) mx-2 grid ${LIST_COLUMNS} gap-3 border-b border-subtle bg-surface px-3`,
        },
        HEADERS.map((header) =>
          m(
            "button.files-sort",
            {
              type: "button",
              "data-sort": header.key,
              "aria-sort": sort.key === header.key ? (sort.descending ? "descending" : "ascending") : "none",
              class: `flex h-7 min-w-0 cursor-pointer items-center gap-1 type-helper ${sort.key === header.key ? "text-primary" : "text-secondary hover:text-primary"} ${header.extra}`,
              onclick: () => app.model.sortBy(header.key),
            },
            [
              m("span", { class: "truncate" }, header.label),
              sort.key === header.key
                ? m.trust(icon("chevron-down", { size: 12, className: sort.descending ? "" : "rotate-180" }))
                : null,
            ],
          ),
        ),
      );
    },
  };
}

export const ListView: m.Component<ViewAttrs> = {
  view({ attrs: { app, entries } }) {
    const now = new Date();
    if (app.isNarrow) {
      return m(
        "div.files-list-view",
        { role: "listbox", class: "py-1" },
        entries.map((entry) => {
          const isRenaming = app.model.rename?.path === entry.path;
          return m(
            "div.files-row",
            {
              key: entry.path,
              class: `mx-2 flex h-14 cursor-default items-center gap-3 rounded-lg px-2 ${stateClass(app, entry)}`,
              ...entryAttrs(app, entry, entries, "list"),
            },
            [
              m(FileIcon, { entry, size: 32, shareBadge: shareBadgeFor(app, entry) }),
              m("div", { class: "min-w-0 flex-1" }, [
                isRenaming
                  ? m(RenameField, { app, extraClass: "type-body" })
                  : m("div.files-name", { class: "truncate type-body text-primary" }, entry.name),
                m(
                  "div",
                  { class: "truncate type-helper text-faint" },
                  [formatModified(entry.mtime, now), sizeText(entry)].filter((part) => part !== "").join(" · "),
                ),
              ]),
              m(EntryMenuButton, { app, entry }),
            ],
          );
        }),
      );
    }
    return m("div.files-list-view", { role: "listbox", "aria-multiselectable": "true", class: "pb-2" }, [
      m(ListHeader, { app }),
      m(
        "div",
        { class: "pt-1" },
        entries.map((entry) => {
          const isRenaming = app.model.rename?.path === entry.path;
          return m(
            "div.files-row",
            {
              key: entry.path,
              class: `mx-2 grid ${LIST_COLUMNS} h-7 cursor-default items-center gap-3 rounded-md px-3 text-(length:--font-size-row) ${stateClass(app, entry)}`,
              ...entryAttrs(app, entry, entries, "list"),
            },
            [
              m("div", { class: "flex min-w-0 items-center gap-2" }, [
                m(FileIcon, { entry, size: 18, shareBadge: shareBadgeFor(app, entry) }),
                isRenaming
                  ? m(RenameField, { app, extraClass: "h-6" })
                  : m("span.files-name", { class: "truncate text-primary" }, entry.name),
              ]),
              m("div", { class: "truncate text-secondary" }, formatModified(entry.mtime, now)),
              m(
                "div",
                { class: "truncate text-right text-secondary tabular-nums" },
                entry.isFolder ? "--" : formatSize(entry.size),
              ),
              m("div", { class: "truncate text-secondary" }, kindLabelOf(entry.name, entry.isFolder)),
            ],
          );
        }),
      ),
    ]);
  },
};

// Columns

function columnBody(
  app: FilesController,
  folder: string,
  state: FolderState | undefined,
  entries: readonly Entry[],
): m.Children {
  if (state === undefined || (state.status === "loading" && entries.length === 0)) {
    return m("div", { class: "flex justify-center py-6" }, m("div.spinner"));
  }
  if (state.status === "error" && entries.length === 0) {
    return m("div", { class: "px-3 py-4 type-helper text-danger" }, state.error);
  }
  if (entries.length === 0) {
    const stuck = stuckSyncOf(app.model.connections, folder);
    if (stuck !== null) {
      return m(
        "div.files-stuck-sync",
        { role: "status", class: "flex flex-col gap-1 px-3 py-4 text-center type-helper" },
        [
          m.trust(alertDotIcon(16, "mx-auto text-warning")),
          m("div", { class: "font-medium text-primary" }, stuck.title),
          m("div", { class: "text-secondary" }, stuck.detail),
        ],
      );
    }
    return m("div", { class: "px-3 py-4 text-center type-helper text-faint" }, "Empty");
  }
  const model = app.model;
  return entries.map((entry) => {
    const isRenaming = model.rename?.path === entry.path;
    // A folder the columns run through, rather than the one chosen last.
    const isOnPath = entry.isFolder && isWithin(model.folder, entry.path) && !model.isSelected(entry.path);
    const highlight =
      model.dropTarget === entry.path
        ? "bg-accent-light outline-2 -outline-offset-2 outline-accent"
        : model.isSelected(entry.path)
          ? "bg-accent-light"
          : isOnPath
            ? "bg-fill-active"
            : "hover:bg-fill-hover";
    return m(
      "div.files-row",
      {
        key: entry.path,
        class: `mx-1 flex h-[22px] shrink-0 cursor-default items-center gap-2 rounded-md px-2 text-(length:--font-size-row) ${highlight}`,
        ...entryAttrs(app, entry, entries, "column"),
      },
      [
        m(FileIcon, { entry, size: 18, shareBadge: shareBadgeFor(app, entry) }),
        isRenaming
          ? m(RenameField, { app, extraClass: "h-5" })
          : m("span.files-name", { class: "min-w-0 flex-1 truncate text-primary" }, entry.name),
        entry.isFolder ? m.trust(icon("chevron-right", { size: 14, className: "shrink-0 text-faint" })) : null,
      ],
    );
  });
}

export function ColumnView(): m.Component<{ app: FilesController }> {
  let shownColumns = 0;
  const revealLast = (dom: Element, count: number): void => {
    if (count === shownColumns) return;
    shownColumns = count;
    dom.scrollTo({ left: dom.scrollWidth, behavior: "smooth" });
  };
  return {
    oncreate({ dom, attrs }) {
      revealLast(dom, attrs.app.model.columnFolders().length);
    },
    onupdate({ dom, attrs }) {
      const model = attrs.app.model;
      const selected = model.selectedEntries();
      revealLast(dom, model.columnFolders().length + (selected.length === 1 && !selected[0].isFolder ? 1 : 0));
    },
    view({ attrs: { app } }) {
      const model = app.model;
      const folders = model.columnFolders();
      const selected = model.selectedEntries();
      const previewed = selected.length === 1 && !selected[0].isFolder ? selected[0] : null;
      return m("div.files-column-view", { class: "flex h-full min-h-0 overflow-x-auto" }, [
        ...folders.map((folder) =>
          m(
            "div.files-column",
            {
              key: folder,
              role: "listbox",
              [FOLDER_SURFACE_ATTR]: folder,
              [DROP_FOLDER_ATTR]: folder,
              class: `flex w-[240px] shrink-0 flex-col gap-px overflow-y-auto border-r border-subtle py-1 ${model.dropTarget === folder ? "bg-accent-light" : ""}`,
              onclick: () => {
                if (model.folder !== folder || model.selection.length > 0) {
                  model.navigate(folder, { history: "replace" });
                }
              },
            },
            columnBody(app, folder, model.folderState(folder), model.visibleEntries(folder)),
          ),
        ),
        ...(previewed === null || parentOf(previewed.path) !== model.folder
          ? []
          : [
              m(
                "div.files-column",
                { key: `preview:${previewed.path}`, class: "w-[300px] shrink-0 overflow-y-auto" },
                m(PreviewPane, { app, entry: previewed }),
              ),
            ]),
      ]);
    },
  };
}
