/**
 * The Start button at the taskbar's left (launcher-and-getting-started plan section 4.1, in
 * Windows 95 dress): a click opens the launcher menu, and a second click closes it. The typed
 * filter and message field of the launcher still exists, as the foot of the open menu: a one-row
 * text area hidden while the menu is closed, focused when it opens, whose typing filters the menu.
 * Shift+Enter breaks the line and the field grows with its text (to a cap, then scrolls), so a
 * longer message can be written here; with a line break in it the text is a message, and the menu
 * offers the free-text rows alone. The field tells its owner how tall it stands so the menu can sit
 * above it. Its keys are the menu's (plan section 4.8): the arrows move the highlight (the caret,
 * once the text has lines), Enter runs the highlight, Ctrl+Enter (Cmd+Enter on a Mac) runs the
 * secondary text action, and Escape clears the text, then closes.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import { isMessageText } from "../reducers/launcherRows";

export const LAUNCHER_PLACEHOLDER = "Start app or send message...";
const FIELD_GLYPH_SIZE = 14;
/** Past this many lines the field scrolls rather than growing. */
export const MAX_FIELD_LINES = 8;

/** The four-pane flag on the Start button, drawn as pixels. */
const START_FLAG_MARKUP =
  '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="14" viewBox="0 0 16 14" shape-rendering="crispEdges" aria-hidden="true">' +
  '<rect x="1" y="3" width="6" height="4" fill="#ff0000"/><rect x="8" y="2" width="7" height="4" fill="#00a000"/>' +
  '<rect x="1" y="8" width="6" height="4" fill="#0000ff"/><rect x="8" y="7" width="7" height="4" fill="#ffff00"/>' +
  "</svg>";

export interface LauncherFieldAttrs {
  readonly query: string;
  readonly isOpen: boolean;
  readonly isCompact: boolean;
  readonly onOpen: () => void;
  readonly onClose: () => void;
  readonly onQuery: (query: string) => void;
  /** Move the highlight one enabled row down (1) or up (-1), wrapping. */
  readonly onMoveHighlight: (delta: number) => void;
  /** Run the highlighted row. */
  readonly onRunHighlight: () => void;
  /** Run the secondary text action, whatever is highlighted. */
  readonly onRunSecondary: () => void;
  /** How far the field stands above the taskbar, in px, each time that changes (0 while it is hidden). */
  readonly onRise: (risePx: number) => void;
}

/** Whether a key press is the secondary action's chord: Enter with Ctrl, or with Cmd on a Mac. */
export function isSecondaryChord(event: Pick<KeyboardEvent, "key" | "ctrlKey" | "metaKey">): boolean {
  return event.key === "Enter" && (event.ctrlKey || event.metaKey);
}

/** Whether a key press is the line break: Enter with Shift and nothing else. */
export function isLineBreakChord(
  event: Pick<KeyboardEvent, "key" | "shiftKey" | "ctrlKey" | "metaKey" | "altKey">,
): boolean {
  return event.key === "Enter" && event.shiftKey && !event.ctrlKey && !event.metaKey && !event.altKey;
}

/** Size the text area to its text: one line until it has more, and at most MAX_FIELD_LINES before it scrolls. */
function fitField(field: HTMLTextAreaElement): void {
  field.style.height = "";
  const lineHeight = Number.parseFloat(getComputedStyle(field).lineHeight);
  if (field.scrollHeight === 0 || Number.isNaN(lineHeight)) return;
  // With no height set, the one-row text area is its line plus its padding.
  const oneRowHeight = field.offsetHeight;
  const cap = lineHeight * MAX_FIELD_LINES + Math.max(0, oneRowHeight - lineHeight);
  field.style.height = `${Math.min(Math.max(field.scrollHeight, oneRowHeight), cap)}px`;
}

export function LauncherField(): m.Component<LauncherFieldAttrs> {
  // The rise last told to the owner, told again only when it changes (a redraw follows each telling).
  let reportedRise = 0;
  // Whether the last render had the menu open: the field takes the focus on the render that opens it.
  let wasOpen = false;

  function fit(area: HTMLTextAreaElement, attrs: LauncherFieldAttrs): void {
    fitField(area);
    // The whole field stands above the taskbar while the menu is open, and nothing does while it is closed.
    const rise = attrs.isOpen ? (area.parentElement?.offsetHeight ?? 0) : 0;
    if (rise === reportedRise) return;
    reportedRise = rise;
    attrs.onRise(rise);
  }

  return {
    view(vnode) {
      const { query, isOpen, onOpen, onClose, onQuery } = vnode.attrs;
      const start = m(
        "button",
        {
          type: "button",
          "data-launcher-start": "",
          class: "launcher-start",
          "aria-haspopup": "menu",
          "aria-expanded": isOpen ? "true" : "false",
          onclick: () => {
            if (isOpen) onClose();
            else onOpen();
          },
        },
        [m.trust(START_FLAG_MARKUP), "Start"],
      );
      const field = m(
        "div",
        {
          class:
            "launcher-field absolute bottom-full left-0 z-(--z-content) flex min-h-9 w-(--desk-launcher-menu-width) " +
            "items-end gap-2 border bg-surface px-2.5 " +
            (isOpen ? "border-accent" : "hidden border-default"),
        },
        [
          m(
            "span",
            { class: "flex h-8.5 shrink-0 items-center text-faint" },
            m.trust(icon("search", { size: FIELD_GLYPH_SIZE })),
          ),
          m("textarea", {
            rows: 1,
            "aria-label": LAUNCHER_PLACEHOLDER,
            placeholder: LAUNCHER_PLACEHOLDER,
            value: query,
            class:
              "launcher-input min-w-0 flex-1 resize-none overflow-y-auto bg-transparent py-1.75 leading-5 " +
              "text-(length:--font-size-row) text-primary outline-none placeholder:text-faint",
            oncreate: (created: m.VnodeDOM) => {
              fit(created.dom as HTMLTextAreaElement, vnode.attrs);
              if (isOpen) (created.dom as HTMLTextAreaElement).focus();
              wasOpen = isOpen;
            },
            // The field takes the keys the moment the Start button opens the menu. A row that ran closed the menu
            // under a focused field: the focus goes with it, so the next keys do not land in the field.
            onupdate: (updated: m.VnodeDOM) => {
              const area = updated.dom as HTMLTextAreaElement;
              fit(area, vnode.attrs);
              if (vnode.attrs.isOpen && !wasOpen) area.focus();
              if (!vnode.attrs.isOpen && document.activeElement === area) area.blur();
              wasOpen = vnode.attrs.isOpen;
            },
            onremove: () => {
              if (reportedRise === 0) return;
              reportedRise = 0;
              vnode.attrs.onRise(0);
            },
            onfocus: onOpen,
            onclick: onOpen,
            oninput: (event: InputEvent) => {
              onQuery((event.target as HTMLTextAreaElement).value);
              onOpen();
            },
            onkeydown: (event: KeyboardEvent) => {
              if (event.key === "Escape") {
                // Handled here in two steps; the document's Escape (which closes the launcher) must not see it.
                event.preventDefault();
                event.stopPropagation();
                if (query !== "") onQuery("");
                else {
                  (event.target as HTMLTextAreaElement).blur();
                  onClose();
                }
                return;
              }
              if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                // With lines in the text the arrows are the caret's, as in any text area.
                if (isMessageText(query)) return;
                event.preventDefault();
                onOpen();
                vnode.attrs.onMoveHighlight(event.key === "ArrowDown" ? 1 : -1);
                return;
              }
              if (isLineBreakChord(event)) return;
              if (isSecondaryChord(event)) {
                event.preventDefault();
                vnode.attrs.onRunSecondary();
                return;
              }
              if (event.key === "Enter") {
                event.preventDefault();
                vnode.attrs.onRunHighlight();
              }
            },
          }),
          query === ""
            ? null
            : m(
                "span",
                { class: "flex h-8.5 shrink-0 items-center" },
                m(
                  Button,
                  {
                    variant: "ghost",
                    icon: true,
                    xs: true,
                    "aria-label": "Clear",
                    extra: "launcher-clear",
                    onclick: () => onQuery(""),
                  },
                  m.trust(icon("close", { size: FIELD_GLYPH_SIZE })),
                ),
              ),
        ],
      );
      // The slot spans the taskbar's height, so the field's foot (bottom-full) meets the backdrop's foot exactly
      // where the menu's bottom offset is measured from.
      return m(
        "div",
        { "data-launcher-field": "", class: "launcher-field-slot relative flex h-full shrink-0 items-center" },
        [start, field],
      );
    },
  };
}
