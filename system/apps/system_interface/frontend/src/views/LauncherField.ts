/**
 * The launcher field at the taskbar's left (launcher-and-getting-started plan section 4.1): a
 * one-row text area whose focus opens the menu and whose typing filters it. Shift+Enter breaks
 * the line and the field grows with its text (to a cap, then scrolls), so a longer message can be
 * written here; with a line break in it the text is a message, and the menu offers the free-text
 * rows alone. The field grows upward out of a one-row footprint in the taskbar, over the backdrop,
 * and tells its owner how far it rose so the menu can sit above it. Its keys are the menu's (plan
 * section 4.8): the arrows move the highlight (the caret, once the text has lines), Enter runs the
 * highlight, Ctrl+Enter (Cmd+Enter on a Mac) runs the secondary text action, and Escape clears the
 * text, then closes. In compact mode it collapses to an icon that expands over the taskbar's
 * entries while the menu is open.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import { isMessageText } from "../reducers/launcherRows";

export const LAUNCHER_PLACEHOLDER = "Start app or send message...";
const FIELD_GLYPH_SIZE = 14;
/** Past this many lines the field scrolls rather than growing. */
export const MAX_FIELD_LINES = 8;

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
  /** How far the field stands above its one-row height, in px, each time that changes. */
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

/** Size the text area to its text: one line until it has more, and at most MAX_FIELD_LINES before it scrolls.
 *  Answers how far it stands above one row, in px; 0 where nothing can be measured (a DOM without layout). */
function fitField(field: HTMLTextAreaElement): number {
  field.style.height = "";
  const lineHeight = Number.parseFloat(getComputedStyle(field).lineHeight);
  if (field.scrollHeight === 0 || Number.isNaN(lineHeight)) return 0;
  // With no height set, the one-row text area is its line plus its padding.
  const oneRowHeight = field.offsetHeight;
  const cap = lineHeight * MAX_FIELD_LINES + Math.max(0, oneRowHeight - lineHeight);
  const height = Math.min(Math.max(field.scrollHeight, oneRowHeight), cap);
  field.style.height = `${height}px`;
  return height - oneRowHeight;
}

export function LauncherField(): m.Component<LauncherFieldAttrs> {
  // The rise last told to the owner, told again only when it changes (a redraw follows each telling).
  let reportedRise = 0;

  function fit(area: HTMLTextAreaElement, attrs: LauncherFieldAttrs): void {
    const rise = fitField(area);
    if (rise === reportedRise) return;
    reportedRise = rise;
    attrs.onRise(rise);
  }

  return {
    view(vnode) {
      const { query, isOpen, isCompact, onOpen, onClose, onQuery } = vnode.attrs;
      if (isCompact && !isOpen) {
        return m(
          Button,
          {
            variant: "ghost",
            icon: true,
            "aria-label": LAUNCHER_PLACEHOLDER,
            extra: "launcher-field-toggle shrink-0",
            "data-launcher-field": "",
            onclick: onOpen,
          },
          m.trust(icon("search", { size: FIELD_GLYPH_SIZE })),
        );
      }
      const field = m(
        "div",
        {
          "data-launcher-field": "",
          class:
            "launcher-field absolute bottom-0 flex min-h-9 items-end gap-2 rounded-lg border bg-surface px-2.5 " +
            (isOpen ? "border-accent " : "border-default ") +
            (isCompact ? "inset-x-2 z-(--z-content)" : "inset-x-0"),
        },
        [
          m(
            "span",
            { class: "flex h-9 shrink-0 items-center text-faint" },
            m.trust(icon("search", { size: FIELD_GLYPH_SIZE })),
          ),
          m("textarea", {
            rows: 1,
            "aria-label": LAUNCHER_PLACEHOLDER,
            placeholder: LAUNCHER_PLACEHOLDER,
            value: query,
            class:
              "launcher-input min-w-0 flex-1 resize-none overflow-y-auto bg-transparent py-2 leading-5 " +
              "text-(length:--font-size-row) text-primary outline-none placeholder:text-faint",
            oncreate: (created: m.VnodeDOM) => {
              fit(created.dom as HTMLTextAreaElement, vnode.attrs);
              if (isCompact) (created.dom as HTMLTextAreaElement).focus();
            },
            // A row that ran closed the menu under a focused field: the focus goes with it, so the next keys do not
            // land in the field, and a click on the field (already focused, so no focus event) opens the menu again.
            onupdate: (updated: m.VnodeDOM) => {
              fit(updated.dom as HTMLTextAreaElement, vnode.attrs);
              if (!vnode.attrs.isOpen && document.activeElement === updated.dom)
                (updated.dom as HTMLTextAreaElement).blur();
            },
            // The compact field collapses with the menu, and nothing stands above one row then.
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
                { class: "flex h-9 shrink-0 items-center" },
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
      if (isCompact) return field;
      // In the taskbar's flow the field keeps a one-row footprint and grows upward out of it, so the taskbar's
      // height and its entries' places hold whatever the text's length.
      return m("div", { class: "launcher-field-slot relative h-9 w-72 max-w-[40vw] shrink-0" }, field);
    },
  };
}
