// One open file in the editor: load its text, and produce the text to save.
import type { Editor } from "@tiptap/core";

import { buildFileText, loadDocument, nodeKey } from "./blocks";
import type { LoadedDocument } from "./blocks";
import { resetSuggestionIds } from "./critic";
import { markdownTools, replaceContent } from "./editor";

export interface OpenDocument {
  loaded: LoadedDocument;
  originalKeys: string[];
  /** The text the file had when it was loaded, or after the last save. */
  savedText: string;
}

export function openText(editor: Editor, text: string): OpenDocument {
  resetSuggestionIds();
  const loaded = loadDocument(text, markdownTools(editor));
  replaceContent(editor, loaded.content);
  const nodes = editor.getJSON().content ?? [];
  if (loaded.preserving && nodes.length !== loaded.content.length) {
    // The editor reshaped the blocks (it should not); fall back to writing the whole file on save.
    loaded.preserving = false;
  }
  return { loaded, originalKeys: nodes.map(nodeKey), savedText: text };
}

export function currentText(editor: Editor, open: OpenDocument): string {
  return buildFileText(open.loaded, open.originalKeys, editor.getJSON().content ?? [], markdownTools(editor));
}
