// Saving never rewrites what was not edited (docs/decisions.md, decision 3).
//
// A file is split into its top-level blocks by the same `marked` lexer the editor parses with; every token carries
// its exact source text. Each block is parsed on its own, and on save every block whose content is unchanged is
// written back byte for byte. Only edited and new blocks go through the serializer.
import type { JSONContent } from "@tiptap/core";

import { tidyCriticMarkup } from "./critic";

/** Token types the editor has a faithful node for; anything else becomes a source block. */
const MODELLED_TOKEN_TYPES = new Set([
  "heading",
  "paragraph",
  "list",
  "taskList",
  "blockquote",
  "code",
  "hr",
  "table",
]);

export interface LexedToken {
  type: string;
  raw: string;
}

export interface MarkdownTools {
  lex: (markdown: string) => LexedToken[];
  parse: (markdown: string) => JSONContent;
  serialize: (doc: JSONContent) => string;
}

export interface Block {
  /** The block's exact source, including the blank lines after it. */
  raw: string;
  /** How many top-level editor nodes the block became. */
  nodeCount: number;
}

export interface LoadedDocument {
  frontMatter: string;
  /** Whitespace before the first block. */
  leading: string;
  blocks: Block[];
  /** The top-level nodes to put in the editor, in order. */
  content: JSONContent[];
  /** False when the lexer's pieces did not add back up to the file, so every save serializes the whole file. */
  preserving: boolean;
}

const FRONT_MATTER = /^---[ \t]*\r?\n[\s\S]*?\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|$)/;

/** Split YAML front matter (kept aside and written back untouched) from the body. */
export function splitFrontMatter(text: string): [string, string] {
  const match = FRONT_MATTER.exec(text);
  if (!match) return ["", text];
  return [match[0], text.slice(match[0].length)];
}

export function sourceBlockNode(raw: string): JSONContent {
  const text = raw.replace(/\s+$/, "");
  return text ? { type: "sourceBlock", content: [{ type: "text", text }] } : { type: "sourceBlock" };
}

export function loadDocument(text: string, tools: MarkdownTools): LoadedDocument {
  const [frontMatter, body] = splitFrontMatter(text);
  const tokens = tools.lex(body);
  const preserving = tokens.map((token) => token.raw).join("") === body;
  if (!preserving) {
    const content = tools.parse(body).content ?? [];
    return { frontMatter, leading: "", blocks: [{ raw: body, nodeCount: content.length }], content, preserving };
  }

  let leading = "";
  const pieces: { raw: string; type: string }[] = [];
  for (const token of tokens) {
    if (token.type === "space") {
      if (pieces.length) pieces[pieces.length - 1].raw += token.raw;
      else leading += token.raw;
    } else {
      // Some tokenizers (task lists) take the blank lines before them; give those back to the block before.
      const blankLines = /^(?:[ \t]*\n)+/.exec(token.raw)?.[0] ?? "";
      if (blankLines) {
        if (pieces.length) pieces[pieces.length - 1].raw += blankLines;
        else leading += blankLines;
      }
      pieces.push({ raw: token.raw.slice(blankLines.length), type: token.type });
    }
  }

  const blocks: Block[] = [];
  const content: JSONContent[] = [];
  for (const piece of pieces) {
    const nodes = MODELLED_TOKEN_TYPES.has(piece.type)
      ? (tools.parse(piece.raw.replace(/\s+$/, "")).content ?? [])
          .filter((node) => !isEmptyParagraph(node))
          .map(softBreaksToSpaces)
      : [sourceBlockNode(piece.raw)];
    if (!nodes.length && blocks.length) {
      // Nothing to show (a stray blank construct): keep its text with the block before it.
      blocks[blocks.length - 1].raw += piece.raw;
      continue;
    }
    if (!nodes.length) {
      leading += piece.raw;
      continue;
    }
    blocks.push({ raw: piece.raw, nodeCount: nodes.length });
    content.push(...nodes);
  }
  return { frontMatter, leading, blocks, content, preserving };
}

/**
 * A line break inside a paragraph of the source is a soft break, which reads as a space. The editor cannot hold it
 * as a newline (typing next to one makes the browser report the whole paragraph as replaced), so it becomes a space.
 * An unedited paragraph is still written back exactly as it was; an edited one comes out as one line.
 */
export function softBreaksToSpaces(node: JSONContent): JSONContent {
  if (node.type === "codeBlock" || node.type === "sourceBlock") return node;
  if (node.type === "text")
    return node.text?.includes("\n") ? { ...node, text: node.text.replace(/[ \t]*\n[ \t]*/g, " ") } : node;
  if (!node.content) return node;
  return { ...node, content: node.content.map(softBreaksToSpaces) };
}

export function isEmptyParagraph(node: JSONContent): boolean {
  if (node.type !== "paragraph") return false;
  return !(node.content ?? []).some((child) => child.type !== "text" || (child.text ?? "").replace(/[\u200B\s]/g, ""));
}

const SUGGESTION_MARK_TYPES = new Set(["insertion", "deletion"]);
/** A private-use character standing in for a space inside suggestion and comment marks while serializing. */
const HIDDEN_SPACE = "\uE000";

/**
 * Ready a node for the serializer: a suggestion mark on a whole block (a new or deleted paragraph) is moved onto
 * the block's text, since CriticMarkup only marks text; formatting-change marks are dropped; the plugin's
 * invisible place markers are removed.
 */
export function prepareForSerializing(node: JSONContent, inherited: JSONContent["marks"] = []): JSONContent | null {
  const ownMarks = node.marks ?? [];
  if (node.type === "text") {
    const text = (node.text ?? "").replace(/\u200B/g, "");
    if (!text) return null;
    let marks = ownMarks.filter((mark) => mark.type !== "modification");
    if (!marks.some((mark) => SUGGESTION_MARK_TYPES.has(mark.type))) marks = [...marks, ...(inherited ?? [])];
    // The serializer moves spaces at a mark's edges outside it (right for bold, wrong for a suggestion, where the
    // space is part of what was inserted or deleted), and drops a mark on spaces alone. Hide the spaces from it.
    const keepSpaces = marks.some((mark) => SUGGESTION_MARK_TYPES.has(mark.type) || mark.type === "comment");
    return {
      ...node,
      text: keepSpaces ? text.replace(/ /g, HIDDEN_SPACE) : text,
      marks: marks.length ? marks : undefined,
    };
  }
  const blockSuggestions = ownMarks.filter((mark) => SUGGESTION_MARK_TYPES.has(mark.type));
  const passDown = blockSuggestions.length ? blockSuggestions : inherited;
  const isInline = node.type === "commentNote" || node.type === "hardBreak" || node.type === "image";
  const keptMarks = isInline ? ownMarks.filter((mark) => mark.type !== "modification") : [];
  const result: JSONContent = { ...node, marks: keptMarks.length ? keptMarks : undefined };
  if (node.content) {
    result.content = node.content
      .map((child) => prepareForSerializing(child, passDown))
      .filter((child): child is JSONContent => child !== null);
  }
  return result;
}

export function serializeNode(node: JSONContent, tools: MarkdownTools): string {
  if (isEmptyParagraph(node)) return "";
  const prepared = prepareForSerializing(node);
  if (!prepared) return "";
  const markdown = tools.serialize({ type: "doc", content: [prepared] }).replace(/\uE000/g, " ");
  return tidyCriticMarkup(markdown).replace(/\s+$/, "");
}

/** A node's identity for "is this block unchanged". */
export function nodeKey(node: JSONContent): string {
  return JSON.stringify(node);
}

/** Longest common subsequence of two key lists, as pairs of matching indexes. */
function matchKeys(original: string[], current: string[]): Map<number, number> {
  const rows = original.length;
  const cols = current.length;
  const table: Uint32Array[] = Array.from({ length: rows + 1 }, () => new Uint32Array(cols + 1));
  for (let i = rows - 1; i >= 0; i--) {
    for (let j = cols - 1; j >= 0; j--) {
      table[i][j] = original[i] === current[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
    }
  }
  const currentToOriginal = new Map<number, number>();
  let i = 0;
  let j = 0;
  while (i < rows && j < cols) {
    if (original[i] === current[j]) {
      currentToOriginal.set(j, i);
      i++;
      j++;
    } else if (table[i + 1][j] >= table[i][j + 1]) {
      i++;
    } else {
      j++;
    }
  }
  return currentToOriginal;
}

/**
 * The file text for the editor's current content.
 *
 * `originalKeys` are the keys of the editor's top-level nodes right after the file was loaded (so the editor's own
 * normalisation of a node is never mistaken for an edit); `current` is its top-level nodes now.
 */
export function buildFileText(
  loaded: LoadedDocument,
  originalKeys: string[],
  current: JSONContent[],
  tools: MarkdownTools,
): string {
  if (!loaded.preserving) {
    const body = current
      .map((node) => serializeNode(node, tools))
      .filter(Boolean)
      .join("\n\n");
    return loaded.frontMatter + (body ? body + "\n" : "");
  }

  const currentKeys = current.map(nodeKey);
  const currentToOriginal = matchKeys(originalKeys, currentKeys);

  // Which original node index each block starts at.
  const blockStarts: number[] = [];
  let index = 0;
  for (const block of loaded.blocks) {
    blockStarts.push(index);
    index += block.nodeCount;
  }
  const blockAtStart = new Map(blockStarts.map((start, blockIndex) => [start, blockIndex]));

  let out = loaded.leading;
  let lastWasSerialized = false;
  // The block written last when it came from the file as is: the next block of the file can follow it verbatim.
  let previousBlock: number | null = -1;
  let position = 0;
  while (position < current.length) {
    const original = currentToOriginal.get(position);
    const blockIndex = original === undefined ? undefined : blockAtStart.get(original);
    if (blockIndex !== undefined) {
      const block = loaded.blocks[blockIndex];
      let intact = true;
      for (let k = 0; k < block.nodeCount; k++) {
        if (currentToOriginal.get(position + k) !== (original as number) + k) intact = false;
      }
      if (intact) {
        if (previousBlock === blockIndex - 1) out += block.raw;
        else out = ensureBlankLine(out) + block.raw;
        position += block.nodeCount;
        lastWasSerialized = false;
        previousBlock = blockIndex;
        continue;
      }
    }
    const text = serializeNode(current[position], tools);
    position++;
    previousBlock = null;
    if (!text) continue;
    out = ensureBlankLine(out) + text + "\n\n";
    lastWasSerialized = true;
  }
  if (lastWasSerialized) out = out.replace(/\n+$/, "\n");
  return loaded.frontMatter + out;
}

/** End `text` with a blank line so the next block starts on its own (nothing is added to an empty start). */
function ensureBlankLine(text: string): string {
  if (!text || /^\s*$/.test(text)) return text;
  if (text.endsWith("\n\n")) return text;
  if (text.endsWith("\n")) return text + "\n";
  return text + "\n\n";
}
