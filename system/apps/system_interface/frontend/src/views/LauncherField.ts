/**
 * The launcher field at the taskbar's left (launcher-and-getting-started plan section 4.1): a
 * text input whose focus opens the menu and whose typing filters it. Its keys are the menu's
 * (plan section 4.8): the arrows move the highlight, Enter runs it, Ctrl+Enter (Cmd+Enter on a
 * Mac) runs the secondary text action, and Escape clears the text, then closes. In compact mode
 * it collapses to an icon that expands over the taskbar's entries while the menu is open.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { icon } from "@imbue/workspace-ui/src/components/icons";

export const LAUNCHER_PLACEHOLDER = "Start app or send message...";
const FIELD_GLYPH_SIZE = 14;

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
}

/** Whether a key press is the secondary action's chord: Enter with Ctrl, or with Cmd on a Mac. */
export function isSecondaryChord(event: Pick<KeyboardEvent, "key" | "ctrlKey" | "metaKey">): boolean {
  return event.key === "Enter" && (event.ctrlKey || event.metaKey);
}

export function LauncherField(): m.Component<LauncherFieldAttrs> {
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
      return m(
        "div",
        {
          "data-launcher-field": "",
          class:
            "launcher-field flex h-9 items-center gap-2 rounded-lg border bg-surface px-2.5 " +
            (isOpen ? "border-accent " : "border-default ") +
            (isCompact ? "absolute inset-x-2 z-(--z-content)" : "w-72 max-w-[40vw] shrink-0"),
        },
        [
          m(
            "span",
            { class: "flex shrink-0 items-center text-faint" },
            m.trust(icon("search", { size: FIELD_GLYPH_SIZE })),
          ),
          m("input", {
            type: "text",
            "aria-label": LAUNCHER_PLACEHOLDER,
            placeholder: LAUNCHER_PLACEHOLDER,
            value: query,
            class:
              "launcher-input min-w-0 flex-1 bg-transparent text-(length:--font-size-row) text-primary outline-none " +
              "placeholder:text-faint",
            oncreate: (created: m.VnodeDOM) => {
              if (isCompact) (created.dom as HTMLInputElement).focus();
            },
            // A row that ran closed the menu under a focused field: the focus goes with it, so the next keys do not
            // land in the field, and a click on the field (already focused, so no focus event) opens the menu again.
            onupdate: (updated: m.VnodeDOM) => {
              if (!vnode.attrs.isOpen && document.activeElement === updated.dom)
                (updated.dom as HTMLInputElement).blur();
            },
            onfocus: onOpen,
            onclick: onOpen,
            oninput: (event: InputEvent) => {
              onQuery((event.target as HTMLInputElement).value);
              onOpen();
            },
            onkeydown: (event: KeyboardEvent) => {
              if (event.key === "Escape") {
                // Handled here in two steps; the document's Escape (which closes the launcher) must not see it.
                event.preventDefault();
                event.stopPropagation();
                if (query !== "") onQuery("");
                else {
                  (event.target as HTMLInputElement).blur();
                  onClose();
                }
                return;
              }
              if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                event.preventDefault();
                onOpen();
                vnode.attrs.onMoveHighlight(event.key === "ArrowDown" ? 1 : -1);
                return;
              }
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
        ],
      );
    },
  };
}
