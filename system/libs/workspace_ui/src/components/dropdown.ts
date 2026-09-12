/**
 * The workspace's dropdown: a form control that picks ONE value from a list.
 *
 * It looks like a menu -- the same floating card, the same rows -- and it is deliberately not
 * one. Picking an option only ever selects it: no verbs, no side effects, no controls on the
 * rows, no submenus. What it holds is the same shape a `<select>` holds, and its trigger is
 * shaped like the field beside it because that is what it is.
 *
 * It opens on a click and closes on a pick, a press outside, or Escape, behind an invisible
 * sheet like every menu -- so a press that closes it never also lands on the field underneath.
 * It portals to <body> on `--z-popover`, above the modal overlays, because the one place a
 * dropdown lives today is inside a modal.
 *
 * The Tailwind scanner reads utility names from the literals in this file: keep every utility
 * name a contiguous literal.
 */

import m from "mithril";

import { icon } from "./icons";
import { menuCardClass } from "./menu";
import { placeMenu, type MenuAnchor } from "../menu-position";
import { Portal } from "../portal";

export interface DropdownOption<V extends string> {
  value: V;
  label: string;
  /** A quieter qualifier shown beside the label on the trigger once picked -- the env var a
   *  key is saved under. */
  detail?: string;
}

export interface DropdownAttrs<V extends string> {
  options: readonly DropdownOption<V>[];
  /** The picked value, or null for none yet. */
  value: V | null;
  /** What the trigger reads while nothing is picked. */
  placeholder: string;
  onSelect: (value: V) => void;
  "aria-label"?: string;
}

/** Marks the trigger, the sheet and the list: `[data-dropdown-part="list"]`. */
export const DROPDOWN_PART_ATTR = "data-dropdown-part";
/** Marks an option by its value. */
export const DROPDOWN_OPTION_ATTR = "data-dropdown-option";

/** The field-shaped trigger. Sized and framed like a text input (see `inputClass`), since a
 *  dropdown and the field beside it read as one form. */
const TRIGGER_CLASS =
  "flex w-full items-center justify-between gap-2 rounded-md border border-default bg-surface " +
  "px-3 py-2 text-left type-body transition-[border-color] duration-(--dur-base) " +
  "hover:border-accent focus-visible:outline-2 focus-visible:outline-offset-2 " +
  "focus-visible:outline-accent cursor-pointer";
const TRIGGER_VALUE_CLASS = "flex min-w-0 items-baseline gap-2";
const TRIGGER_LABEL_CLASS = "truncate text-primary";
const TRIGGER_DETAIL_CLASS = "shrink-0 font-mono type-helper text-faint";
const TRIGGER_EMPTY_CLASS = "text-faint";
const CARET_CLASS = "shrink-0 text-faint transition-transform";
const CARET_OPEN_CLASS = "rotate-180";

/** The sheet under the open list. `dropdown-sheet` is a bare marker with no CSS attached. */
const SHEET_CLASS = "dropdown-sheet fixed inset-0 z-(--z-popover) cursor-default";
/** The list wears the shared floating-card chrome and scrolls past eight or so rows. */
const LIST_CLASS = menuCardClass("fixed max-h-[280px] overflow-y-auto overscroll-contain");
/** The shared menu row shape, minus its hover: the picked row keeps its steady accent fill, so
 *  only the idle variant hovers. */
const OPTION_CLASS =
  "flex h-8 cursor-pointer items-center justify-between gap-3 mx-1 w-[calc(100%-0.5rem)] rounded px-2 text-left " +
  "focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent";
const OPTION_IDLE_CLASS = "hover:bg-fill-hover";
const OPTION_PICKED_CLASS = "bg-accent-light";
const OPTION_LABEL_CLASS = "truncate type-body";
const OPTION_LABEL_PICKED_CLASS = "truncate type-body text-accent";

export function Dropdown<V extends string>(): m.Component<DropdownAttrs<V>> {
  // Open iff non-null: where the trigger was when it was pressed, which the list hangs under.
  let anchor: MenuAnchor | null = null;

  function onKeydown(event: KeyboardEvent): void {
    if (event.key !== "Escape" || anchor === null) return;
    // Ours alone: a dropdown open inside a modal is the thing Escape closes, not the modal.
    event.stopImmediatePropagation();
    event.preventDefault();
    close();
    m.redraw();
  }

  function open(next: MenuAnchor): void {
    if (anchor === null) window.addEventListener("keydown", onKeydown, true);
    anchor = next;
  }

  function close(): void {
    if (anchor === null) return;
    window.removeEventListener("keydown", onKeydown, true);
    anchor = null;
  }

  function list(attrs: DropdownAttrs<V>, at: MenuAnchor): m.Vnode {
    const place = (vnode: m.VnodeDOM): void => {
      const element = vnode.dom as HTMLElement;
      const rect = element.getBoundingClientRect();
      const position = placeMenu(
        at,
        { width: rect.width, height: rect.height },
        { width: window.innerWidth, height: window.innerHeight },
        "below",
      );
      element.style.left = `${position.left}px`;
      element.style.top = `${position.top}px`;
    };
    return m(
      "div",
      {
        class: LIST_CLASS,
        role: "listbox",
        [DROPDOWN_PART_ATTR]: "list",
        // As wide as the field it drops from, so the two read as one control.
        style: `left: 0; top: 0; width: ${at.width}px;`,
        oncreate: place,
        onupdate: place,
      },
      attrs.options.map((option) => {
        const isPicked = option.value === attrs.value;
        return m(
          "button",
          {
            type: "button",
            key: option.value,
            role: "option",
            "aria-selected": isPicked ? "true" : "false",
            [DROPDOWN_OPTION_ATTR]: option.value,
            class: `${OPTION_CLASS} ${isPicked ? OPTION_PICKED_CLASS : OPTION_IDLE_CLASS}`,
            onclick: (event: MouseEvent) => {
              event.stopPropagation();
              close();
              attrs.onSelect(option.value);
            },
          },
          [
            m("span", { class: isPicked ? OPTION_LABEL_PICKED_CLASS : OPTION_LABEL_CLASS }, option.label),
            isPicked ? m.trust(icon("check", { size: 15, strokeWidth: 2.5 })) : null,
          ],
        );
      }),
    );
  }

  return {
    onremove() {
      close();
    },
    view(vnode) {
      const attrs = vnode.attrs;
      const picked = attrs.options.find((option) => option.value === attrs.value) ?? null;
      const isOpen = anchor !== null;
      return [
        m(
          "button",
          {
            type: "button",
            class: TRIGGER_CLASS,
            role: "combobox",
            "aria-haspopup": "listbox",
            "aria-expanded": isOpen ? "true" : "false",
            "aria-label": attrs["aria-label"],
            [DROPDOWN_PART_ATTR]: "trigger",
            onclick: (event: MouseEvent) => {
              event.stopPropagation();
              if (isOpen) close();
              else open((event.currentTarget as HTMLElement).getBoundingClientRect());
            },
          },
          [
            picked !== null
              ? m("span", { class: TRIGGER_VALUE_CLASS }, [
                  m("span", { class: TRIGGER_LABEL_CLASS }, picked.label),
                  picked.detail === undefined ? null : m("span", { class: TRIGGER_DETAIL_CLASS }, picked.detail),
                ])
              : m("span", { class: TRIGGER_EMPTY_CLASS }, attrs.placeholder),
            m(
              "span",
              { class: `${CARET_CLASS} ${isOpen ? CARET_OPEN_CLASS : ""}` },
              m.trust(icon("chevron-down", { size: 15 })),
            ),
          ],
        ),
        anchor === null
          ? null
          : m(Portal, {
              children: [
                m("div", {
                  class: SHEET_CLASS,
                  [DROPDOWN_PART_ATTR]: "sheet",
                  // Mouse DOWN, not click: a click fires wherever the press ENDED, and selecting
                  // text in the list and releasing past its edge is not a dismissal.
                  onmousedown: (event: MouseEvent) => {
                    event.preventDefault();
                    event.stopPropagation();
                    close();
                    m.redraw();
                  },
                }),
                list(attrs, anchor),
              ],
            }),
      ];
    },
  };
}
