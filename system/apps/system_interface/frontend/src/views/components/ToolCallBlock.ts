/**
 * The collapsible tool-call block: a "▸ header" strip that expands in place to
 * reveal monospace input/output panes. One shared renderer for assistant tool
 * calls (views/message-renderers.ts) and collapsed system/hook chips
 * (views/user-message-display.ts). The markdown variant (fenced blocks wrapped
 * by src/markdown.ts) shares only the class NAMES; its look is the
 * `.markdown-content .tool-call-block` rules in style.css.
 *
 * Expansion is deliberately a DOM class (`tool-call-block--expanded`) toggled
 * on the real element rather than component state: the open state then
 * survives memoized redraws (StableAssistantMessage skips re-rendering, and a
 * re-render would reset vnode state). The children react to it through
 * `group-[.tool-call-block--expanded]/tool:*` variants, so the whole state
 * machine still lives in the markup.
 */

import m from "mithril";

/** The class names are bare markers (markdown.ts drives the same state class;
 *  the inspector reads them); the utilities beside them carry the look. */
const BLOCK_CLASS = "tool-call-block group/tool overflow-hidden rounded-md border bg-sidebar";

const HEADER_CLASS =
  "tool-call-header flex cursor-pointer items-center gap-1.5 px-2.5 py-[3px] font-mono " +
  "text-(length:--font-size-body) text-secondary select-none transition-colors duration-100 hover:bg-fill-hover";

// text-[10px]: icon glyph (the chevron), sized independently of the text scale.
const CHEVRON_CLASS =
  "tool-call-chevron inline-block text-[10px] transition-transform duration-150 " +
  "group-[.tool-call-block--expanded]/tool:rotate-90";

const DETAILS_CLASS = "tool-call-details hidden border-t group-[.tool-call-block--expanded]/tool:block";

const PANE_CLASS = "px-3 py-2";

/** One monospace pane of the expanded body. Color inherits, so the error tint
 *  sits on the pane and reaches the code text. */
function renderPane(marker: string, text: string, extra = ""): m.Vnode {
  return m("div", { class: `${marker} ${PANE_CLASS} ${extra}`.trim() }, [
    m(
      "pre",
      { class: "overflow-x-auto" },
      m(
        "code",
        { class: "font-mono text-(length:--font-size-helper) leading-normal break-all whitespace-pre-wrap" },
        text,
      ),
    ),
  ]);
}

/**
 * The collapsible block. `extra` is appended to the root's class string --
 * margins and width belong to the call site (the assistant flow gives it the
 * markdown rhythm, the system chip caps its width instead).
 */
export function renderToolBlock(options: {
  headerText: string;
  inputText?: string;
  outputText?: string;
  isError?: boolean;
  extra?: string;
}): m.Vnode {
  const { headerText, inputText = "", outputText = "", isError = false, extra = "" } = options;
  return m("div", { class: `${BLOCK_CLASS} ${extra}`.trim() }, [
    m(
      "div",
      {
        class: HEADER_CLASS,
        onclick(e: Event) {
          const block = (e.currentTarget as HTMLElement).parentElement;
          if (block) {
            block.classList.toggle("tool-call-block--expanded");
          }
        },
      },
      [m("span", { class: CHEVRON_CLASS }, "▸"), m("span", headerText)],
    ),
    m("div", { class: DETAILS_CLASS }, [
      inputText ? renderPane("tool-call-input", inputText) : null,
      outputText
        ? renderPane(
            isError ? "tool-call-output tool-call-output--error" : "tool-call-output",
            outputText,
            // The hairline between the panes exists exactly when both do --
            // resolved here instead of by a sibling-combinator rule.
            `${inputText ? "border-t border-subtle " : ""}${isError ? "text-danger" : ""}`.trim(),
          )
        : null,
    ]),
  ]);
}
