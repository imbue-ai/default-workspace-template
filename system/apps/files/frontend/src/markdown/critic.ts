// Suggestions and comments, read from and written to the file as CriticMarkup (docs/decisions.md, decision 2):
//
//   {++inserted++}  {--deleted--}  {~~old~>new~~}  {==highlighted==}{>>comment<<}  {>>bare comment<<}
//
// The insertion and deletion marks are the ones the suggest-changes plugin works with (same names, same `id`
// attribute), so a suggestion read from the file can be accepted or rejected exactly like one typed in Suggest mode.
import { Mark, Node, mergeAttributes } from "@tiptap/core";
import type { JSONContent, MarkdownToken } from "@tiptap/core";

export const SUGGESTION_MARKS = "insertion modification deletion";

let nextSuggestionId = 1;

/** Restart suggestion numbering; called before a file is parsed. */
export function resetSuggestionIds(): void {
  nextSuggestionId = 1;
}

function takeSuggestionId(): number {
  return nextSuggestionId++;
}

function addMark(nodes: JSONContent[], mark: { type: string; attrs?: Record<string, unknown> }): JSONContent[] {
  return nodes.map((node) => ({ ...node, marks: [...(node.marks ?? []), mark] }));
}

/** A comment's text as it may sit between `{>>` and `<<}`. */
export function cleanCommentText(text: string): string {
  return text.replace(/<<\}/g, "<< }").replace(/\r?\n/g, " ").trim();
}

const CRITIC_START = /\{(\+\+|--|~~|==|>>)/;

type CriticToken = MarkdownToken & {
  kind: "ins" | "del" | "sub" | "hl" | "note";
  newTokens?: MarkdownToken[];
  comment?: string | null;
  text?: string;
};

function idAttribute() {
  return {
    id: {
      default: null,
      parseHTML: (element: HTMLElement) => {
        const raw = element.dataset["id"];
        if (!raw) return null;
        try {
          return JSON.parse(raw);
        } catch {
          return raw;
        }
      },
      renderHTML: (attributes: Record<string, unknown>) => ({ "data-id": JSON.stringify(attributes["id"]) }),
    },
  };
}

export const Insertion = Mark.create({
  name: "insertion",
  inclusive: false,
  excludes: "deletion modification insertion",
  addAttributes: idAttribute,
  parseHTML() {
    return [{ tag: "ins[data-id]" }];
  },
  renderHTML({ HTMLAttributes }) {
    return ["ins", mergeAttributes(HTMLAttributes, { class: "suggestion suggestion-insert" }), 0];
  },

  // The one tokenizer for every CriticMarkup form lives here; each form is turned into marks or nodes below.
  markdownTokenName: "critic",
  markdownTokenizer: {
    name: "critic",
    level: "inline",
    start: (src: string) => src.search(CRITIC_START),
    tokenize(src, _tokens, lexer) {
      let match = /^\{\+\+([\s\S]*?)\+\+\}/.exec(src);
      if (match) return { type: "critic", kind: "ins", raw: match[0], tokens: lexer.inlineTokens(match[1]) };
      match = /^\{--([\s\S]*?)--\}/.exec(src);
      if (match) return { type: "critic", kind: "del", raw: match[0], tokens: lexer.inlineTokens(match[1]) };
      match = /^\{~~([\s\S]*?)~>([\s\S]*?)~~\}/.exec(src);
      if (match) {
        return {
          type: "critic",
          kind: "sub",
          raw: match[0],
          tokens: lexer.inlineTokens(match[1]),
          newTokens: lexer.inlineTokens(match[2]),
        };
      }
      match = /^\{==([\s\S]*?)==\}(?:\{>>([\s\S]*?)<<\})?/.exec(src);
      if (match) {
        return {
          type: "critic",
          kind: "hl",
          raw: match[0],
          tokens: lexer.inlineTokens(match[1]),
          comment: match[2] ?? null,
        };
      }
      match = /^\{>>([\s\S]*?)<<\}/.exec(src);
      if (match) return { type: "critic", kind: "note", raw: match[0], text: match[1], tokens: [] };
      return undefined;
    },
  },
  parseMarkdown(token, helpers) {
    const critic = token as CriticToken;
    const inner = helpers.parseInline(critic.tokens ?? []);
    switch (critic.kind) {
      case "ins":
        return addMark(inner, { type: "insertion", attrs: { id: takeSuggestionId() } });
      case "del":
        return addMark(inner, { type: "deletion", attrs: { id: takeSuggestionId() } });
      case "sub": {
        const id = takeSuggestionId();
        return [
          ...addMark(inner, { type: "deletion", attrs: { id } }),
          ...addMark(helpers.parseInline(critic.newTokens ?? []), { type: "insertion", attrs: { id } }),
        ];
      }
      case "hl":
        return addMark(inner, { type: "comment", attrs: { text: (critic.comment ?? "").trim() } });
      case "note":
        return { type: "commentNote", attrs: { text: (critic.text ?? "").trim() } };
    }
    return inner;
  },
  renderMarkdown: (node, helpers) => `{++${helpers.renderChildren(node)}++}`,
});

export const Deletion = Mark.create({
  name: "deletion",
  inclusive: false,
  excludes: "insertion modification deletion",
  addAttributes: idAttribute,
  parseHTML() {
    return [{ tag: "del[data-id]" }];
  },
  renderHTML({ HTMLAttributes }) {
    return ["del", mergeAttributes(HTMLAttributes, { class: "suggestion suggestion-delete" }), 0];
  },
  renderMarkdown: (node, helpers) => `{--${helpers.renderChildren(node)}--}`,
});

// The plugin records formatting changes with this mark. Suggest mode applies formatting directly (decision 4), so
// it only appears if something slips through; it is drawn as nothing and dropped when the file is written.
export const Modification = Mark.create({
  name: "modification",
  inclusive: false,
  excludes: "deletion insertion",
  addAttributes() {
    return {
      ...idAttribute(),
      type: { default: null },
      attrName: { default: null },
      previousValue: { default: null },
      newValue: { default: null },
    };
  },
  parseHTML() {
    return [{ tag: "span[data-type='modification']" }];
  },
  renderHTML() {
    return ["span", { "data-type": "modification" }, 0];
  },
  renderMarkdown: (node, helpers) => helpers.renderChildren(node),
});

export const Comment = Mark.create({
  name: "comment",
  inclusive: false,
  excludes: "",
  addAttributes() {
    return {
      text: {
        default: "",
        parseHTML: (element: HTMLElement) => element.dataset["comment"] ?? "",
        renderHTML: (attributes: Record<string, unknown>) => ({ "data-comment": String(attributes["text"] ?? "") }),
      },
    };
  },
  parseHTML() {
    return [{ tag: "span[data-comment]" }];
  },
  renderHTML({ HTMLAttributes }) {
    return ["span", mergeAttributes(HTMLAttributes, { class: "comment", title: HTMLAttributes["data-comment"] }), 0];
  },
  renderMarkdown: (node, helpers) => {
    const text = cleanCommentText(String(node.attrs?.text ?? ""));
    return `{==${helpers.renderChildren(node)}==}` + (text ? `{>>${text}<<}` : "");
  },
});

export const CommentNote = Node.create({
  name: "commentNote",
  group: "inline",
  inline: true,
  atom: true,
  selectable: true,
  addAttributes() {
    return {
      text: {
        default: "",
        parseHTML: (element: HTMLElement) => element.dataset["comment"] ?? "",
        renderHTML: (attributes: Record<string, unknown>) => ({ "data-comment": String(attributes["text"] ?? "") }),
      },
    };
  },
  parseHTML() {
    return [{ tag: "span.comment-note" }];
  },
  renderHTML({ HTMLAttributes }) {
    return ["span", mergeAttributes(HTMLAttributes, { class: "comment-note", title: HTMLAttributes["data-comment"] })];
  },
  renderMarkdown: (node) => `{>>${cleanCommentText(String(node.attrs?.text ?? ""))}<<}`,
});

/**
 * A block the editor cannot show faithfully (raw HTML, link definitions, anything unrecognised): shown and edited as
 * its source text, and written back exactly as it reads (docs/decisions.md, decision 3).
 */
export const SourceBlock = Node.create({
  name: "sourceBlock",
  group: "block",
  content: "text*",
  marks: SUGGESTION_MARKS,
  code: true,
  defining: true,
  parseHTML() {
    return [{ tag: "pre.source-block", preserveWhitespace: "full" }];
  },
  renderHTML() {
    return [
      "pre",
      { class: "source-block", title: "Shown as written: the editor has no view for this part" },
      ["code", 0],
    ];
  },
  renderMarkdown: (node) => (node.content ?? []).map((child: JSONContent) => child.text ?? "").join(""),
});

/** Strip the plugin's invisible place markers and drop the suggestion markup that ends up empty. */
export function tidyCriticMarkup(markdown: string): string {
  return markdown
    .replace(/\u200B/g, "")
    .replace(/\{\+\+\+\+\}|\{----\}|\{====\}/g, "")
    .replace(/\{--((?:(?!--\})[\s\S])*?)--\}\{\+\+((?:(?!\+\+\})[\s\S])*?)\+\+\}/g, "{~~$1~>$2~~}");
}
