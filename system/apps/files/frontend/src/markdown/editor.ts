// The editor: Tiptap 3 with its official markdown extension, plus the suggest-changes plugin for Suggest mode
// (docs/decisions.md, decisions 1 and 4).
import { Editor, Extension, Node as TiptapNode } from "@tiptap/core";
import type { JSONContent } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { Markdown } from "@tiptap/markdown";
import { BulletList, ListItem, OrderedList, TaskItem, TaskList } from "@tiptap/extension-list";
import { TableKit } from "@tiptap/extension-table";
import Blockquote from "@tiptap/extension-blockquote";
import Document from "@tiptap/extension-document";
import { Placeholder } from "@tiptap/extensions";
import type { Transaction } from "@tiptap/pm/state";
import {
  applySuggestion,
  applySuggestions,
  disableSuggestChanges,
  enableSuggestChanges,
  isSuggestChangesEnabled,
  revertSuggestion,
  revertSuggestions,
  suggestChanges,
  suggestChangesKey,
  transformToSuggestionTransaction,
} from "@handlewithcare/prosemirror-suggest-changes";

import { Comment, CommentNote, Deletion, Insertion, Modification, SourceBlock, SUGGESTION_MARKS } from "./critic";
import type { MarkdownTools } from "./blocks";

export type Mode = "suggest" | "edit" | "read";

function documentText(transaction: Transaction, which: "before" | "after"): string {
  const doc = which === "before" ? transaction.before : transaction.doc;
  return doc.textBetween(0, doc.content.size, "\n", "\uFFFC");
}

/**
 * In Suggest mode, turn every edit that changes the text into tracked insertions and deletions. An edit that leaves
 * the text alone (bold, a heading level, a list, a comment) is applied directly: CriticMarkup cannot record it as a
 * suggestion, and a suggestion that vanished on the next load would be worse than a plain change.
 */
const TrackChanges = Extension.create({
  name: "trackChanges",
  addProseMirrorPlugins() {
    return [suggestChanges()];
  },
  dispatchTransaction({ transaction, next }) {
    const state = this.editor.state;
    const track =
      transaction.docChanged &&
      isSuggestChangesEnabled(state) &&
      !transaction.getMeta("history$") &&
      !("skip" in (transaction.getMeta(suggestChangesKey) ?? {})) &&
      documentText(transaction, "before") !== documentText(transaction, "after");
    next(track ? transformToSuggestionTransaction(transaction, state) : transaction);
  },
});

// Suggestion marks may sit on whole blocks (a new list item, a deleted paragraph), so every node that holds blocks
// allows them on its children.
const withSuggestionMarks = <T extends TiptapNode>(node: T) => node.extend({ marks: SUGGESTION_MARKS }) as T;

export function createEditor(element: HTMLElement, onChange: () => void): Editor {
  return new Editor({
    element,
    editable: true,
    editorProps: { attributes: { spellcheck: "false" } },
    extensions: [
      StarterKit.configure({
        document: false,
        bulletList: false,
        orderedList: false,
        listItem: false,
        listKeymap: {},
        blockquote: false,
        underline: false,
        trailingNode: false,
        link: { openOnClick: false, autolink: true, linkOnPaste: true },
      }),
      withSuggestionMarks(Document),
      withSuggestionMarks(BulletList),
      withSuggestionMarks(OrderedList),
      withSuggestionMarks(ListItem),
      withSuggestionMarks(TaskList),
      withSuggestionMarks(TaskItem.configure({ nested: true })),
      withSuggestionMarks(Blockquote),
      TableKit.configure({ table: { resizable: false } }),
      Markdown.configure({ indentation: { style: "space", size: 2 } }),
      Placeholder.configure({ placeholder: "Empty file" }),
      Insertion,
      Deletion,
      Modification,
      Comment,
      CommentNote,
      SourceBlock,
      TrackChanges,
    ],
    onUpdate: ({ transaction }) => {
      if (transaction.docChanged) onChange();
    },
  });
}

export function markdownTools(editor: Editor): MarkdownTools {
  const manager = editor.markdown;
  if (!manager) throw new Error("The markdown extension did not load");
  return {
    lex: (markdown) => manager.instance.lexer(markdown) as unknown as { type: string; raw: string }[],
    parse: (markdown) => manager.parse(markdown),
    serialize: (doc) => manager.serialize(doc),
  };
}

/** Put content in the editor without it counting as an edit or a suggestion. */
export function replaceContent(editor: Editor, content: JSONContent[]): void {
  const doc = { type: "doc", content: content.length ? content : [{ type: "paragraph" }] };
  editor
    .chain()
    .setMeta(suggestChangesKey, { skip: true })
    .setMeta("addToHistory", false)
    .setMeta("fromFile", true)
    .setContent(doc, { emitUpdate: false })
    .run();
}

export function setMode(editor: Editor, mode: Mode): void {
  editor.setEditable(mode !== "read", false);
  const { state, view } = editor;
  if (mode === "suggest" && !isSuggestChangesEnabled(state)) enableSuggestChanges(state, view.dispatch);
  if (mode !== "suggest" && isSuggestChangesEnabled(editor.state)) disableSuggestChanges(editor.state, view.dispatch);
}

/** After a rejection, drop paragraphs that are now empty: they held only rejected text, and markdown cannot keep them. */
function dropEmptyParagraphs(editor: Editor): void {
  const { state } = editor;
  const ranges: [number, number][] = [];
  state.doc.descendants((node, pos) => {
    let onlyText = true;
    node.forEach((child) => {
      if (!child.isText) onlyText = false;
    });
    if (node.type.name === "paragraph" && onlyText && node.textContent.replace(/[\u200B\s]/g, "") === "") {
      ranges.push([pos, pos + node.nodeSize]);
    }
    return node.isBlock && !node.isTextblock;
  });
  if (!ranges.length || ranges.length >= state.doc.childCount) return;
  const tr = state.tr.setMeta(suggestChangesKey, { skip: true });
  for (const [from, to] of ranges.reverse()) {
    const $from = tr.doc.resolve(from);
    if ($from.parent.childCount > 1) tr.delete(from, to);
  }
  if (tr.docChanged) editor.view.dispatch(tr);
}

export function acceptSuggestion(editor: Editor, id: number | string | null): void {
  const { state, view } = editor;
  if (id === null) applySuggestions(state, view.dispatch);
  else applySuggestion(id as number)(state, view.dispatch);
}

export function rejectSuggestion(editor: Editor, id: number | string | null): void {
  const { state, view } = editor;
  if (id === null) revertSuggestions(state, view.dispatch);
  else revertSuggestion(id as number)(state, view.dispatch);
  dropEmptyParagraphs(editor);
}

export interface ReviewCounts {
  suggestions: number;
  comments: number;
}

export function reviewCounts(editor: Editor): ReviewCounts {
  const suggestionIds = new Set<string>();
  const comments = new Set<string>();
  editor.state.doc.descendants((node, pos) => {
    for (const mark of node.marks) {
      if (mark.type.name === "insertion" || mark.type.name === "deletion") suggestionIds.add(String(mark.attrs["id"]));
      if (mark.type.name === "comment") comments.add(`${mark.attrs["text"]}`);
    }
    if (node.type.name === "commentNote") comments.add(`note@${pos}`);
    return true;
  });
  return { suggestions: suggestionIds.size, comments: comments.size };
}
