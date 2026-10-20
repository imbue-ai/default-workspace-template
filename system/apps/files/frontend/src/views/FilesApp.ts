/**
 * A Files window on a folder: the shortcuts and connected folders down the side (a resizable sidebar, floated over the
 * folder on a small screen), the toolbar (where you are, how the folder is laid out, and what can be added to it), and
 * the folder itself. Files dropped anywhere on it go
 * into the folder under the pointer; entries dragged onto a folder move into it.
 */

import m from "mithril";

import { Button } from "@imbue/workspace-ui/src/components/Button";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { alertDotIcon, icon, type IconName } from "@imbue/workspace-ui/src/components/icons";
import { createMenu, type Menu, type MenuRow } from "@imbue/workspace-ui/src/components/menu";
import { SegmentedControl } from "@imbue/workspace-ui/src/components/segmentedControl";
import { Toasts } from "@imbue/workspace-ui/src/components/toasts";

import { formatItemCount, formatSize } from "../models/format";
import { DEFAULT_PREFERENCES } from "../models/preferences";
import type { Shortcut } from "../models/FilesModel";
import {
  describeShare,
  deviceNameOf,
  labelOf,
  shareAt,
  shareBadgeOf,
  type ShareBadge,
  stuckSyncOf,
  targetOf,
  type Connections,
  type Share,
} from "../models/connections";
import type { SortKey, ViewMode } from "../models/preferences";
import { folderPathOf, isWithin, segmentsOf } from "../models/paths";
import { isFileDrag, sourcesFromDrop } from "../models/uploads";
import { DROP_FOLDER_ATTR, ENTRY_DRAG_TYPE, FOLDER_SURFACE_ATTR } from "./entries";
import { menuRowsOf, type FilesController } from "./FilesController";
import { cornerBadge, fileIconSvg } from "./FileIcon";
import { ColumnView, IconView, ListView } from "./FolderViews";
import { iconButton } from "./iconButton";
import { handleKeydown } from "./keyboard";

const SHORTCUT_ICONS: Record<string, IconName> = {
  workspace: "house",
  uploads: "inbox",
};

const VIEW_MODES: readonly { mode: ViewMode; label: string; icon: IconName }[] = [
  { mode: "icons", label: "Icons", icon: "layout-grid" },
  { mode: "list", label: "List", icon: "list" },
  { mode: "columns", label: "Columns", icon: "columns" },
];

const SORT_LABELS: Record<SortKey, string> = {
  name: "Name",
  modified: "Date Modified",
  size: "Size",
  kind: "Kind",
};

// Sidebar

const CONNECTED_INFO =
  "Files and folders shared from your computers. A synced one has a copy here; the rest open live from your computer while it's awake.";
const NOTHING_CONNECTED = "Nothing yet. Share a file or folder from Imbue Studio on your computer.";

interface SidebarRow {
  key: string;
  label: string;
  path: string;
  icon: IconName;
  /** A small glyph in the icon's corner: how a shared item reaches the workspace. */
  iconBadge?: ShareBadge;
  title?: string;
  /** Marks after the label, shown only when there is something to say: read-only, stuck, not answering. */
  badges?: m.Children;
  isFaint?: boolean;
  /** A shared file opens its page and takes no drops; every other row is a folder. */
  isFile?: boolean;
  onOpen?: () => void;
}

function sidebarRow(app: FilesController, row: SidebarRow, isCurrent: boolean): m.Vnode {
  const model = app.model;
  return m(
    "button.files-sidebar-row",
    {
      key: row.key,
      type: "button",
      "data-sidebar-row": row.key,
      [DROP_FOLDER_ATTR]: row.isFile === true ? undefined : row.path,
      "aria-current": isCurrent ? "true" : undefined,
      ...(row.title === undefined ? {} : hoverTooltipAttrs(row.title, "right", { maxWidth: 260 })),
      class: `flex h-7 w-full shrink-0 cursor-pointer items-center gap-2 rounded-md px-2 text-left text-(length:--font-size-row) ${
        model.dropTarget === row.path
          ? "bg-accent-light outline-2 -outline-offset-2 outline-accent"
          : isCurrent
            ? "bg-fill-active text-primary"
            : row.isFaint === true
              ? "text-faint hover:bg-fill-hover hover:text-secondary"
              : "text-secondary hover:bg-fill-hover hover:text-primary"
      }`,
      onclick: () => {
        if (row.onOpen !== undefined) row.onOpen();
        else model.navigate(row.path, { startColumns: true });
        app.isSidebarOpen = false;
      },
    },
    [
      row.iconBadge === undefined
        ? m.trust(icon(row.icon, { size: 16, className: "shrink-0" }))
        : sharedItemIcon(row.label, row.icon === "folder", row.iconBadge),
      m("span.files-sidebar-label", { class: "min-w-0 flex-1 truncate" }, row.label),
      row.badges ?? null,
    ],
  );
}

/** A shared item drawn as the folder views draw it, with their small disc over its corner saying how it reaches the
 *  workspace. */
function sharedItemIcon(name: string, isFolder: boolean, badge: ShareBadge): m.Vnode {
  return m("span.files-share-icon", { "data-share-badge": badge, class: "relative flex shrink-0" }, [
    m.trust(fileIconSvg(name, isFolder, 18)),
    cornerBadge(badge, 18),
  ]);
}

function mark(kind: string, label: string, body: m.Children, extra: string): m.Vnode {
  return m("span.files-share-mark", { "data-mark": kind, "aria-label": label, class: `flex shrink-0 ${extra}` }, body);
}

/** A filled disc with an exclamation: the share needs a look. */
function stuckMark(): m.Vnode {
  return mark("stuck", "Not caught up", m.trust(alertDotIcon(12)), "text-warning");
}

function readOnlyMark(): m.Vnode {
  return mark(
    "read-only",
    "Read-only",
    "Read-only",
    "h-4 items-center rounded-full border border-border px-1 type-tag text-faint",
  );
}

function unreachableMark(): m.Vnode {
  return mark("unreachable", "Not answering", m.trust(icon("laptop-off", { size: 12 })), "text-faint");
}

function shareRow(app: FilesController, connections: Connections, share: Share): SidebarRow {
  const target = targetOf(share);
  const isUnreachable = !share.is_reachable || share.sync_state === "unknown";
  return {
    key: `share:${share.device}:${share.path}`,
    label: labelOf(share),
    path: target,
    icon: share.kind === "file" ? "page" : "folder",
    iconBadge: shareBadgeOf(share),
    title: describeShare(share, deviceNameOf(connections, share.device)),
    isFaint: !share.is_reachable && share.synced_copy === null,
    isFile: share.kind === "file",
    badges: [
      share.access === "read" ? readOnlyMark() : null,
      share.sync_state === "behind" ? stuckMark() : null,
      isUnreachable ? unreachableMark() : null,
    ],
    onOpen: share.kind === "file" ? () => app.openPage(target, "view") : undefined,
  };
}

function sectionHeading(label: string, extra: m.Children = null): m.Vnode {
  return m(
    "div.files-sidebar-heading",
    { class: "flex h-8 shrink-0 items-center gap-1 px-2 pt-3 pb-1 type-section text-faint" },
    [m("span", { class: "min-w-0 flex-1 truncate" }, label), extra],
  );
}

/** The handle on the sidebar's edge: dragging it sets the width, a double-click puts the default back. */
function resizeHandle(app: FilesController): m.Vnode {
  const model = app.model;
  return m("div.files-sidebar-resize", {
    role: "separator",
    "aria-orientation": "vertical",
    "aria-label": "Resize sidebar",
    class: "absolute inset-y-0 -right-1 z-(--z-content) w-2 cursor-col-resize",
    onpointerdown: (event: PointerEvent) => {
      event.preventDefault();
      const handle = event.currentTarget as HTMLElement;
      handle.setPointerCapture(event.pointerId);
      const startX = event.clientX;
      const startWidth = model.preferences.sidebarWidth;
      const widthAt = (moveEvent: PointerEvent): number => startWidth + moveEvent.clientX - startX;
      const onMove = (moveEvent: PointerEvent): void => model.setSidebarWidth(widthAt(moveEvent), false);
      const onUp = (upEvent: PointerEvent): void => {
        handle.removeEventListener("pointermove", onMove);
        handle.removeEventListener("pointerup", onUp);
        model.setSidebarWidth(widthAt(upEvent), true);
      };
      handle.addEventListener("pointermove", onMove);
      handle.addEventListener("pointerup", onUp);
    },
    ondblclick: () => model.setSidebarWidth(DEFAULT_PREFERENCES.sidebarWidth, true),
  });
}

function Sidebar(): m.Component<{ app: FilesController; floating: boolean }> {
  return {
    view({ attrs: { app, floating } }) {
      const model = app.model;
      const connections = model.connections;
      const shortcutRows: SidebarRow[] = model.shortcuts.map((shortcut: Shortcut) => ({
        key: `shortcut:${shortcut.key}`,
        label: shortcut.label,
        path: shortcut.path,
        icon: SHORTCUT_ICONS[shortcut.key] ?? ("folder" as IconName),
      }));
      const shareRows: SidebarRow[] =
        connections === null ? [] : connections.shares.map((share) => shareRow(app, connections, share));
      // The row the folder is under, the deepest one when rows nest.
      const current = [...shortcutRows, ...shareRows]
        .filter((row) => isWithin(model.folder, row.path))
        .sort((a, b) => b.path.length - a.path.length)[0]?.key;
      let connectedBody: m.Children;
      if (shareRows.length > 0) {
        connectedBody = shareRows.map((row) => sidebarRow(app, row, row.key === current));
      } else {
        const empty = model.connectionsError ?? (connections === null ? "Looking…" : NOTHING_CONNECTED);
        connectedBody = m("div.files-connected-empty", { class: "px-2 py-1 type-helper text-faint" }, empty);
      }
      return m(
        "nav.files-sidebar",
        {
          class: `flex shrink-0 flex-col bg-surface ${
            floating ? "fixed inset-y-0 left-0 z-(--z-overlay) max-w-[85vw] shadow-overlay" : "relative"
          }`,
          style: `width:${model.preferences.sidebarWidth}px`,
        },
        [
          m("div", { class: "flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto px-2 pt-1 pb-3" }, [
            sectionHeading("Shortcuts"),
            shortcutRows.map((row) => sidebarRow(app, row, row.key === current)),
            m("div.files-connected", { class: "flex flex-col gap-0.5" }, [
              sectionHeading("Connected", [
                m(
                  "span.files-connected-info",
                  {
                    tabindex: 0,
                    "aria-label": CONNECTED_INFO,
                    class: "flex shrink-0 cursor-help text-faint hover:text-secondary",
                    ...hoverTooltipAttrs(CONNECTED_INFO, "right", { maxWidth: 220 }),
                  },
                  m.trust(icon("info", { size: 13 })),
                ),
              ]),
              connectedBody,
            ]),
          ]),
          resizeHandle(app),
        ],
      );
    },
  };
}

// Toolbar

interface Crumb {
  label: string;
  path: string;
}

/** The path as the bar shows it. Inside a share it starts at the computer's name and the share: above a live one
 *  nothing can be read from here, and above a copy are only the workspace's own folders for it. */
export function crumbsOf(folder: string, connections: Connections | null): Crumb[] {
  const share = shareAt(connections, folder);
  if (share !== undefined && connections !== null) {
    const root = targetOf(share);
    const below = segmentsOf(folder.slice(root.length));
    return [
      { label: deviceNameOf(connections, share.device), path: root },
      { label: labelOf(share), path: root },
      ...below.map((segment, index) => ({ label: segment, path: `${root}${below.slice(0, index + 1).join("/")}/` })),
    ];
  }
  const segments = segmentsOf(folder);
  return [{ label: "Computer", path: "/" }].concat(
    segments.map((segment, index) => ({ label: segment, path: folderPathOf(segments.slice(0, index + 1)) })),
  );
}

function Breadcrumb(): m.Component<{ app: FilesController }> {
  return {
    onupdate({ dom }) {
      dom.scrollLeft = dom.scrollWidth;
    },
    oncreate({ dom }) {
      dom.scrollLeft = dom.scrollWidth;
    },
    view({ attrs: { app } }) {
      const model = app.model;
      const crumbs = crumbsOf(model.folder, model.connections);
      return m(
        "div.files-breadcrumb",
        { class: "flex min-w-0 flex-1 items-center overflow-x-auto [scrollbar-width:none]" },
        crumbs.map((crumb, index) => {
          const isLast = index === crumbs.length - 1;
          return m.fragment({ key: `${index}:${crumb.path}` }, [
            index === 0 ? null : m.trust(icon("chevron-right", { size: 12, className: "shrink-0 text-faint" })),
            m(
              "button.files-crumb",
              {
                type: "button",
                "data-crumb": crumb.path,
                [DROP_FOLDER_ATTR]: crumb.path,
                class: `shrink-0 cursor-pointer rounded-md px-1.5 py-1 whitespace-nowrap text-(length:--font-size-row) ${
                  model.dropTarget === crumb.path
                    ? "bg-accent-light text-primary"
                    : isLast
                      ? "font-semibold text-primary"
                      : "text-secondary hover:bg-fill-hover hover:text-primary"
                }`,
                onclick: () => {
                  if (crumb.path !== model.folder) model.navigate(crumb.path);
                },
              },
              crumb.label,
            ),
          ]);
        }),
      );
    },
  };
}

function Toolbar(): m.Component<{ app: FilesController }> {
  let addMenu: Menu | null = null;
  let optionsMenu: Menu | null = null;
  return {
    oninit() {
      addMenu = createMenu({ placement: "below", align: "end", minWidth: 200, extraClass: "files-add-menu" });
      optionsMenu = createMenu({ placement: "below", align: "end", minWidth: 220, extraClass: "files-options-menu" });
    },
    onremove() {
      addMenu?.dispose();
      optionsMenu?.dispose();
    },
    view({ attrs: { app } }) {
      const model = app.model;
      if (addMenu === null || optionsMenu === null) return null;
      const sort = model.preferences.sort;
      const optionRows: MenuRow[] = [
        {
          kind: "submenu",
          key: "sort",
          label: "Sort by",
          value: `${SORT_LABELS[sort.key]}${sort.descending ? ", descending" : ""}`,
          rows: () => [
            ...(Object.keys(SORT_LABELS) as SortKey[]).map((key) => ({
              kind: "action" as const,
              key: `sort-${key}`,
              label: SORT_LABELS[key],
              trailing: sort.key === key ? m.trust(icon("check", { size: 14 })) : null,
              keepsOpen: true,
              onSelect: () => model.setSort({ key, descending: sort.key === key ? sort.descending : false }),
            })),
            { kind: "divider" as const },
            {
              kind: "check" as const,
              key: "sort-descending",
              label: "Descending",
              isChecked: sort.descending,
              onToggle: () => model.setSort({ ...sort, descending: !sort.descending }),
            },
          ],
        },
        {
          kind: "check",
          key: "show-hidden",
          label: "Show system files",
          isChecked: model.preferences.showHidden,
          onToggle: () => model.toggleHidden(),
        },
      ];
      const modes = VIEW_MODES.filter((view) => !(app.isNarrow && view.mode === "columns"));
      const viewSwitch = m(SegmentedControl<ViewMode>, {
        label: "View",
        segments: modes.map((view) => ({
          value: view.mode,
          label: `View as ${view.label}`,
          icon: view.icon,
          attrs: { "data-view-mode": view.mode },
        })),
        value: app.viewMode,
        onSelect: (mode: ViewMode) => model.setViewMode(mode),
      });
      const add = m("div", { class: "flex shrink-0" }, [
        iconButton("Add", "plus", { "data-action": "add", ...addMenu.triggerAttrs() }, { tooltip: false }),
        addMenu.view(menuRowsOf(app.folderActions(model.folder).reverse())),
      ]);
      const options = m("div", { class: "flex shrink-0" }, [
        iconButton(
          "View options",
          "ellipsis",
          { "data-action": "options", ...optionsMenu.triggerAttrs() },
          { tooltip: false },
        ),
        optionsMenu.view(optionRows),
      ]);
      return m("header.files-toolbar", { class: "flex shrink-0 items-center gap-1 bg-surface px-3 py-1" }, [
        app.isNarrow
          ? iconButton("Show sidebar", "panel-left", {
              "data-action": "sidebar",
              onclick: () => (app.isSidebarOpen = true),
            })
          : null,
        m(Breadcrumb, { app }),
        viewSwitch,
        add,
        options,
      ]);
    },
  };
}

// The folder

function FolderBody(): m.Component<{ app: FilesController }> {
  return {
    view({ attrs: { app } }) {
      const model = app.model;
      const mode = app.viewMode;
      if (mode === "columns") return m(ColumnView, { app });
      const state = model.folderState(model.folder);
      const entries = model.visibleEntries(model.folder);
      const stuck = stuckSyncOf(model.connections, model.folder);
      let empty: m.Children = null;
      if (state === undefined || (state.status === "loading" && entries.length === 0)) {
        empty = m("div", { class: "flex justify-center pt-16" }, m("div.spinner"));
      } else if (state.status === "error" && entries.length === 0) {
        empty = message("Couldn't open this folder", state.error ?? "", () => void model.load(model.folder));
      } else if (!state.exists) {
        empty = message("This folder doesn't exist", "It may have been moved or deleted.");
      } else if (stuck !== null && entries.length === 0) {
        empty = m("div.files-stuck-sync", { role: "status" }, message(stuck.title, stuck.detail));
      } else if (entries.length === 0) {
        empty = message(
          "This folder is empty",
          app.isNarrow ? "Tap + to put something here." : "Drop files here, or use + to upload or create something.",
        );
      }
      return m("div", { class: "min-h-full" }, [
        stuck !== null && entries.length > 0 ? stuckBanner(stuck) : null,
        empty ?? (mode === "icons" ? m(IconView, { app, entries }) : m(ListView, { app, entries })),
      ]);
    },
  };
}

/** A strip above a copy's entries saying it has fallen behind its computer. */
export function stuckBanner(stuck: { title: string; detail: string }): m.Vnode {
  return m(
    "div.files-stuck-sync",
    {
      role: "status",
      class: "mx-3 mt-3 flex items-start gap-2 rounded-lg bg-warning-surface/50 px-3 py-2 type-helper",
    },
    [
      m.trust(alertDotIcon(14, "mt-0.5 shrink-0 text-warning")),
      m("div", [
        m("div", { class: "font-medium text-primary" }, stuck.title),
        m("div", { class: "text-secondary" }, stuck.detail),
      ]),
    ],
  );
}

function message(title: string, detail: string, retry?: () => void): m.Vnode {
  return m("div.files-empty", { class: "flex flex-col items-center gap-1 px-6 pt-20 text-center" }, [
    m("div", { class: "type-label text-secondary" }, title),
    detail === "" ? null : m("div", { class: "max-w-[360px] type-helper text-faint" }, detail),
    retry === undefined ? null : m(Button, { sm: true, extra: "mt-3", onclick: retry }, "Try again"),
  ]);
}

// The uploads

function UploadPanel(): m.Component<{ app: FilesController }> {
  return {
    view({ attrs: { app } }) {
      const uploads = app.model.uploads;
      if (uploads.items.length === 0) return null;
      const { loaded, total } = uploads.progress;
      const busy = uploads.isBusy;
      const failed = uploads.items.filter((item) => item.status === "failed").length;
      const done = uploads.items.filter((item) => item.status === "done").length;
      // The item under way, counted from the first: the ones already settled, then this one.
      const settled = uploads.items.filter((item) => item.status !== "queued" && item.status !== "uploading").length;
      const summary = busy
        ? `Uploading ${Math.min(settled + 1, uploads.items.length)} of ${uploads.items.length}`
        : failed > 0
          ? `${failed} upload${failed === 1 ? "" : "s"} failed`
          : `Uploaded ${formatItemCount(done)}`;
      const percent = total === 0 ? 100 : Math.round((loaded / total) * 100);
      return m(
        "section.files-uploads",
        {
          class:
            "fixed right-4 bottom-10 z-(--z-sticky) flex w-[320px] max-w-[calc(100vw-32px)] flex-col overflow-hidden rounded-xl border border-default bg-surface shadow-overlay",
        },
        [
          m("div", { class: "flex items-center gap-2 px-3 pt-2.5 pb-2" }, [
            m("div", { class: "min-w-0 flex-1" }, [
              m("div.files-uploads-summary", { class: "type-label text-primary" }, summary),
              m("div", { class: "type-helper text-faint" }, `${formatSize(loaded)} of ${formatSize(total)}`),
            ]),
            busy
              ? m(Button, { sm: true, variant: "ghost", onclick: () => uploads.cancelAll() }, "Cancel")
              : m(
                  Button,
                  {
                    sm: true,
                    variant: "ghost",
                    "data-action": "dismiss-uploads",
                    onclick: () => uploads.clearFinished(),
                  },
                  "Done",
                ),
          ]),
          m(
            "div",
            { class: "mx-3 mb-2 h-1 overflow-hidden rounded-full bg-fill-hover" },
            m("div", {
              class: `h-full rounded-full ${failed > 0 && !busy ? "bg-danger" : "bg-accent"}`,
              style: `width:${percent}%`,
            }),
          ),
          m(
            "ul",
            { class: "m-0 max-h-[180px] list-none overflow-y-auto border-t border-subtle p-0 py-1" },
            uploads.items.map((item) =>
              m(
                "li.files-upload",
                { key: item.id, "data-status": item.status, class: "flex items-center gap-2 px-3 py-1 type-helper" },
                [
                  m(
                    "span",
                    { class: "min-w-0 flex-1 truncate text-primary", title: item.error ?? item.name },
                    item.name,
                  ),
                  m(
                    "span",
                    { class: `shrink-0 ${item.status === "failed" ? "text-danger" : "text-faint"}` },
                    item.status === "uploading"
                      ? `${Math.round((item.loaded / Math.max(item.file.size, 1)) * 100)}%`
                      : item.status === "done"
                        ? m.trust(icon("check", { size: 13, className: "text-success" }))
                        : item.status === "failed"
                          ? "Failed"
                          : item.status === "canceled"
                            ? "Canceled"
                            : "Waiting",
                  ),
                ],
              ),
            ),
          ),
        ],
      );
    },
  };
}

// Dragging in

/** The folder a drag over `target` would drop into: a folder under the pointer, else the folder on show. */
function dropFolderAt(app: FilesController, target: EventTarget | null): string {
  const element = target instanceof Element ? target.closest(`[${DROP_FOLDER_ATTR}]`) : null;
  return element?.getAttribute(DROP_FOLDER_ATTR) ?? app.model.folder;
}

function installDropHandling(app: FilesController, root: HTMLElement): () => void {
  const model = app.model;
  const isEntryDrag = (event: DragEvent): boolean =>
    event.dataTransfer !== null && Array.from(event.dataTransfer.types).includes(ENTRY_DRAG_TYPE);
  const setTarget = (folder: string | null): void => {
    if (model.dropTarget === folder) return;
    model.dropTarget = folder;
    m.redraw();
  };
  const onDragOver = (event: DragEvent): void => {
    const fromOutside = isFileDrag(event.dataTransfer);
    if (!fromOutside && !isEntryDrag(event)) return;
    event.preventDefault();
    const folder = dropFolderAt(app, event.target);
    if (event.dataTransfer !== null) event.dataTransfer.dropEffect = fromOutside ? "copy" : "move";
    setTarget(folder);
  };
  const onDragLeave = (event: DragEvent): void => {
    if (event.relatedTarget === null || !root.contains(event.relatedTarget as Node)) {
      setTarget(null);
    }
  };
  const onDrop = (event: DragEvent): void => {
    const transfer = event.dataTransfer;
    if (transfer === null) return;
    const folder = dropFolderAt(app, event.target);
    setTarget(null);
    if (isFileDrag(transfer)) {
      event.preventDefault();
      void sourcesFromDrop(transfer).then((sources) => model.uploadInto(folder, sources));
      return;
    }
    if (isEntryDrag(event)) {
      event.preventDefault();
      const paths = JSON.parse(transfer.getData(ENTRY_DRAG_TYPE)) as string[];
      void model.moveInto(paths, folder);
    }
  };
  const onDragEnd = (): void => setTarget(null);
  root.addEventListener("dragover", onDragOver);
  root.addEventListener("dragleave", onDragLeave);
  root.addEventListener("drop", onDrop);
  root.addEventListener("dragend", onDragEnd);
  return () => {
    root.removeEventListener("dragover", onDragOver);
    root.removeEventListener("dragleave", onDragLeave);
    root.removeEventListener("drop", onDrop);
    root.removeEventListener("dragend", onDragEnd);
  };
}

// The window

export function FilesApp(): m.Component<{ app: FilesController }> {
  let uninstall: (() => void) | null = null;
  let onKeydown: ((event: KeyboardEvent) => void) | null = null;
  return {
    oncreate({ dom, attrs: { app } }) {
      uninstall = installDropHandling(app, dom as HTMLElement);
      onKeydown = (event) => handleKeydown(app, event);
      document.addEventListener("keydown", onKeydown);
    },
    onupdate({ dom, attrs: { app } }) {
      if (!app.revealSelection) return;
      app.revealSelection = false;
      const last = app.model.selection[app.model.selection.length - 1];
      if (last === undefined) return;
      const element = dom.querySelector(`[data-entry-path="${CSS.escape(last)}"]`);
      element?.scrollIntoView({ block: "nearest", inline: "nearest" });
    },
    onremove() {
      uninstall?.();
      if (onKeydown !== null) document.removeEventListener("keydown", onKeydown);
    },
    view({ attrs: { app } }) {
      const model = app.model;
      const fileDropOnFolder = model.dropTarget !== null && model.dropTarget === model.folder;
      return m("div.files-app", { class: "flex h-screen min-h-0 bg-surface text-primary" }, [
        app.isNarrow ? null : m(Sidebar, { app, floating: false }),
        m("div", { class: `flex min-w-0 flex-1 flex-col ${app.isNarrow ? "" : "border-l border-default"}` }, [
          m(Toolbar, { app }),
          m(
            "main.files-folder",
            {
              [FOLDER_SURFACE_ATTR]: model.folder,
              class: `relative min-h-0 flex-1 ${app.viewMode === "columns" ? "overflow-hidden" : "overflow-y-auto"} ${
                fileDropOnFolder ? "bg-accent-light/50 outline-2 -outline-offset-4 outline-accent outline-dashed" : ""
              }`,
              onclick: () => {
                if (model.selection.length > 0) model.clearSelection();
              },
            },
            m(FolderBody, { app }),
          ),
        ]),
        // A small screen keeps the sidebar out of the way and floats it over the folder when asked for.
        app.isNarrow && app.isSidebarOpen
          ? [
              m("div.files-sidebar-scrim", {
                class: "fixed inset-0 z-(--z-overlay) bg-black/20",
                onclick: () => (app.isSidebarOpen = false),
              }),
              m(Sidebar, { app, floating: true }),
            ]
          : null,
        m(UploadPanel, { app }),
        m(Toasts, {
          toasts: app.model.toasts.current(),
          placementClass: "fixed bottom-10",
          onDismiss: (id: number) => app.model.toasts.dismiss(id),
        }),
        app.dialogs.view(),
      ]);
    },
  };
}
