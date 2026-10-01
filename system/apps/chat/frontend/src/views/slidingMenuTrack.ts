/**
 * A menu's rows as the phone layout shows them: one card whose submenus slide in over its own
 * rows on a two-pane track, rather than flying out beside it. A phone has no room beside the
 * card, and no hover to open a flyout with.
 *
 * It takes the same `MenuRow`s the workspace `Menu` does and is drawn inside that menu's card
 * (as its one `custom` row), so opening, placing, the sheet under the card and Escape stay the
 * shared menu's; only the rows and the track are drawn here, in the shared menu's own chrome. A
 * submenu row slides its content in under a back row that names it, and the back row slides
 * it out again.
 */

import m from "mithril";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import {
  MENU_ROW_ATTR,
  menuDividerClass,
  menuRowClass,
  startTruncated,
  type MenuRow,
  type SubmenuRow,
} from "@imbue/workspace-ui/src/components/menu";

/** The shared menu's own cap on a submenu, ten 32px rows inside the card's border and padding, for a submenu row
 *  that sets none. */
const SUBMENU_DEFAULT_MAX_HEIGHT = 2 * 5 + 10 * 32;

const TONE_CLASS = { default: "text-primary", danger: "text-danger", quiet: "text-faint" } as const;
const KEY_VALUE_CLASS = "ml-auto flex min-w-0 items-center gap-1.5";
const KEY_SUB_CLASS = "type-helper text-faint";

export interface SlidingMenuTrackOptions {
  /** Run whenever the submenu shown changes, with its key or null: where state scoped to a submenu is reset. */
  onSubmenuChange?: (key: string | null) => void;
}

export interface SlidingMenuTrack {
  /** Slide `key`'s submenu in, or with null slide back to the menu's own rows. */
  showSubmenu(key: string | null): void;
  /** The track for `rows`. `close` closes the card, which an action row does after its pick. */
  view(rows: readonly MenuRow[], close: () => void): m.Vnode;
}

export function createSlidingMenuTrack(options: SlidingMenuTrackOptions = {}): SlidingMenuTrack {
  let shownKey: string | null = null;

  function showSubmenu(key: string | null): void {
    if (key === shownKey) return;
    shownKey = key;
    options.onSubmenuChange?.(key);
  }

  function valueText(value: string | undefined, truncate: "start" | "end" | undefined): m.Children {
    if (value === undefined) return null;
    return truncate === "start" ? startTruncated(value) : m("span", { class: "truncate" }, value);
  }

  function keyAttrs(key: string | undefined, tooltip: string | undefined): m.Attributes {
    return {
      ...(key === undefined ? {} : { [MENU_ROW_ATTR]: key }),
      ...(tooltip === undefined ? {} : hoverTooltipAttrs(tooltip, "above")),
    };
  }

  function renderRow(row: MenuRow, close: () => void): m.Children {
    switch (row.kind) {
      case "action": {
        const isDisabled = row.isDisabled === true;
        return m(
          "button",
          {
            type: "button",
            role: "menuitem",
            class: menuRowClass({
              tightGap: row.tightGap,
              extra: `${isDisabled ? "text-faint cursor-default" : TONE_CLASS[row.tone ?? "default"]} ${row.extraClass ?? ""}`,
            }),
            "aria-disabled": isDisabled ? "true" : undefined,
            ...keyAttrs(row.key, row.tooltip),
            onclick: (event: MouseEvent) => {
              event.stopPropagation();
              if (isDisabled) return;
              row.onSelect();
              if (row.keepsOpen !== true) close();
            },
          },
          [
            row.icon === undefined
              ? null
              : m(
                  "span",
                  { class: row.iconBoxClass ?? "flex w-4 shrink-0 items-center justify-center" },
                  m.trust(typeof row.icon === "string" ? icon(row.icon, { size: 14 }) : row.icon.markup),
                ),
            m("span", { class: "min-w-0 flex-1 truncate" }, row.label),
            row.trailing ?? null,
          ],
        );
      }
      case "submenu":
        return m(
          "button",
          {
            type: "button",
            role: "menuitem",
            class: menuRowClass({ extra: TONE_CLASS.default }),
            "aria-haspopup": "true",
            "aria-expanded": shownKey === row.key ? "true" : "false",
            ...keyAttrs(row.key, row.tooltip),
            onclick: (event: MouseEvent) => {
              event.stopPropagation();
              showSubmenu(row.key);
              row.onOpen?.();
            },
          },
          [
            m("span", { class: "text-secondary" }, row.label),
            m("span", { class: KEY_VALUE_CLASS }, [
              valueText(row.value, row.truncateValue),
              row.sub === undefined ? null : m("span", { class: KEY_SUB_CLASS }, `(${row.sub})`),
              m("span", { class: "shrink-0 text-faint" }, m.trust(icon("chevron-right", { size: 13 }))),
            ]),
          ],
        );
      case "value":
        return m(
          "div",
          {
            role: "menuitem",
            "aria-disabled": "true",
            class: menuRowClass({ inert: true, extra: TONE_CLASS.default }),
            ...keyAttrs(row.key, row.tooltip),
          },
          [
            m("span", { class: "text-secondary" }, row.label),
            m("span", { class: KEY_VALUE_CLASS }, [
              valueText(row.value, row.truncateValue),
              row.sub === undefined ? null : m("span", { class: KEY_SUB_CLASS }, `(${row.sub})`),
            ]),
          ],
        );
      case "check":
        return m("label", { class: menuRowClass({ extra: TONE_CLASS.default }), ...keyAttrs(row.key, row.tooltip) }, [
          m("input", { type: "checkbox", checked: row.isChecked, onchange: () => row.onToggle() }),
          row.label,
        ]);
      case "divider":
        return m("div", { role: "separator", class: menuDividerClass() });
      case "custom":
        return m("div", { [MENU_ROW_ATTR]: row.key }, row.render());
    }
  }

  /** The submenu's pane: a back row naming it, then its content, capped as the shared menu caps a flyout. */
  function submenuPane(row: SubmenuRow, close: () => void): m.Children {
    const content =
      row.content !== undefined ? row.content() : (row.rows?.() ?? []).map((child) => renderRow(child, close));
    return [
      m(
        "button",
        {
          type: "button",
          class: menuRowClass({ tightGap: true, extra: "text-secondary" }),
          "data-menu-track-back": "",
          onclick: (event: MouseEvent) => {
            event.stopPropagation();
            showSubmenu(null);
          },
        },
        [m("span", { class: "flex shrink-0" }, m.trust(icon("chevron-left", { size: 14 }))), row.label],
      ),
      m("div", { role: "separator", class: menuDividerClass() }),
      m(
        "div",
        {
          class: "flex min-h-0 flex-col overflow-hidden",
          style: `max-height: ${row.maxHeight ?? SUBMENU_DEFAULT_MAX_HEIGHT}px`,
        },
        content,
      ),
    ];
  }

  return {
    showSubmenu,
    view(rows, close) {
      const submenu =
        shownKey === null
          ? null
          : (rows.find((row): row is SubmenuRow => row.kind === "submenu" && row.key === shownKey) ?? null);
      const isSubmenuShown = submenu !== null;
      return m(
        "div",
        { class: "sliding-menu overflow-hidden", "data-menu-track": isSubmenuShown ? "submenu" : "menu" },
        m(
          "div",
          {
            class: "sliding-menu-track flex w-[200%] items-start",
            style: `transform: translateX(${isSubmenuShown ? "-50%" : "0"})`,
          },
          [
            m(
              "div",
              { class: "w-1/2 flex-none", inert: isSubmenuShown },
              rows.map((row) => renderRow(row, close)),
            ),
            // Emptied the moment the track heads back, rather than held for the slide: an empty pane sliding out
            // reads the same as a full one, and the card is back to the menu's own height at once.
            m(
              "div",
              { class: "flex w-1/2 flex-none flex-col", inert: !isSubmenuShown },
              submenu === null ? null : submenuPane(submenu, close),
            ),
          ],
        ),
      );
    },
  };
}
