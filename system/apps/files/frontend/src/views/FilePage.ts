/**
 * A file's own page: `?view` shows it (a picture, a player, a PDF, or its text), `?edit` opens its text in an editor
 * that saves back with Cmd/Ctrl+S. It is a window of its own, closed when done with, so the header names the file and
 * carries what can be done to it, with no way off to the folder.
 */

import m from "mithril";

import { Button, buttonClass } from "@imbue/workspace-ui/src/components/Button";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import { createMenu, type Menu } from "@imbue/workspace-ui/src/components/menu";
import { SeeDetails } from "@imbue/workspace-ui/src/components/SeeDetails";
import { Toasts } from "@imbue/workspace-ui/src/components/toasts";
import { ToastQueue } from "@imbue/workspace-ui/src/models/toasts";
import { startDownload } from "@imbue/workspace-ui/src/download";

import { isLiveShared } from "../models/connections";
import * as server from "../models/server";
import {
  askAgentAction,
  goneSentence,
  reportBugAction,
  toastFailures,
  toldFailure,
  type FailureHost,
  type FilesFailure,
} from "../models/failures";
import { renameOnServer } from "../models/FilesModel";
import { isTextLike, previewKindOf } from "../models/fileKinds";
import { formatSize } from "../models/format";
import { matchesKeybinding } from "../models/keybindings";
import { childPath, nameOf, parentOf } from "../models/paths";
import { Dialogs } from "./dialogs";
import { FailureActions } from "./FailureActions";
import { FileIcon } from "./FileIcon";
import { iconButton } from "./iconButton";
import { ZoomableImage } from "./ZoomableImage";

export interface FilePageOptions {
  path: string;
  page: "view" | "edit";
  /** The server's word that the file is small and reads as text. */
  isText: boolean;
}

type TextState =
  { status: "loading" } | { status: "ready"; saved: string } | { status: "error"; failure: FilesFailure };

/** Why the text of the file at `path` could not be loaded: gone since the page was asked for, or anything else in
 *  words. */
function loadFailureOf(error: unknown, path: string): FilesFailure {
  const told = toldFailure(error, {
    whatFailed: "This file couldn't be opened",
    operation: "open",
    tried: `open the file ${path}`,
    item: nameOf(path),
  });
  // The page shows this one file, so it needs no name.
  return told.isGone ? { ...told, message: "This file is no longer there. It may have been moved or deleted." } : told;
}

export class FilePageModel {
  text: TextState = { status: "loading" };
  draft = "";
  isSaving = false;
  readonly toasts = new ToastQueue(() => m.redraw());
  isDeleted = false;
  size: number | null = null;
  readonly dialogs = new Dialogs();

  constructor(
    readonly options: FilePageOptions,
    /** The shell connection a failure's buttons go through. */
    readonly reporter: FailureHost,
  ) {}

  get isDirty(): boolean {
    return this.text.status === "ready" && this.draft !== this.text.saved;
  }

  get showsText(): boolean {
    return previewKindOf(nameOf(this.options.path), this.options.isText) === "text";
  }

  async load(): Promise<void> {
    void fetch(server.urlOf(this.options.path), { method: "HEAD" })
      .then((response) => {
        const length = response.headers.get("content-length");
        this.size = length === null ? null : Number(length);
        m.redraw();
      })
      .catch(() => undefined);
    if (!this.showsText) return;
    await this.loadText();
  }

  /** Read the file's text into the editor; what went wrong takes its place when it cannot be read. */
  async loadText(): Promise<void> {
    this.text = { status: "loading" };
    try {
      const text = await server.readText(this.options.path);
      this.text = { status: "ready", saved: text };
      this.draft = text;
    } catch (error) {
      this.text = { status: "error", failure: loadFailureOf(error, this.options.path) };
    }
    m.redraw();
  }

  async save(): Promise<void> {
    if (!this.isDirty || this.isSaving) return;
    const content = this.draft;
    this.isSaving = true;
    m.redraw();
    try {
      await server.writeFile(this.options.path, content);
      this.text = { status: "ready", saved: content };
      this.size = new Blob([content]).size;
    } catch (error) {
      const path = this.options.path;
      const name = nameOf(path);
      const failure = toldFailure(error, {
        whatFailed: `Couldn't save "${name}"`,
        operation: "item",
        tried: `save ${path}`,
        item: name,
      });
      toastFailures(this.toasts, [failure], this.reporter, () => m.redraw());
    }
    this.isSaving = false;
    m.redraw();
  }

  async rename(): Promise<void> {
    const path = this.options.path;
    const name = await this.dialogs.askName(nameOf(path));
    const parent = parentOf(path);
    if (name === null || parent === null) return;
    const target = childPath(parent, name, false);
    const failure = await renameOnServer(path, target, name);
    if (failure !== null) {
      toastFailures(this.toasts, [failure], this.reporter, () => m.redraw());
      m.redraw();
      return;
    }
    window.location.replace(server.pageUrlOf(target, this.options.page));
  }

  async copyPath(): Promise<void> {
    try {
      await navigator.clipboard.writeText(this.options.path);
    } catch {
      this.toasts.show("Couldn't copy to the clipboard.");
    }
  }

  async delete(): Promise<void> {
    const path = this.options.path;
    const entry: server.Entry = { name: nameOf(path), path, isFolder: false, isSymlink: false, size: 0, mtime: 0 };
    if (!(await this.dialogs.confirmDelete([entry]))) return;
    try {
      await server.deletePath(path);
    } catch (error) {
      const failure = toldFailure(error, {
        whatFailed: `Couldn't delete "${nameOf(path)}"`,
        operation: "item",
        tried: `delete ${path}`,
        item: nameOf(path),
      });
      // Already gone is what was asked for.
      if (!failure.isGone) {
        toastFailures(this.toasts, [failure], this.reporter, () => m.redraw());
        m.redraw();
        return;
      }
    }
    // The window shows this one file, so with it gone there is nothing left to show here but the fact.
    this.isDeleted = true;
    m.redraw();
  }
}

function body(page: FilePageModel): m.Children {
  const { path, page: kind } = page.options;
  const name = nameOf(path);
  const url = server.urlOf(path);
  const preview = previewKindOf(name, page.options.isText);
  const centred = "flex h-full items-center justify-center overflow-auto bg-page p-6";
  if (page.isDeleted) {
    return m("div.files-file-deleted", { class: `${centred} flex-col gap-1 text-center` }, [
      m("div", { class: "type-label text-secondary" }, `"${name}" was deleted`),
      m("div", { class: "type-helper text-faint" }, "You can close this window."),
    ]);
  }
  switch (preview) {
    case "image":
      return m(ZoomableImage, { src: url, alt: name, frameClass: "h-full bg-page" });
    case "video":
      return m("div", { class: centred }, m("video", { src: url, controls: true, class: "max-h-full max-w-full" }));
    case "audio":
      return m("div", { class: `${centred} flex-col gap-6` }, [
        m(FileIcon, { entry: { name, path, isFolder: false, isSymlink: false, size: 0, mtime: 0 }, size: 128 }),
        m("audio", { src: url, controls: true, class: "w-[min(480px,100%)]" }),
      ]);
    case "pdf":
      return m("iframe.files-file-frame", { src: url, title: name, class: "h-full w-full border-0" });
    case "text": {
      const text = page.text;
      if (text.status === "loading") return m("div", { class: centred }, m("div.spinner"));
      if (text.status === "error") return loadFailureView(page, text.failure, centred);
      return m("textarea.files-editor", {
        value: page.draft,
        readOnly: kind === "view",
        spellcheck: false,
        "aria-label": kind === "edit" ? `Editing ${name}` : name,
        class:
          "block h-full w-full resize-none border-0 bg-surface px-6 py-4 font-mono text-(length:--font-size-body) leading-relaxed text-primary outline-none",
        oninput: (event: InputEvent) => {
          page.draft = (event.target as HTMLTextAreaElement).value;
        },
      });
    }
    case "none":
      return m("div", { class: `${centred} flex-col gap-3 text-center` }, [
        m(FileIcon, { entry: { name, path, isFolder: false, isSymlink: false, size: 0, mtime: 0 }, size: 128 }),
        m("div", { class: "type-label text-secondary" }, "No preview available"),
        m("div", { class: "type-helper text-faint" }, "This file is too large to show, or isn't text."),
        m(Button, { sm: true, extra: "mt-2", onclick: () => downloadFile(page, path) }, "Download"),
      ]);
  }
}

/** A file whose text could not be loaded, in place of the editor: the sentence, what the server said behind "See
 *  details", Try again, and, where the failure owes help, "Ask an agent to fix this". */
function loadFailureView(page: FilePageModel, failure: FilesFailure, centred: string): m.Children {
  return m("div.files-load-failure", { class: `${centred} flex-col gap-3 text-center` }, [
    m("div.files-load-failure-message", { class: "type-body text-danger" }, failure.message),
    m(SeeDetails, { details: failure.details }),
    m(FailureActions, {
      host: page.reporter,
      failure,
      leading: m(Button, { sm: true, "data-action": "retry-load", onclick: () => void page.loadText() }, "Try again"),
      isCentred: true,
    }),
  ]);
}

/** Download the file; a download that did not start says so in its own words, which cannot say whether the file is
 *  gone, so the server is asked that. */
function downloadFile(page: FilePageModel, path: string): void {
  startDownload(document, server.downloadUrlOf(path, false), nameOf(path), (note) => {
    void server.isGone(path).then((isGone) => page.toasts.show(isGone ? goneSentence(nameOf(path)) : note));
  });
}

export function FilePage(): m.Component<{ page: FilePageModel }> {
  let menu: Menu | null = null;
  let onKeydown: ((event: KeyboardEvent) => void) | null = null;
  let onBeforeUnload: ((event: BeforeUnloadEvent) => void) | null = null;
  return {
    oninit({ attrs: { page } }) {
      menu = createMenu({ placement: "below", align: "end", minWidth: 200, extraClass: "files-file-menu" });
      onKeydown = (event) => {
        if (matchesKeybinding(event, "save-file")) {
          event.preventDefault();
          void page.save();
        }
      };
      onBeforeUnload = (event) => {
        if (page.isDirty) event.preventDefault();
      };
      document.addEventListener("keydown", onKeydown);
      window.addEventListener("beforeunload", onBeforeUnload);
    },
    onremove() {
      menu?.dispose();
      if (onKeydown !== null) document.removeEventListener("keydown", onKeydown);
      if (onBeforeUnload !== null) window.removeEventListener("beforeunload", onBeforeUnload);
    },
    view({ attrs: { page } }) {
      if (menu === null) return null;
      const { path, page: kind } = page.options;
      const name = nameOf(path);
      // The server calls any small file that decodes as text editable; Edit is offered only on one named as text.
      const canEdit = page.showsText && page.text.status === "ready" && isTextLike(name);
      return m("div.files-file-page", { class: "flex h-screen min-h-0 flex-col bg-surface text-primary" }, [
        m("header.files-toolbar", { class: "flex shrink-0 items-center gap-2 px-3 py-1" }, [
          m("div", { class: "flex min-w-0 flex-1 items-center gap-2" }, [
            m(FileIcon, { entry: { name, path, isFolder: false, isSymlink: false, size: 0, mtime: 0 }, size: 20 }),
            m("span.files-file-name", { class: "truncate text-(length:--font-size-row) font-semibold" }, name),
            page.isDirty ? m("span.files-file-dirty", { class: "type-helper text-faint" }, "Edited") : null,
            page.size === null ? null : m("span", { class: "shrink-0 type-helper text-faint" }, formatSize(page.size)),
          ]),
          kind === "edit"
            ? [
                m(
                  "a",
                  {
                    href: server.pageUrlOf(path, "view"),
                    class: buttonClass("ghost", { sm: true }),
                    "data-action": "done",
                  },
                  "Done",
                ),
                m(
                  Button,
                  {
                    variant: "primary",
                    sm: true,
                    "data-action": "save",
                    disabled: !page.isDirty || page.isSaving,
                    onclick: () => void page.save(),
                  },
                  page.isSaving ? "Saving..." : "Save",
                ),
              ]
            : canEdit
              ? m(
                  "a.edit-file",
                  {
                    href: server.pageUrlOf(path, "edit"),
                    class: buttonClass("ghost", { icon: true, sm: true }),
                    "aria-label": "Edit",
                    "data-action": "edit",
                    ...hoverTooltipAttrs("Edit"),
                  },
                  m.trust(icon("edit", { size: 16 })),
                )
              : null,
          iconButton("Download", "download", { "data-action": "download", onclick: () => downloadFile(page, path) }),
          m("div", { class: "flex shrink-0" }, [
            iconButton("More", "ellipsis", { "data-action": "more", ...menu.triggerAttrs() }),
            menu.view([
              ...(isLiveShared(path)
                ? []
                : [
                    {
                      kind: "action" as const,
                      key: "rename",
                      label: "Rename",
                      icon: "edit" as const,
                      onSelect: () => void page.rename(),
                    },
                  ]),
              {
                kind: "action",
                key: "copy-path",
                label: "Copy Path",
                icon: "copy",
                onSelect: () => void page.copyPath(),
              },
              { kind: "divider" },
              {
                kind: "action",
                key: "delete",
                label: "Delete",
                icon: "trash",
                tone: "danger",
                onSelect: () => void page.delete(),
              },
            ]),
          ]),
        ]),
        m("main", { class: "min-h-0 flex-1" }, body(page)),
        m(Toasts, {
          toasts: page.toasts.current(),
          placementClass: "fixed bottom-6",
          onDismiss: (id: number) => page.toasts.dismiss(id),
          closesPlainToasts: true,
          onReportBug: reportBugAction(page.reporter),
          onAskAgent: askAgentAction(page.reporter),
          onHold: (id: number) => page.toasts.hold(id),
        }),
        page.dialogs.view(),
      ]);
    },
  };
}
