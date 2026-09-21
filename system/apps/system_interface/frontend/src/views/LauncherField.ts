/**
 * The launcher field at the taskbar's left (plan section 4.11): a text input whose focus opens
 * the overlay and whose typing filters it. In compact mode it collapses to an icon that expands
 * over the taskbar's entries while the overlay is open.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { icon } from "@imbue/workspace-ui/src/components/icons";

const SEARCH_PLACEHOLDER = "Search apps, windows, and templates";
const FIELD_GLYPH_SIZE = 14;

export interface LauncherFieldAttrs {
  readonly query: string;
  readonly isOpen: boolean;
  readonly isCompact: boolean;
  readonly onOpen: () => void;
  readonly onClose: () => void;
  readonly onQuery: (query: string) => void;
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
            "aria-label": SEARCH_PLACEHOLDER,
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
            "aria-label": SEARCH_PLACEHOLDER,
            placeholder: SEARCH_PLACEHOLDER,
            value: query,
            class:
              "launcher-input min-w-0 flex-1 bg-transparent text-(length:--font-size-row) text-primary outline-none " +
              "placeholder:text-faint",
            oncreate: (created: m.VnodeDOM) => {
              if (isCompact) (created.dom as HTMLInputElement).focus();
            },
            onfocus: onOpen,
            oninput: (event: InputEvent) => {
              onQuery((event.target as HTMLInputElement).value);
              onOpen();
            },
            onkeydown: (event: KeyboardEvent) => {
              if (event.key !== "Escape") return;
              // Handled here in two steps; the document's Escape (which closes the launcher) must not see it.
              event.preventDefault();
              event.stopPropagation();
              if (query !== "") onQuery("");
              else {
                (event.target as HTMLInputElement).blur();
                onClose();
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
                  "aria-label": "Clear search",
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
