/**
 * The launcher menu (launcher-and-getting-started plan section 4.2): the card above the taskbar's
 * field, a column of rows in which one is always highlighted. The launch-path rows come first,
 * then a divider and the window rows while typing, then a divider and the free-text rows; a
 * section with no rows draws nothing. The rows themselves come from ``reducers/launcherRows``;
 * this draws them and reports a hover (which moves the highlight) and a click (which runs a row).
 * The highlighted row, whatever its kind, carries the ``Enter`` caption; the secondary text row
 * carries its chord's.
 * Markers the tests use are data attributes (plan section 6.2): ``data-launcher-overlay`` on
 * the card, and on each row ``data-launcher-row``, ``data-launch``, ``data-launcher-window``,
 * ``data-text-action``, ``data-highlighted``, and ``data-disabled``.
 * The phone's start sheet draws the same sections (``LauncherSections``) with rows sized for a
 * finger and no key captions, since its rows are tapped rather than chosen from a keyboard.
 */

import m from "mithril";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import {
  MENU_ROW_FOCUS,
  MENU_ROW_SLAB,
  menuCardClass,
  menuDividerClass,
} from "@imbue/workspace-ui/src/components/menu";
import { shortcutKey } from "../model/records";
import type { LauncherMenuRows, LauncherRow, TextRow } from "../reducers/launcherRows";
import { isRowEnabled } from "../reducers/launcherRows";
import { appGlyph, glyph } from "./glyphs";

/** An app's icon in a row, filling the cell that holds it, so its tile reads as the tile it wears
 *  everywhere else. */
const APP_GLYPH_SIZE = 24;
/** A mark of the chrome's own in the same cell -- the plus a free-text row wears. Smaller than a
 *  tile: it is a stroke on the surface, not a picture sitting on it. */
const MARK_GLYPH_SIZE = 20;
/** The same two in a sheet's taller row. */
const SHEET_APP_GLYPH_SIZE = 32;
const SHEET_MARK_GLYPH_SIZE = 22;
const NO_MATCH_MESSAGE = "No apps or windows match";
/** How many words of the typed text a free-text row's caption repeats. */
const PREVIEW_WORD_COUNT = 4;

export interface LauncherSectionsAttrs {
  readonly menu: LauncherMenuRows;
  /** The index into ``menu.rows`` of the highlighted row; -1 for none. */
  readonly highlightIndex: number;
  /** Whether the secondary action's key reads as Cmd+Enter rather than Ctrl+Enter. */
  readonly isApplePlatform: boolean;
  /** Drawn in the phone's start sheet: rows sized for a finger, and no key captions. */
  readonly isSheet: boolean;
  readonly onRun: (row: LauncherRow) => void;
  readonly onHighlight: (index: number) => void;
}

export interface LauncherMenuAttrs extends Omit<LauncherSectionsAttrs, "isSheet"> {
  /** How far above the backdrop's foot the card's foot sits, in px: the field's rise above its one row. */
  readonly bottomOffsetPx: number;
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

// The shared menu's own row recipe, taken rather than copied so the two cannot drift: an inset slab
// with the menu's corner, and its focus ring. The height is this menu's -- its rows carry an app's
// tile, where a menu row carries a glyph.
const ROW_CLASS =
  `launcher-row flex h-10 items-center gap-2 text-left text-(length:--font-size-body) ` +
  `${MENU_ROW_SLAB} ${MENU_ROW_FOCUS} `;
const SHEET_ROW_CLASS =
  `launcher-row flex h-(--desk-phone-row-height) items-center gap-3 text-left text-(length:--font-size-body) ` +
  `${MENU_ROW_SLAB} ${MENU_ROW_FOCUS} `;
const CAPTION_CLASS = "type-helper shrink-0 truncate text-faint";
const ENTER_KEY_LABEL = "Enter";
const NO_MATCH_CLASS = "launcher-no-matches my-0 mx-3 py-1 text-(length:--font-size-body) text-faint";

function rowAttrs(row: LauncherRow, index: number, attrs: LauncherSectionsAttrs): m.Attributes {
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
      (attrs.isSheet ? SHEET_ROW_CLASS : ROW_CLASS) +
      (isEnabled ? "cursor-pointer text-primary " : "cursor-default text-faint ") +
      // The highlight is the menu's hover tint: the arrow keys move the same mark the pointer does.
      (isHighlighted ? "bg-fill-hover" : ""),
    onpointerenter: isEnabled ? () => attrs.onHighlight(index) : undefined,
    onclick: isEnabled ? () => attrs.onRun(row) : undefined,
  };
}

function glyphCell(markup: string, isSheet: boolean): m.Vnode {
  return m(
    "span",
    { class: `flex shrink-0 items-center justify-center text-faint ${isSheet ? "size-8" : "size-6"}` },
    m.trust(markup),
  );
}

/** The key captions a row wears: the secondary text row's chord, and ``Enter`` on the highlighted row; none in a
 *  sheet, whose rows are tapped. */
function keyCaptions(row: LauncherRow, isHighlighted: boolean, attrs: LauncherSectionsAttrs): m.Children {
  if (attrs.isSheet) return null;
  return [
    row.kind === "text" && row.textAction === "secondary"
      ? m("span", { class: CAPTION_CLASS }, secondaryKeyLabel(attrs.isApplePlatform))
      : null,
    isHighlighted ? m("span", { class: CAPTION_CLASS, "data-key": "enter" }, ENTER_KEY_LABEL) : null,
  ];
}

function textRowCaption(row: TextRow): m.Children {
  const preview = textPreview(row.text);
  return preview === "" ? null : m("span", { class: `${CAPTION_CLASS} max-w-1/3` }, `“${preview}”`);
}

function rowView(row: LauncherRow, index: number, attrs: LauncherSectionsAttrs): m.Vnode {
  const keys = keyCaptions(row, index === attrs.highlightIndex, attrs);
  const appSize = attrs.isSheet ? SHEET_APP_GLYPH_SIZE : APP_GLYPH_SIZE;
  const markSize = attrs.isSheet ? SHEET_MARK_GLYPH_SIZE : MARK_GLYPH_SIZE;
  switch (row.kind) {
    case "launch":
      return m(
        "button",
        { ...rowAttrs(row, index, attrs), "data-launch": shortcutKey(row.app.name, row.launchPath.id) },
        [
          glyphCell(appGlyph(row.app, appSize), attrs.isSheet),
          m("span", { class: "min-w-0 flex-1 truncate" }, row.label),
          row.caption === null ? null : m("span", { class: CAPTION_CLASS }, row.caption),
          keys,
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
          glyphCell(appGlyph(row.app, appSize), attrs.isSheet),
          // A phone names no desktop and places what it shows minimized, so the sheet carries neither mark.
          m(
            "span",
            { class: "min-w-0 flex-1 truncate" + (row.isMinimized && !attrs.isSheet ? " text-faint" : "") },
            row.title,
          ),
          m("span", { class: CAPTION_CLASS }, row.app?.display_name ?? row.window.app),
          row.isOnActiveDesktop || attrs.isSheet ? null : m("span", { class: CAPTION_CLASS }, row.desktopName),
          keys,
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
          glyphCell(glyph("plus", markSize), attrs.isSheet),
          m("span", { class: "min-w-0 flex-1 truncate" }, row.label),
          textRowCaption(row),
          keys,
        ],
      );
  }
}

/** The menu's sections, launch-path rows then window rows then free-text rows, each after a divider. */
function launcherSections(attrs: LauncherSectionsAttrs): m.Children {
  const { launchRows, windowRows, textRows, rows, isNoMatch } = attrs.menu;
  // The flat index of each section's first row, so hover and highlight speak of one list.
  const windowsFrom = launchRows.length;
  const textFrom = launchRows.length + windowRows.length;
  return [
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
      ? m("p", { class: NO_MATCH_CLASS }, isNoMatch ? NO_MATCH_MESSAGE : "No apps are registered on this machine yet.")
      : null,
  ];
}

/** The sections alone, for a surface that frames them itself. */
export const LauncherSections: m.Component<LauncherSectionsAttrs> = {
  view: (vnode) => launcherSections(vnode.attrs),
};

export function LauncherMenu(): m.Component<LauncherMenuAttrs> {
  return {
    view(vnode) {
      const attrs = vnode.attrs;
      return m(
        "div",
        {
          "data-launcher-overlay": "",
          role: "menu",
          class: menuCardClass(
            "launcher-menu absolute left-2 w-(--desk-launcher-menu-width) max-h-[85%] overflow-y-auto",
          ),
          // Anchored above the field (plan section 4.2): the card rises with a field that grew past one row.
          style: { bottom: `${attrs.bottomOffsetPx}px` },
        },
        launcherSections({ ...attrs, isSheet: false }),
      );
    },
  };
}
