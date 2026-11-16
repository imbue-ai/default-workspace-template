/**
 * A markdown file's view page: the file formatted, in Suggest mode by default, where what the person types and
 * deletes becomes tracked suggestions and any selection can be commented on; Edit changes the file directly and Read
 * changes nothing. Suggestions and comments are saved inside the file as CriticMarkup (`critic.ts`), so the agent that
 * reads it next sees them in place, and blocks the person did not touch are written back byte for byte
 * (`document.ts`).
 *
 * Saving is automatic, a second after the last edit, through the Files server's PUT with `If-Match`: a save based on a
 * version the file has since left is refused (412), and the page offers to load the new version or keep its own.
 * While nothing is unsaved it checks the file's version every few seconds and shows an agent's rewrite as it lands.
 *
 * The page draws its own DOM inside the host the file page gives it, and tells the file page when what the header
 * shows (whether anything is unsaved) changes.
 */

import type { Editor } from "@tiptap/core";
import { suggestChangesKey } from "@handlewithcare/prosemirror-suggest-changes";

import { buttonClass } from "@imbue/workspace-ui/src/components/Button";

import { urlOf } from "../models/server";
import "./markdown.css";
import { cleanCommentText } from "./critic";
import { currentText, openText } from "./document";
import type { OpenDocument } from "./document";
import { acceptSuggestion, createEditor, rejectSuggestion, reviewCounts, setMode } from "./editor";
import type { Mode } from "./editor";

const MODE_KEY = "files.markdown.mode";
const SIGN_KEY = "files.markdown.sign-as";
const SAVE_DELAY_MS = 1000;
const POLL_MS = 4000;
const NOTICE_MS = 4000;
const HTTP_PRECONDITION_FAILED = 412;

const TOOL_CLASS = buttonClass("ghost", { sm: true });
const PRIMARY_CLASS = buttonClass("primary", { sm: true });
const SECONDARY_CLASS = buttonClass("secondary", { sm: true });

export interface MarkdownDocumentOptions {
  path: string;
  /** False where the file cannot be saved with a version check (a file read live from a computer): Read only. */
  canSave: boolean;
  /** Called when whether anything is unsaved changes, for the file page's header. */
  onChange: () => void;
}

/** A file's text and the version it was read at. */
interface Loaded {
  text: string;
  version: string | null;
}

class SaveError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

function element<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attributes: Record<string, string> = {},
  text = "",
): HTMLElementTagNameMap[K] {
  const created = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) created.setAttribute(name, value);
  if (text !== "") created.textContent = text;
  return created;
}

function button(label: string, id: string, className: string, title = ""): HTMLButtonElement {
  const created = element("button", { type: "button", id, class: className }, label);
  if (title !== "") created.title = title;
  return created;
}

function preferredMode(): Mode {
  const saved = localStorage.getItem(MODE_KEY);
  return saved === "edit" || saved === "read" ? saved : "suggest";
}

async function readFile(path: string): Promise<Loaded> {
  // Never from the browser's cache: the version must be the one on disk now.
  const response = await fetch(urlOf(path), { cache: "no-store" });
  if (!response.ok) throw new Error(`Couldn't read ${path}: ${(await response.text()).trim() || response.status}`);
  return { text: await response.text(), version: response.headers.get("ETag") };
}

async function versionOf(path: string): Promise<string | null> {
  const response = await fetch(urlOf(path), { method: "HEAD", cache: "no-store" });
  return response.ok ? response.headers.get("ETag") : null;
}

async function writeFile(path: string, text: string, version: string | null): Promise<string | null> {
  const headers: Record<string, string> = version === null ? {} : { "If-Match": version };
  const response = await fetch(urlOf(path), { method: "PUT", body: text, headers });
  if (!response.ok) throw new SaveError((await response.text()).trim() || `error ${response.status}`, response.status);
  return response.headers.get("ETag");
}

export class MarkdownDocument {
  private editor: Editor | null = null;
  private doc: OpenDocument | null = null;
  private version: string | null = null;
  private mode: Mode = "suggest";
  private isSaving = false;
  private isConflicted = false;
  /** Bumped on every edit, so a save that finishes knows whether newer edits are waiting. */
  private editCount = 0;
  private saveTimer: number | null = null;
  private pollTimer: number | null = null;
  private noticeTimer: number | null = null;
  isDirty = false;

  private readonly root = element("div", { class: "files-markdown" });
  private readonly modeSwitch = element("div", {
    class: "files-markdown-mode-switch",
    role: "radiogroup",
    "aria-label": "Editing mode",
  });
  private readonly controls = element("div", { class: "files-markdown-controls", id: "markdown-toolbar" });
  private readonly reviewBox = element("span", { class: "files-markdown-review", id: "markdown-review" });
  private readonly reviewCountsText = element("span", { id: "review-counts" });
  private readonly acceptAll = button("Accept all", "accept-all", TOOL_CLASS, "Keep every suggested change");
  private readonly rejectAll = button("Reject all", "reject-all", TOOL_CLASS, "Undo every suggested change");
  private readonly saveStatus = element("span", { class: "files-markdown-save-status", id: "save-status" });
  private readonly banner = element("div", { class: "files-markdown-banner", id: "markdown-banner" });
  private readonly bannerText = element("span", { id: "markdown-banner-text" });
  private readonly bannerActions = element("span", { class: "files-markdown-banner-actions" });
  private readonly frontMatter = element("details", { class: "files-markdown-front-matter" });
  private readonly frontMatterText = element("pre");
  private readonly editorHost = element("div", { id: "markdown-editor" });
  private readonly popover = element("div", { class: "files-markdown-popover", id: "markdown-popover" });
  private readonly commandButtons: HTMLButtonElement[] = [];

  private readonly onKeydown = (event: KeyboardEvent): void => {
    if (event.key === "Escape" && !this.popover.hidden) this.closePopover();
    if ((event.metaKey || event.ctrlKey) && event.altKey && event.code === "KeyM") {
      event.preventDefault();
      this.addComment();
    }
  };

  private readonly onMousedown = (event: MouseEvent): void => {
    const target = event.target as Node;
    if (!this.popover.hidden && !this.popover.contains(target) && !this.editorHost.contains(target)) {
      this.closePopover();
    }
  };

  constructor(readonly options: MarkdownDocumentOptions) {}

  /** Draw the page into `host` and open the file. */
  async mount(host: HTMLElement): Promise<void> {
    this.buildControls();
    this.banner.hidden = true;
    this.banner.append(this.bannerText, this.bannerActions);
    this.frontMatter.hidden = true;
    this.frontMatter.append(element("summary", {}, "Properties"), this.frontMatterText);
    this.popover.hidden = true;
    const bar = element("div", { class: "files-markdown-bar" });
    bar.append(this.controls, this.banner);
    const article = element("article", { class: "files-markdown-document" });
    article.append(this.frontMatter, this.editorHost);
    this.root.append(bar, article, this.popover);
    host.append(this.root);
    this.editorHost.addEventListener("click", (event) => this.onEditorClick(event));
    document.addEventListener("keydown", this.onKeydown);
    document.addEventListener("mousedown", this.onMousedown);
    this.pollTimer = window.setInterval(() => void this.poll(), POLL_MS);
    try {
      this.show(await readFile(this.options.path));
    } catch (error) {
      this.setStatus(error instanceof Error ? error.message : String(error), "error");
    }
  }

  destroy(): void {
    for (const timer of [this.saveTimer, this.noticeTimer]) if (timer !== null) window.clearTimeout(timer);
    if (this.pollTimer !== null) window.clearInterval(this.pollTimer);
    document.removeEventListener("keydown", this.onKeydown);
    document.removeEventListener("mousedown", this.onMousedown);
    this.editor?.destroy();
    this.editor = null;
    this.root.remove();
  }

  /** Save now rather than after the pause (Cmd/Ctrl+S). */
  async saveNow(): Promise<void> {
    if (this.saveTimer !== null) window.clearTimeout(this.saveTimer);
    this.saveTimer = null;
    await this.save();
  }

  // ---------------------------------------------------------------- the bar

  private buildControls(): void {
    for (const [mode, label, title] of [
      ["suggest", "Suggest", "Your edits are tracked as suggestions; you can comment"],
      ["edit", "Edit", "Your edits change the file directly"],
      ["read", "Read", "Read only"],
    ] as const) {
      const modeButton = button(label, `mode-${mode}`, TOOL_CLASS, title);
      modeButton.setAttribute("role", "radio");
      modeButton.dataset["mode"] = mode;
      modeButton.addEventListener("click", () => {
        if (!modeButton.disabled) this.applyMode(mode, true);
      });
      this.modeSwitch.append(modeButton);
    }
    this.controls.append(this.modeSwitch);
    const commands: [string, string, string, () => void][] = [
      ["H1", "h1", "Heading 1", () => this.chain().toggleHeading({ level: 1 }).run()],
      ["H2", "h2", "Heading 2", () => this.chain().toggleHeading({ level: 2 }).run()],
      ["B", "bold", "Bold (Ctrl+B)", () => this.chain().toggleBold().run()],
      ["I", "italic", "Italic (Ctrl+I)", () => this.chain().toggleItalic().run()],
      ["\u2022", "bullet", "Bulleted list", () => this.chain().toggleBulletList().run()],
      ["1.", "ordered", "Numbered list", () => this.chain().toggleOrderedList().run()],
      ["\u201c", "quote", "Quote", () => this.chain().toggleBlockquote().run()],
      ["Link", "link", "Link", () => this.editLink()],
      ["Comment", "comment", "Comment on the selected text (Ctrl+Alt+M)", () => this.addComment()],
      ["Undo", "undo", "Undo (Ctrl+Z)", () => this.chain().undo().run()],
      ["Redo", "redo", "Redo (Ctrl+Shift+Z)", () => this.chain().redo().run()],
    ];
    for (const [label, id, title, run] of commands) {
      const commandButton = button(label, `tool-${id}`, TOOL_CLASS, title);
      // Keep the editor's selection while a toolbar button is pressed.
      commandButton.addEventListener("mousedown", (event) => event.preventDefault());
      commandButton.addEventListener("click", () => {
        if (!commandButton.disabled && this.editor !== null) run();
      });
      this.commandButtons.push(commandButton);
      if (id === "undo") this.controls.append(element("span", { class: "files-markdown-gap" }));
      this.controls.append(commandButton);
    }
    this.acceptAll.addEventListener("click", () => {
      if (this.editor !== null) acceptSuggestion(this.editor, null);
      this.closePopover();
    });
    this.rejectAll.addEventListener("click", () => {
      if (this.editor !== null) rejectSuggestion(this.editor, null);
      this.closePopover();
    });
    this.reviewBox.append(this.reviewCountsText, this.acceptAll, this.rejectAll);
    this.reviewBox.hidden = true;
    this.controls.append(this.reviewBox, this.saveStatus);
  }

  private chain(): ReturnType<Editor["chain"]> {
    return (this.editor as Editor).chain().focus();
  }

  private setStatus(text: string, tone: "" | "error" = ""): void {
    this.saveStatus.textContent = text;
    if (tone === "") delete this.saveStatus.dataset["tone"];
    else this.saveStatus.dataset["tone"] = tone;
  }

  private setDirty(isDirty: boolean): void {
    if (this.isDirty === isDirty) return;
    this.isDirty = isDirty;
    this.options.onChange();
  }

  private showBanner(text: string, actions: { label: string; id: string; run: () => void }[], tone: string): void {
    this.bannerText.textContent = text;
    this.bannerActions.replaceChildren(
      ...actions.map((action) => {
        const actionButton = button(action.label, action.id, SECONDARY_CLASS);
        actionButton.addEventListener("click", action.run);
        return actionButton;
      }),
    );
    this.banner.dataset["tone"] = tone;
    this.banner.hidden = false;
  }

  private hideBanner(): void {
    this.banner.hidden = true;
  }

  private showConflict(): void {
    this.isConflicted = true;
    this.setStatus("Not saved", "error");
    this.showBanner(
      "This file was changed outside the editor while you had unsaved edits.",
      [
        { id: "conflict-load", label: "Load the new version", run: () => void this.reloadFromDisk() },
        { id: "conflict-keep", label: "Keep mine", run: () => void this.save(true).then(() => this.hideBanner()) },
      ],
      "warn",
    );
  }

  // ---------------------------------------------------------------- loading

  /** A fresh editor per load, so Undo never reaches back into an older version of the file. */
  private show(loaded: Loaded): void {
    const scroll = this.root.parentElement?.scrollTop ?? 0;
    this.editor?.destroy();
    this.editorHost.replaceChildren();
    const editor = createEditor(this.editorHost, () => this.onEdit());
    this.editor = editor;
    this.doc = openText(editor, loaded.text);
    this.version = loaded.version;
    this.isConflicted = false;
    this.setDirty(false);
    const { frontMatter } = this.doc.loaded;
    this.frontMatter.hidden = !frontMatter;
    this.frontMatterText.textContent = frontMatter.replace(/^---[^\n]*\n/, "").replace(/\n(---|\.\.\.)[ \t]*\n?$/, "");
    this.applyMode(this.options.canSave ? preferredMode() : "read", false);
    this.setStatus(this.options.canSave ? "Saved" : "Read only");
    if (this.root.parentElement !== null) this.root.parentElement.scrollTop = scroll;
  }

  private async reloadFromDisk(): Promise<void> {
    this.show(await readFile(this.options.path));
    this.hideBanner();
  }

  // ---------------------------------------------------------------- modes

  private applyMode(mode: Mode, remember: boolean): void {
    const shown = this.options.canSave ? mode : "read";
    this.mode = shown;
    if (this.editor !== null) setMode(this.editor, shown);
    if (remember) localStorage.setItem(MODE_KEY, shown);
    for (const modeButton of this.modeSwitch.querySelectorAll<HTMLButtonElement>("button[data-mode]")) {
      modeButton.setAttribute("aria-checked", String(modeButton.dataset["mode"] === shown));
      modeButton.disabled = !this.options.canSave && modeButton.dataset["mode"] !== "read";
    }
    this.root.dataset["mode"] = shown;
    for (const commandButton of this.commandButtons) commandButton.disabled = shown === "read";
    this.updateReview();
  }

  // ---------------------------------------------------------------- saving

  private onEdit(): void {
    if (this.doc === null) return;
    this.editCount++;
    this.setDirty(true);
    this.setStatus("Unsaved");
    this.updateReview();
    if (this.saveTimer !== null) window.clearTimeout(this.saveTimer);
    this.saveTimer = window.setTimeout(() => {
      this.saveTimer = null;
      void this.save();
    }, SAVE_DELAY_MS);
  }

  private async save(force = false): Promise<void> {
    const { doc, editor } = this;
    if (doc === null || editor === null || !this.options.canSave || this.isSaving) return;
    if (this.isConflicted && !force) return;
    const text = currentText(editor, doc);
    if (text === doc.savedText && !force) {
      this.setDirty(false);
      this.setStatus("Saved");
      return;
    }
    const editsAtStart = this.editCount;
    this.isSaving = true;
    this.setStatus("Saving...");
    try {
      // "Keep mine" writes over whatever is there now.
      const version = force ? await versionOf(this.options.path) : this.version;
      this.version = await writeFile(this.options.path, text, version);
      doc.savedText = text;
      this.isConflicted = false;
      if (this.editCount === editsAtStart) {
        this.setDirty(false);
        this.setStatus("Saved");
      }
    } catch (error) {
      if (error instanceof SaveError && error.status === HTTP_PRECONDITION_FAILED) this.showConflict();
      else this.setStatus(`Couldn't save: ${error instanceof Error ? error.message : String(error)}`, "error");
    } finally {
      this.isSaving = false;
    }
    if (this.isDirty && this.editCount !== editsAtStart && !this.isConflicted) void this.saveNow();
  }

  private async poll(): Promise<void> {
    if (this.doc === null || this.isSaving || this.isConflicted || document.visibilityState !== "visible") return;
    try {
      const version = await versionOf(this.options.path);
      if (version === null || version === this.version || this.isSaving) return;
      if (this.isDirty) {
        this.showConflict();
        return;
      }
      await this.reloadFromDisk();
      this.showBanner("This file was just updated outside the editor; showing the new version.", [], "info");
      if (this.noticeTimer !== null) window.clearTimeout(this.noticeTimer);
      this.noticeTimer = window.setTimeout(() => this.hideBanner(), NOTICE_MS);
    } catch {
      // The server is restarting or the file moved; the next check tries again.
    }
  }

  // ---------------------------------------------------------------- review

  private updateReview(): void {
    if (this.editor === null) return;
    const counts = reviewCounts(this.editor);
    const parts: string[] = [];
    if (counts.suggestions) parts.push(`${counts.suggestions} suggestion${counts.suggestions === 1 ? "" : "s"}`);
    if (counts.comments) parts.push(`${counts.comments} comment${counts.comments === 1 ? "" : "s"}`);
    this.reviewCountsText.textContent = parts.join(" \u00b7 ");
    this.reviewBox.hidden = parts.length === 0;
    const canAct = counts.suggestions > 0 && this.mode !== "read";
    this.acceptAll.hidden = !canAct;
    this.rejectAll.hidden = !canAct;
  }

  // ---------------------------------------------------------------- popover

  private closePopover(): void {
    this.popover.hidden = true;
    this.popover.replaceChildren();
  }

  private placePopover(left: number, top: number): void {
    this.popover.hidden = false;
    const width = this.popover.offsetWidth;
    this.popover.style.left = `${Math.max(8, Math.min(left, window.innerWidth - width - 8))}px`;
    this.popover.style.top = `${top + 6}px`;
  }

  /** A small form in the popover: one text field, Save and Cancel. */
  private askText(options: {
    title: string;
    value: string;
    placeholder: string;
    isMultiline: boolean;
    hasSignature: boolean;
    at: { left: number; bottom: number };
    onSave: (value: string) => void;
  }): void {
    this.closePopover();
    const form = element("form");
    form.append(element("p", { class: "files-markdown-popover-title" }, options.title));
    const field = options.isMultiline ? element("textarea") : element("input");
    field.id = "popover-field";
    field.value = options.value;
    field.placeholder = options.placeholder;
    form.append(field);
    let signature: HTMLInputElement | null = null;
    if (options.hasSignature) {
      const label = element("label", { class: "files-markdown-signature" }, "Sign as ");
      signature = element("input", { id: "popover-signature", placeholder: "your name (optional)" });
      signature.value = localStorage.getItem(SIGN_KEY) ?? "";
      label.append(signature);
      form.append(label);
    }
    const actions = element("div", { class: "files-markdown-popover-actions" });
    const saveButton = button("Save", "popover-save", PRIMARY_CLASS);
    saveButton.type = "submit";
    const cancelButton = button("Cancel", "popover-cancel", SECONDARY_CLASS);
    cancelButton.addEventListener("click", () => {
      this.closePopover();
      this.editor?.commands.focus();
    });
    actions.append(cancelButton, saveButton);
    form.append(actions);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (signature !== null) localStorage.setItem(SIGN_KEY, signature.value.trim());
      options.onSave(field.value);
      this.closePopover();
    });
    field.addEventListener("keydown", (event) => {
      const keyboard = event as KeyboardEvent;
      if (keyboard.key === "Enter" && (!options.isMultiline || keyboard.metaKey || keyboard.ctrlKey)) {
        keyboard.preventDefault();
        form.requestSubmit();
      }
      if (keyboard.key === "Escape") {
        this.closePopover();
        this.editor?.commands.focus();
      }
    });
    this.popover.append(form);
    this.placePopover(options.at.left, options.at.bottom);
    field.focus();
  }

  private selectionCoords(editor: Editor): { left: number; bottom: number } {
    const coords = editor.view.coordsAtPos(editor.state.selection.to);
    return { left: coords.left, bottom: coords.bottom };
  }

  private signed(text: string): string {
    const name = (localStorage.getItem(SIGN_KEY) ?? "").trim();
    const clean = cleanCommentText(text);
    if (!clean) return "";
    return name && !clean.startsWith(`${name}:`) ? `${name}: ${clean}` : clean;
  }

  private addComment(): void {
    const editor = this.editor;
    if (editor === null || this.mode === "read") return;
    const { empty } = editor.state.selection;
    this.askText({
      title: empty ? "Add a note here" : "Comment on the selected text",
      value: "",
      placeholder: "Your comment",
      isMultiline: true,
      hasSignature: true,
      at: this.selectionCoords(editor),
      onSave: (value) => {
        const text = this.signed(value);
        if (!text) return;
        const chain = editor.chain().focus().setMeta(suggestChangesKey, { skip: true });
        if (empty) chain.insertContent({ type: "commentNote", attrs: { text } }).run();
        else chain.setMark("comment", { text }).run();
      },
    });
  }

  private editLink(): void {
    const editor = this.editor;
    if (editor === null || this.mode === "read") return;
    this.askText({
      title: "Link to",
      value: String(editor.getAttributes("link")["href"] ?? ""),
      placeholder: "https://...",
      isMultiline: false,
      hasSignature: false,
      at: this.selectionCoords(editor),
      onSave: (value) => {
        const href = value.trim();
        const chain = editor.chain().focus().extendMarkRange("link");
        if (href) chain.setLink({ href }).run();
        else chain.unsetLink().run();
      },
    });
  }

  /** Clicking a suggestion or a comment shows what it is and what can be done with it. */
  private onEditorClick(event: MouseEvent): void {
    const editor = this.editor;
    const target = (event.target as HTMLElement).closest<HTMLElement>(
      "ins.suggestion, del.suggestion, span.comment, span.comment-note",
    );
    if (editor === null || target === null) {
      this.closePopover();
      return;
    }
    const rect = target.getBoundingClientRect();
    const canAct = this.mode !== "read";
    this.closePopover();
    const box = element("div");
    const actions = element("div", { class: "files-markdown-popover-actions" });
    const action = (label: string, id: string, className: string, run: () => void): void => {
      const actionButton = button(label, id, className);
      actionButton.addEventListener("click", () => {
        run();
        this.closePopover();
      });
      actions.append(actionButton);
    };

    if (target.matches("ins.suggestion, del.suggestion")) {
      let id: number | string | null;
      try {
        id = JSON.parse(target.dataset["id"] ?? "null");
      } catch {
        id = target.dataset["id"] ?? null;
      }
      const isInsertion = target.matches("ins");
      box.append(
        element(
          "p",
          { class: "files-markdown-popover-title" },
          isInsertion ? "Suggested addition" : "Suggested removal",
        ),
      );
      if (canAct && id !== null) {
        const suggestion = id;
        action("Reject", "suggestion-reject", SECONDARY_CLASS, () => rejectSuggestion(editor, suggestion));
        action("Accept", "suggestion-accept", PRIMARY_CLASS, () => acceptSuggestion(editor, suggestion));
        box.append(actions);
      }
    } else {
      const text = target.dataset["comment"] ?? "";
      const isNote = target.matches("span.comment-note");
      box.append(
        element("p", { class: "files-markdown-popover-title" }, isNote ? "Note" : "Comment"),
        element("p", { class: "files-markdown-popover-comment" }, text || "(no text)"),
      );
      if (canAct) {
        const position = editor.view.posAtDOM(target, 0);
        const edit = (): void =>
          this.askText({
            title: "Edit comment",
            value: text,
            placeholder: "Your comment",
            isMultiline: true,
            hasSignature: false,
            at: { left: rect.left, bottom: rect.bottom },
            onSave: (value) => {
              const clean = cleanCommentText(value);
              if (!clean) return;
              const chain = editor.chain().setMeta(suggestChangesKey, { skip: true });
              if (isNote) chain.setNodeSelection(position).updateAttributes("commentNote", { text: clean }).run();
              else
                chain.setTextSelection(position).extendMarkRange("comment").setMark("comment", { text: clean }).run();
            },
          });
        const resolve = (): void => {
          const chain = editor.chain().setMeta(suggestChangesKey, { skip: true });
          if (isNote) chain.deleteRange({ from: position, to: position + 1 }).run();
          else chain.setTextSelection(position).extendMarkRange("comment").unsetMark("comment").run();
        };
        const editButton = button("Edit", "comment-edit", SECONDARY_CLASS);
        // Edit opens its own form in the popover, so it does not close it afterwards.
        editButton.addEventListener("click", edit);
        actions.append(editButton);
        action("Resolve", "comment-resolve", PRIMARY_CLASS, resolve);
        box.append(actions);
      }
    }
    this.popover.append(box);
    this.placePopover(rect.left, rect.bottom);
  }
}
