// The page: open a file by `?path=`, edit it in Suggest, Edit or Read mode, save as you go.
import "./styles.css";

import type { Editor } from "@tiptap/core";
import { suggestChangesKey } from "@handlewithcare/prosemirror-suggest-changes";

import { connectToShell } from "@imbue/workspace-ui/src/app_contract";
import type { ShellHandshake } from "@imbue/workspace-ui/src/app_contract";
import { installElementContextMenu } from "@imbue/workspace-ui/src/context_menu";

import * as api from "./api";
import type { FileRecord, FoundFile } from "./api";
import { cleanCommentText } from "./critic";
import { currentText, openText } from "./document";
import type { OpenDocument } from "./document";
import { acceptSuggestion, createEditor, rejectSuggestion, reviewCounts, setMode } from "./editor";
import type { Mode } from "./editor";

const MODE_KEY = "markdown-editor.mode";
const SIGN_KEY = "markdown-editor.sign-as";
const RECENT_KEY = "markdown-editor.recent";
const SAVE_DELAY_MS = 1000;
const POLL_MS = 4000;
const RECENT_LIMIT = 12;

function byId<T extends HTMLElement = HTMLElement>(id: string): T {
  const element = document.getElementById(id);
  if (!element) throw new Error(`missing #${id}`);
  return element as T;
}

const ui = {
  fileName: byId("file-name"),
  fileSize: byId("file-size"),
  modeSwitch: byId("mode-switch"),
  saveStatus: byId("save-status"),
  toggleSource: byId<HTMLButtonElement>("toggle-source"),
  download: byId<HTMLAnchorElement>("download"),
  toolbar: byId("toolbar"),
  review: byId("review"),
  reviewCounts: byId("review-counts"),
  banner: byId("banner"),
  bannerText: byId("banner-text"),
  bannerActions: byId("banner-actions"),
  picker: byId("picker"),
  openForm: byId<HTMLFormElement>("open-form"),
  openPath: byId<HTMLInputElement>("open-path"),
  recentHeading: byId("recent-heading"),
  recentList: byId("recent-list"),
  foundStatus: byId("found-status"),
  foundList: byId("found-list"),
  document: byId("document"),
  frontMatter: byId<HTMLDetailsElement>("front-matter"),
  frontMatterText: byId("front-matter-text"),
  editorHost: byId("editor"),
  sourceView: byId("source-view"),
  loadError: byId("load-error"),
  popover: byId("popover"),
};

interface PageState {
  path: string | null;
  file: FileRecord | null;
  doc: OpenDocument | null;
  baseSha: string | null;
  mode: Mode;
  dirty: boolean;
  saving: boolean;
  conflict: boolean;
  showingSource: boolean;
  /** Bumped on every edit, so a save that finishes knows whether newer edits are waiting. */
  editCount: number;
  saveTimer: number | null;
}

const state: PageState = {
  path: null,
  file: null,
  doc: null,
  baseSha: null,
  mode: "suggest",
  dirty: false,
  saving: false,
  conflict: false,
  showingSource: false,
  editCount: 0,
  saveTimer: null,
};

// Scrolling to a line (find, keyboard, a clicked suggestion) stops below the sticky header, whatever its height.
const chrome = byId("chrome");
new ResizeObserver(() => {
  document.documentElement.style.setProperty("--chrome-height", `${chrome.offsetHeight}px`);
}).observe(chrome);

let editor: Editor = createEditor(ui.editorHost, onEdit);

/** A fresh editor per load, so Undo never reaches back into another file or an older version of this one. */
function freshEditor(): void {
  editor.destroy();
  ui.editorHost.replaceChildren();
  editor = createEditor(ui.editorHost, onEdit);
}

// ---------------------------------------------------------------- shell

// Framed by the workspace shell, the page reports where it is (``shell:location``) and offers the shell's
// right-click menu on every element; on its own the connection simply never hears back.
let handshake: ShellHandshake | null = null;
const shell = connectToShell({
  onHandshake: (received) => {
    handshake = received;
    reportLocation();
  },
});
window.addEventListener("focus", () => shell.focused());
installElementContextMenu({ connection: shell, handshake: () => handshake });

function reportLocation(): void {
  shell.location(location.pathname + location.search, document.title);
}

// ---------------------------------------------------------------- preferences

function preferredMode(): Mode {
  const fromUrl = new URLSearchParams(location.search).get("mode");
  if (fromUrl === "suggest" || fromUrl === "edit" || fromUrl === "read") return fromUrl;
  const saved = localStorage.getItem(MODE_KEY);
  return saved === "edit" || saved === "read" ? saved : "suggest";
}

function recentPaths(): string[] {
  try {
    const value = JSON.parse(localStorage.getItem(RECENT_KEY) ?? "[]");
    return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
  } catch {
    return [];
  }
}

function rememberRecent(path: string): void {
  const paths = [path, ...recentPaths().filter((item) => item !== path)].slice(0, RECENT_LIMIT);
  localStorage.setItem(RECENT_KEY, JSON.stringify(paths));
}

// ---------------------------------------------------------------- status and banner

function setStatus(text: string, kind: "" | "error" = ""): void {
  ui.saveStatus.textContent = text;
  ui.saveStatus.classList.toggle("error", kind === "error");
}

interface BannerAction {
  label: string;
  id: string;
  run: () => void;
}

function showBanner(text: string, actions: BannerAction[] = [], tone: "warn" | "info" = "warn"): void {
  ui.bannerText.textContent = text;
  ui.bannerActions.replaceChildren(
    ...actions.map((action) => {
      const button = document.createElement("button");
      button.type = "button";
      button.id = action.id;
      button.textContent = action.label;
      button.addEventListener("click", action.run);
      return button;
    }),
  );
  ui.banner.dataset["tone"] = tone;
  ui.banner.hidden = false;
}

function hideBanner(): void {
  ui.banner.hidden = true;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

// ---------------------------------------------------------------- loading

function showFile(file: FileRecord, keepScroll = false): void {
  const scroll = window.scrollY;
  state.file = file;
  state.baseSha = file.sha256;
  freshEditor();
  state.doc = openText(editor, file.text);
  state.dirty = false;
  state.conflict = false;
  const { frontMatter } = state.doc.loaded;
  ui.frontMatter.hidden = !frontMatter;
  ui.frontMatterText.textContent = frontMatter.replace(/^---[^\n]*\n/, "").replace(/\n(---|\.\.\.)[ \t]*\n?$/, "");
  ui.fileName.textContent = file.name;
  ui.fileName.title = file.path;
  ui.fileSize.textContent = formatSize(file.size);
  ui.download.href = api.downloadUrl(file.path);
  ui.sourceView.textContent = file.text;
  document.title = file.name;
  applyMode(file.is_writable ? preferredMode() : "read", false);
  setStatus(file.is_writable ? "Saved" : "Read only");
  updateReview();
  if (keepScroll) window.scrollTo(0, scroll);
}

async function loadPath(path: string): Promise<void> {
  state.path = path;
  ui.picker.hidden = true;
  ui.loadError.hidden = true;
  hideBanner();
  try {
    const file = await api.readFile(path);
    state.path = file.path;
    showFile(file);
    for (const element of [ui.document, ui.toolbar, ui.modeSwitch, ui.toggleSource, ui.download])
      element.hidden = false;
    rememberRecent(file.path);
    reportLocation();
  } catch (error) {
    ui.document.hidden = true;
    ui.toolbar.hidden = true;
    ui.loadError.hidden = false;
    ui.loadError.textContent = `Could not open ${path}: ${error instanceof Error ? error.message : error}`;
    document.title = "Markdown Editor";
  }
}

async function reloadFromDisk(): Promise<void> {
  if (!state.path) return;
  const file = await api.readFile(state.path);
  showFile(file, true);
  hideBanner();
}

// ---------------------------------------------------------------- modes

function applyMode(mode: Mode, remember: boolean): void {
  if (state.file && !state.file.is_writable) mode = "read";
  state.mode = mode;
  setMode(editor, mode);
  if (remember) localStorage.setItem(MODE_KEY, mode);
  for (const button of ui.modeSwitch.querySelectorAll<HTMLButtonElement>("button[data-mode]")) {
    const selected = button.dataset["mode"] === mode;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-checked", String(selected));
    button.disabled = !state.file?.is_writable && button.dataset["mode"] !== "read";
  }
  document.body.dataset["mode"] = mode;
  for (const button of ui.toolbar.querySelectorAll<HTMLButtonElement>("button[data-command]")) {
    button.disabled = mode === "read";
  }
  updateReview();
}

ui.modeSwitch.addEventListener("click", (event) => {
  const button = (event.target as HTMLElement).closest<HTMLButtonElement>("button[data-mode]");
  if (button && !button.disabled) applyMode(button.dataset["mode"] as Mode, true);
});

// ---------------------------------------------------------------- saving

function onEdit(): void {
  if (!state.doc) return;
  state.editCount++;
  state.dirty = true;
  setStatus("Unsaved");
  updateReview();
  scheduleSave();
}

function scheduleSave(): void {
  if (state.saveTimer !== null) window.clearTimeout(state.saveTimer);
  state.saveTimer = window.setTimeout(() => {
    state.saveTimer = null;
    void save();
  }, SAVE_DELAY_MS);
}

async function save(force = false): Promise<void> {
  const { doc, path, file } = state;
  if (!doc || !path || !file?.is_writable || state.saving || (state.conflict && !force)) return;
  const text = currentText(editor, doc);
  if (text === doc.savedText && !force) {
    state.dirty = false;
    setStatus("Saved");
    return;
  }
  const editsAtStart = state.editCount;
  state.saving = true;
  setStatus("Saving...");
  try {
    const version = await api.writeFile(path, text, force ? null : state.baseSha);
    state.baseSha = version.sha256;
    doc.savedText = text;
    ui.sourceView.textContent = text;
    state.conflict = false;
    if (state.editCount === editsAtStart) {
      state.dirty = false;
      setStatus("Saved");
    }
  } catch (error) {
    if (error instanceof api.ApiError && error.status === 409) {
      state.conflict = true;
      setStatus("Not saved", "error");
      showBanner("This file was changed outside the editor while you had unsaved edits.", [
        { id: "conflict-load", label: "Load the new version", run: () => void reloadFromDisk() },
        { id: "conflict-keep", label: "Keep mine", run: () => void save(true).then(hideBanner) },
      ]);
    } else {
      setStatus(`Couldn't save: ${error instanceof Error ? error.message : error}`, "error");
    }
  } finally {
    state.saving = false;
  }
  if (state.dirty && state.editCount !== editsAtStart && !state.conflict) scheduleSave();
}

async function poll(): Promise<void> {
  if (!state.path || !state.doc || state.saving || state.conflict || document.visibilityState !== "visible") return;
  try {
    const version = await api.fileVersion(state.path);
    if (version.sha256 === state.baseSha || state.saving) return;
    if (!state.dirty) {
      await reloadFromDisk();
      showBanner("This file was just updated outside the editor; showing the new version.", [], "info");
      window.setTimeout(hideBanner, 4000);
    } else {
      state.conflict = true;
      showBanner("This file was changed outside the editor while you had unsaved edits.", [
        { id: "conflict-load", label: "Load the new version", run: () => void reloadFromDisk() },
        { id: "conflict-keep", label: "Keep mine", run: () => void save(true).then(hideBanner) },
      ]);
    }
  } catch {
    // The server is restarting or the file moved; the next poll tries again.
  }
}

window.setInterval(() => void poll(), POLL_MS);
window.addEventListener("beforeunload", (event) => {
  if (state.dirty) {
    void save();
    event.preventDefault();
  }
});

// ---------------------------------------------------------------- review

function updateReview(): void {
  const counts = reviewCounts(editor);
  const parts: string[] = [];
  if (counts.suggestions) parts.push(`${counts.suggestions} suggestion${counts.suggestions === 1 ? "" : "s"}`);
  if (counts.comments) parts.push(`${counts.comments} comment${counts.comments === 1 ? "" : "s"}`);
  ui.reviewCounts.textContent = parts.join(" · ");
  ui.review.hidden = parts.length === 0;
  const canAct = counts.suggestions > 0 && state.mode !== "read";
  byId<HTMLButtonElement>("accept-all").hidden = !canAct;
  byId<HTMLButtonElement>("reject-all").hidden = !canAct;
}

byId("accept-all").addEventListener("click", () => {
  acceptSuggestion(editor, null);
  closePopover();
});
byId("reject-all").addEventListener("click", () => {
  rejectSuggestion(editor, null);
  closePopover();
});

// ---------------------------------------------------------------- popover

function closePopover(): void {
  ui.popover.hidden = true;
  ui.popover.replaceChildren();
}

function placePopover(left: number, top: number): void {
  ui.popover.hidden = false;
  const width = ui.popover.offsetWidth;
  ui.popover.style.left = `${Math.max(8, Math.min(left, window.innerWidth - width - 8))}px`;
  ui.popover.style.top = `${top + 6}px`;
}

function button(label: string, id: string, run: () => void, kind = ""): HTMLButtonElement {
  const element = document.createElement("button");
  element.type = "button";
  element.textContent = label;
  element.id = id;
  if (kind) element.className = kind;
  element.addEventListener("click", run);
  return element;
}

function paragraph(text: string, className = ""): HTMLParagraphElement {
  const element = document.createElement("p");
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

/** A small form in the popover: one text field, Save and Cancel. */
function askText(options: {
  title: string;
  value: string;
  placeholder: string;
  multiline: boolean;
  withSignature: boolean;
  at: { left: number; bottom: number };
  onSave: (value: string) => void;
}): void {
  closePopover();
  const form = document.createElement("form");
  form.className = "popover-form";
  form.append(paragraph(options.title, "popover-title"));
  const field = options.multiline ? document.createElement("textarea") : document.createElement("input");
  field.id = "popover-field";
  field.value = options.value;
  field.placeholder = options.placeholder;
  form.append(field);
  let signature: HTMLInputElement | null = null;
  if (options.withSignature) {
    const label = document.createElement("label");
    label.className = "signature";
    label.textContent = "Sign as ";
    signature = document.createElement("input");
    signature.id = "popover-signature";
    signature.value = localStorage.getItem(SIGN_KEY) ?? "";
    signature.placeholder = "your name (optional)";
    label.append(signature);
    form.append(label);
  }
  const actions = document.createElement("div");
  actions.className = "popover-actions";
  const saveButton = button("Save", "popover-save", () => {}, "primary");
  saveButton.type = "submit";
  actions.append(
    saveButton,
    button("Cancel", "popover-cancel", () => {
      closePopover();
      editor.commands.focus();
    }),
  );
  form.append(actions);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (signature) localStorage.setItem(SIGN_KEY, signature.value.trim());
    options.onSave(field.value);
    closePopover();
  });
  field.addEventListener("keydown", (event) => {
    const keyboard = event as KeyboardEvent;
    if (keyboard.key === "Enter" && (!options.multiline || keyboard.metaKey || keyboard.ctrlKey)) {
      keyboard.preventDefault();
      form.requestSubmit();
    }
    if (keyboard.key === "Escape") {
      closePopover();
      editor.commands.focus();
    }
  });
  ui.popover.append(form);
  placePopover(options.at.left, options.at.bottom);
  field.focus();
}

function selectionCoords(): { left: number; bottom: number } {
  const { to } = editor.state.selection;
  const coords = editor.view.coordsAtPos(to);
  return { left: coords.left, bottom: coords.bottom };
}

function signed(text: string): string {
  const name = (localStorage.getItem(SIGN_KEY) ?? "").trim();
  const clean = cleanCommentText(text);
  if (!clean) return "";
  return name && !clean.startsWith(`${name}:`) ? `${name}: ${clean}` : clean;
}

function addComment(): void {
  if (state.mode === "read") return;
  const { empty } = editor.state.selection;
  askText({
    title: empty ? "Add a note here" : "Comment on the selected text",
    value: "",
    placeholder: "Your comment",
    multiline: true,
    withSignature: true,
    at: selectionCoords(),
    onSave: (value) => {
      const text = signed(value);
      if (!text) return;
      const chain = editor.chain().focus().setMeta(suggestChangesKey, { skip: true });
      if (empty) chain.insertContent({ type: "commentNote", attrs: { text } }).run();
      else chain.setMark("comment", { text }).run();
    },
  });
}

function editLink(): void {
  if (state.mode === "read") return;
  const current = String(editor.getAttributes("link")["href"] ?? "");
  askText({
    title: "Link to",
    value: current,
    placeholder: "https://...",
    multiline: false,
    withSignature: false,
    at: selectionCoords(),
    onSave: (value) => {
      const href = value.trim();
      const chain = editor.chain().focus().extendMarkRange("link");
      if (href) chain.setLink({ href }).run();
      else chain.unsetLink().run();
    },
  });
}

/** Clicking a suggestion or a comment shows what it is and what can be done with it. */
ui.editorHost.addEventListener("click", (event) => {
  const target = (event.target as HTMLElement).closest<HTMLElement>(
    "ins.suggestion, del.suggestion, span.comment, span.comment-note",
  );
  if (!target) {
    closePopover();
    return;
  }
  const rect = target.getBoundingClientRect();
  const canAct = state.mode !== "read" && !!state.file?.is_writable;
  closePopover();
  const box = document.createElement("div");
  box.className = "popover-box";

  if (target.matches("ins.suggestion, del.suggestion")) {
    let id: number | string | null = null;
    try {
      id = JSON.parse(target.dataset["id"] ?? "null");
    } catch {
      id = target.dataset["id"] ?? null;
    }
    const inserted = target.matches("ins");
    box.append(paragraph(inserted ? "Suggested addition" : "Suggested removal", "popover-title"));
    if (canAct && id !== null) {
      const actions = document.createElement("div");
      actions.className = "popover-actions";
      actions.append(
        button(
          "Accept",
          "suggestion-accept",
          () => {
            acceptSuggestion(editor, id);
            closePopover();
          },
          "primary",
        ),
        button("Reject", "suggestion-reject", () => {
          rejectSuggestion(editor, id);
          closePopover();
        }),
      );
      box.append(actions);
    }
  } else {
    const text = target.dataset["comment"] ?? "";
    const isNote = target.matches("span.comment-note");
    box.append(
      paragraph(isNote ? "Note" : "Comment", "popover-title"),
      paragraph(text || "(no text)", "popover-comment"),
    );
    if (canAct) {
      const position = editor.view.posAtDOM(target, 0);
      const actions = document.createElement("div");
      actions.className = "popover-actions";
      const resolve = () => {
        const chain = editor.chain().setMeta(suggestChangesKey, { skip: true });
        if (isNote) chain.deleteRange({ from: position, to: position + 1 }).run();
        else chain.setTextSelection(position).extendMarkRange("comment").unsetMark("comment").run();
        closePopover();
      };
      const edit = () => {
        askText({
          title: "Edit comment",
          value: text,
          placeholder: "Your comment",
          multiline: true,
          withSignature: false,
          at: { left: rect.left, bottom: rect.bottom },
          onSave: (value) => {
            const clean = cleanCommentText(value);
            const chain = editor.chain().setMeta(suggestChangesKey, { skip: true });
            if (!clean) return;
            if (isNote) chain.setNodeSelection(position).updateAttributes("commentNote", { text: clean }).run();
            else chain.setTextSelection(position).extendMarkRange("comment").setMark("comment", { text: clean }).run();
          },
        });
      };
      actions.append(button("Edit", "comment-edit", edit), button("Resolve", "comment-resolve", resolve, "primary"));
      box.append(actions);
    }
  }
  ui.popover.append(box);
  placePopover(rect.left, rect.bottom);
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !ui.popover.hidden) closePopover();
  const mod = event.metaKey || event.ctrlKey;
  if (mod && event.key.toLowerCase() === "s") {
    event.preventDefault();
    if (state.saveTimer !== null) window.clearTimeout(state.saveTimer);
    void save();
  }
  if (mod && event.altKey && event.code === "KeyM") {
    event.preventDefault();
    addComment();
  }
});

document.addEventListener("mousedown", (event) => {
  if (
    !ui.popover.hidden &&
    !ui.popover.contains(event.target as Node) &&
    !ui.editorHost.contains(event.target as Node)
  ) {
    closePopover();
  }
});

// ---------------------------------------------------------------- toolbar

const COMMANDS: Record<string, () => void> = {
  h1: () => editor.chain().focus().toggleHeading({ level: 1 }).run(),
  h2: () => editor.chain().focus().toggleHeading({ level: 2 }).run(),
  bold: () => editor.chain().focus().toggleBold().run(),
  italic: () => editor.chain().focus().toggleItalic().run(),
  bullet: () => editor.chain().focus().toggleBulletList().run(),
  ordered: () => editor.chain().focus().toggleOrderedList().run(),
  quote: () => editor.chain().focus().toggleBlockquote().run(),
  link: editLink,
  comment: addComment,
  undo: () => editor.chain().focus().undo().run(),
  redo: () => editor.chain().focus().redo().run(),
};

ui.toolbar.addEventListener("mousedown", (event) => {
  // Keep the editor's selection while a toolbar button is pressed.
  if ((event.target as HTMLElement).closest("button")) event.preventDefault();
});
ui.toolbar.addEventListener("click", (event) => {
  const target = (event.target as HTMLElement).closest<HTMLButtonElement>("button[data-command]");
  if (target && !target.disabled) COMMANDS[target.dataset["command"] ?? ""]?.();
});

ui.toggleSource.addEventListener("click", () => {
  state.showingSource = !state.showingSource;
  if (state.showingSource && state.doc) ui.sourceView.textContent = currentText(editor, state.doc);
  ui.sourceView.hidden = !state.showingSource;
  ui.editorHost.hidden = state.showingSource;
  ui.toggleSource.textContent = state.showingSource ? "Document" : "Source";
  ui.toggleSource.classList.toggle("selected", state.showingSource);
});

// ---------------------------------------------------------------- picker

function fileItem(file: { path: string; name: string; detail?: string }): HTMLLIElement {
  const item = document.createElement("li");
  const link = document.createElement("a");
  link.href = `/?path=${encodeURIComponent(file.path)}`;
  link.dataset["path"] = file.path;
  link.textContent = file.name;
  const path = document.createElement("span");
  path.className = "muted file-path";
  path.textContent = file.detail ?? file.path;
  item.append(link, path);
  return item;
}

function openFromPicker(path: string): void {
  const url = `/?path=${encodeURIComponent(path)}`;
  history.pushState({ path }, "", url);
  void loadPath(path);
}

async function showPicker(): Promise<void> {
  ui.picker.hidden = false;
  ui.document.hidden = true;
  ui.toolbar.hidden = true;
  for (const element of [ui.modeSwitch, ui.toggleSource, ui.download]) element.hidden = true;
  ui.fileName.textContent = "Markdown Editor";
  ui.fileSize.textContent = "";
  document.title = "Markdown Editor";
  const recent = recentPaths();
  ui.recentHeading.hidden = recent.length === 0;
  ui.recentList.replaceChildren(...recent.map((path) => fileItem({ path, name: path.split("/").pop() ?? path })));
  try {
    const { files, root } = await api.listFiles();
    ui.foundStatus.textContent = files.length
      ? `Most recently changed first, under ${root}`
      : `None found under ${root}.`;
    ui.foundList.replaceChildren(
      ...files.map((file: FoundFile) =>
        fileItem({
          path: file.path,
          name: file.name,
          detail: `${file.path.replace(`${root}/`, "")} · ${formatSize(file.size)}`,
        }),
      ),
    );
  } catch (error) {
    ui.foundStatus.textContent = `Could not list files: ${error instanceof Error ? error.message : error}`;
  }
}

for (const list of [ui.recentList, ui.foundList]) {
  list.addEventListener("click", (event) => {
    const link = (event.target as HTMLElement).closest<HTMLAnchorElement>("a[data-path]");
    if (!link || event.metaKey || event.ctrlKey) return;
    event.preventDefault();
    openFromPicker(link.dataset["path"] ?? "");
  });
}

ui.openForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const path = ui.openPath.value.trim();
  if (path) openFromPicker(path);
});

byId("open-other").addEventListener("click", (event) => {
  event.preventDefault();
  history.pushState({}, "", "/");
  void route();
});

async function route(): Promise<void> {
  if (state.dirty) await save();
  const path = new URLSearchParams(location.search).get("path");
  if (path) await loadPath(path);
  else {
    state.path = null;
    state.doc = null;
    state.file = null;
    await showPicker();
    reportLocation();
  }
}

window.addEventListener("popstate", () => void route());

void route();
