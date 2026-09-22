/**
 * The launcher menu (launcher-and-getting-started plan section 4.2): the card above the taskbar's
 * field, a column of rows in which one is always highlighted. The launch-path rows come first,
 * then a divider and the window rows while typing, then a divider and the free-text rows; a
 * section with no rows draws nothing. The rows themselves come from ``reducers/launcherRows``;
 * this draws them and reports a hover (which moves the highlight) and a click (which runs a row).
 * Markers the tests use are data attributes (plan section 6.2): ``data-launcher-overlay`` on
 * the card, and on each row ``data-launcher-row``, ``data-launch``, ``data-launcher-window``,
 * ``data-text-action``, ``data-highlighted``, and ``data-disabled``.
 */

import m from "mithril";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { menuCardClass, menuDividerClass } from "@imbue/workspace-ui/src/components/menu";
import { shortcutKey } from "../model/records";
import type { LauncherMenuRows, LauncherRow, TextRow } from "../reducers/launcherRows";
import { isRowEnabled } from "../reducers/launcherRows";
import { appGlyph, glyph } from "./glyphs";

const GLYPH_SIZE = 15;
const NO_MATCH_MESSAGE = "No apps or windows match";
/** How many words of the typed text a free-text row's caption repeats. */
const PREVIEW_WORD_COUNT = 4;

export interface LauncherMenuAttrs {
  readonly menu: LauncherMenuRows;
  /** The index into ``menu.rows`` of the highlighted row; -1 for none. */
  readonly highlightIndex: number;
  readonly isCompact: boolean;
  /** Whether the secondary action's key reads as Cmd+Enter rather than Ctrl+Enter. */
  readonly isApplePlatform: boolean;
  readonly onRun: (row: LauncherRow) => void;
  readonly onHighlight: (index: number) => void;
}

/** The key that runs the secondary text action, as the platform spells it. */
export function secondaryKeyLabel(isApplePlatform: boolean): string {
  return isApplePlatform ? "Cmd+Enter" : "Ctrl+Enter";
}

/** The first words of the typed text, as a free-text row's caption repeats them. */
export function textPreview(text: string): string {
  const words = text.split(/\s+/).filter((word) => word !== "");
  if (words.length === 0) return "";
  const shown = words.slice(0, PREVIEW_WORD_COUNT).join(" ");
  return words.length > PREVIEW_WORD_COUNT ? `${shown}…` : shown;
}

const ROW_CLASS =
  "launcher-row flex h-9 w-full items-center gap-2 px-3 text-left text-(length:--font-size-row) " +
  "focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent ";
const CAPTION_CLASS = "type-helper shrink-0 truncate text-faint";
const NO_MATCH_CLASS = "launcher-no-matches m-0 px-3 py-1 text-(length:--font-size-row) text-faint";

function rowAttrs(row: LauncherRow, index: number, attrs: LauncherMenuAttrs): m.Attributes {
  const isEnabled = isRowEnabled(row);
  const isHighlighted = index === attrs.highlightIndex;
  return {
    key: row.key,
    type: "button",
    "data-launcher-row": row.key,
    "data-highlighted": isHighlighted ? "true" : "false",
    "data-disabled": isEnabled ? undefined : "true",
    "aria-disabled": isEnabled ? undefined : "true",
    class:
      ROW_CLASS +
      (isEnabled ? "cursor-pointer text-primary " : "cursor-default text-faint ") +
      (isHighlighted ? "bg-fill-active" : ""),
    onpointerenter: isEnabled ? () => attrs.onHighlight(index) : undefined,
    onclick: isEnabled ? () => attrs.onRun(row) : undefined,
  };
}

function glyphCell(markup: string): m.Vnode {
  return m("span", { class: "flex w-5 shrink-0 items-center justify-center text-faint" }, m.trust(markup));
}

function textRowCaption(row: TextRow, attrs: LauncherMenuAttrs): m.Children {
  const key = row.textAction === "primary" ? "Enter" : secondaryKeyLabel(attrs.isApplePlatform);
  const preview = textPreview(row.text);
  return [
    preview === "" ? null : m("span", { class: `${CAPTION_CLASS} max-w-1/3` }, `“${preview}”`),
    row.textAction === null ? null : m("span", { class: CAPTION_CLASS }, key),
  ];
}

function rowView(row: LauncherRow, index: number, attrs: LauncherMenuAttrs): m.Vnode {
  switch (row.kind) {
    case "launch":
      return m(
        "button",
        { ...rowAttrs(row, index, attrs), "data-launch": shortcutKey(row.app.name, row.launchPath.id) },
        [
          glyphCell(appGlyph(row.app, GLYPH_SIZE)),
          m("span", { class: "min-w-0 flex-1 truncate" }, row.label),
          row.caption === null ? null : m("span", { class: CAPTION_CLASS }, row.caption),
        ],
      );
    case "window":
      return m(
        "button",
        {
          ...rowAttrs(row, index, attrs),
          "data-launcher-window": row.window.id,
          "data-minimized": row.isMinimized ? "true" : "false",
        },
        [
          glyphCell(appGlyph(row.app, GLYPH_SIZE)),
          m("span", { class: "min-w-0 flex-1 truncate" + (row.isMinimized ? " text-faint" : "") }, row.title),
          m("span", { class: CAPTION_CLASS }, row.app?.display_name ?? row.window.app),
          row.isOnActiveDesktop ? null : m("span", { class: CAPTION_CLASS }, row.desktopName),
        ],
      );
    case "text":
      return m(
        "button",
        {
          ...rowAttrs(row, index, attrs),
          "data-launch": shortcutKey(row.app.name, row.launchPath.id),
          "data-text-action": row.textAction ?? undefined,
          ...hoverTooltipAttrs(row.disabledReason),
        },
        [
          glyphCell(glyph("plus", GLYPH_SIZE)),
          m("span", { class: "min-w-0 flex-1 truncate" }, row.label),
          textRowCaption(row, attrs),
        ],
      );
  }
}

export function LauncherMenu(): m.Component<LauncherMenuAttrs> {
  return {
    view(vnode) {
      const attrs = vnode.attrs;
      const { launchRows, windowRows, textRows, rows, isNoMatch } = attrs.menu;
      // The flat index of each section's first row, so hover and highlight speak of one list.
      const windowsFrom = launchRows.length;
      const textFrom = launchRows.length + windowRows.length;
      return m(
        "div",
        {
          "data-launcher-overlay": "",
          role: "menu",
          class: menuCardClass(
            "launcher-menu absolute bottom-0 left-2 w-(--desk-launcher-menu-width) max-h-[85%] overflow-y-auto",
          ),
        },
        [
          m(
            "div",
            { "data-section": "launch" },
            launchRows.map((row, index) => rowView(row, index, attrs)),
          ),
          windowRows.length === 0
            ? null
            : m("div", { "data-section": "windows" }, [
                launchRows.length === 0 ? null : m("div", { class: menuDividerClass() }),
                windowRows.map((row, index) => rowView(row, windowsFrom + index, attrs)),
              ]),
          textRows.length === 0
            ? null
            : m("div", { "data-section": "text" }, [
                isNoMatch ? m("p", { class: NO_MATCH_CLASS }, NO_MATCH_MESSAGE) : null,
                textFrom === 0 && !isNoMatch ? null : m("div", { class: menuDividerClass() }),
                textRows.map((row, index) => rowView(row, textFrom + index, attrs)),
              ]),
          rows.length === 0
            ? m(
                "p",
                { class: NO_MATCH_CLASS },
                isNoMatch ? NO_MATCH_MESSAGE : "No apps are registered on this machine yet.",
              )
            : null,
        ],
      );
    },
  };
}
