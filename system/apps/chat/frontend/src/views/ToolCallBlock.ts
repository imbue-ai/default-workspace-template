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
 * machine still lives in the markup. A caller that also passes `expansionKey`
 * gets persistence across full unmounts too (virtualization evicting the row):
 * the toggle is recorded in the session-scoped expansion store and a fresh
 * mount renders back in the recorded state.
 */

import m from "mithril";
import { isBlockExpanded, setBlockExpanded } from "./expansion-state";

/** The class names are bare markers (markdown.ts drives the same state class;
 *  the inspector reads them); the utilities beside them carry the look. */
const BLOCK_CLASS = "tool-call-block group/tool overflow-hidden rounded-md border bg-sidebar";

const HEADER_CLASS =
  "tool-call-header flex cursor-pointer items-center gap-1.5 px-2.5 py-[3px] font-mono " +
  "text-(length:--font-size-body) text-secondary select-none transition-colors duration-(--dur-base) hover:bg-fill-hover";

// text-[10px]: icon glyph (the chevron), sized independently of the text scale.
const CHEVRON_CLASS =
  "tool-call-chevron inline-block text-[10px] transition-transform duration-(--dur-base) " +
  "group-[.tool-call-block--expanded]/tool:rotate-90";

const DETAILS_CLASS = "tool-call-details hidden border-t group-[.tool-call-block--expanded]/tool:block";

const PANE_CLASS = "px-3 py-2";

/** A pane's deferred-payload state: events are payload-free on the wire, so a
 *  pane may still be fetching or reference a payload the backend no longer holds. */
export type PayloadState = "loaded" | "loading" | "unavailable";

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

/** A pane that isn't loaded yet renders as a quiet italic note instead of code. */
function renderPaneNote(marker: string, state: "loading" | "unavailable", extra = ""): m.Vnode {
  return m(
    "div",
    {
      class:
        `${marker} tool-call-payload-note ${PANE_CLASS} text-(length:--font-size-helper) italic text-secondary ${extra}`.trim(),
    },
    state === "loading" ? "Loading…" : "No longer available",
  );
}

/** One section of the expanded body, or nothing (loaded with no text). */
function renderSection(marker: string, text: string, state: PayloadState, extra = ""): m.Vnode | null {
  if (state !== "loaded") return renderPaneNote(marker, state, extra);
  return text ? renderPane(marker, text, extra) : null;
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
  /** Stable identity in the expansion store (e.g. the tool call's id), so the
   *  open state survives the row unmounting and remounting (virtualization)
   *  or re-rendering (streaming). Omitted: open state is this-mount-only. */
  expansionKey?: string;
  /** A failed call's stamped first line -- shown under the collapsed header so
   *  the failure stays glanceable without expanding (or fetching) anything. */
  errorSnippet?: string;
  /** Deferred-payload states; default "loaded" (the text is already in hand). */
  inputState?: PayloadState;
  outputState?: PayloadState;
  /** Called when the header toggles the block open -- the hook for kicking off
   *  on-demand payload fetches. */
  onExpand?: () => void;
}): m.Vnode {
  const {
    headerText,
    inputText = "",
    outputText = "",
    isError = false,
    extra = "",
    expansionKey,
    errorSnippet,
    inputState = "loaded",
    outputState = "loaded",
    onExpand,
  } = options;
  const startExpanded = expansionKey !== undefined && isBlockExpanded(expansionKey);
  const inputSection = renderSection("tool-call-input", inputText, inputState);
  const outputSection = renderSection(
    isError ? "tool-call-output tool-call-output--error" : "tool-call-output",
    outputText,
    outputState,
    // The hairline between the panes exists exactly when both do --
    // resolved here instead of by a sibling-combinator rule.
    `${inputSection ? "border-t border-subtle " : ""}${isError ? "text-danger" : ""}`.trim(),
  );
  return m("div", { class: `${BLOCK_CLASS}${startExpanded ? " tool-call-block--expanded" : ""} ${extra}`.trim() }, [
    m(
      "div",
      {
        class: HEADER_CLASS,
        onclick(e: Event) {
          const block = (e.currentTarget as HTMLElement).parentElement;
          if (block) {
            // Toggle the DOM directly (memoized wrappers skip re-patching)
            // AND record it so a fresh mount renders in the same state.
            const isNowExpanded = block.classList.toggle("tool-call-block--expanded");
            if (expansionKey !== undefined) {
              setBlockExpanded(expansionKey, isNowExpanded);
            }
            if (isNowExpanded) {
              onExpand?.();
            }
          }
        },
      },
      [m("span", { class: CHEVRON_CLASS }, "▸"), m("span", headerText)],
    ),
    // A failed call stays glanceable without a fetch: its stamped first line
    // rides the event and shows under the collapsed header.
    isError && errorSnippet
      ? m(
          "div",
          {
            class:
              "tool-call-error-snippet border-t px-2.5 py-[3px] font-mono text-(length:--font-size-helper) text-danger",
          },
          errorSnippet,
        )
      : null,
    inputSection || outputSection ? m("div", { class: DETAILS_CLASS }, [inputSection, outputSection]) : null,
  ]);
}
