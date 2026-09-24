/**
 * The size menu's one row: a heading over a grid of pictogram tiles, each placing the window in
 * one zone of the backdrop -- the whole of it, the four halves, the four quarters. It is the
 * body of the menu the maximize control opens on hover, and the first section of the window's
 * own menu, so the two offer the identical choices off the identical rule.
 *
 * The three zones a window STATE stands for (maximized and the two side halves) are placed by
 * setting that state, so they keep the behaviour a drag to the edge gives them -- restoring
 * returns to the frame the window kept. The rest are placed as plain frames.
 */

import m from "mithril";
import type { CustomRow } from "@imbue/workspace-ui/src/components/menu";
import {
  BOTTOM_HALF_FRAME,
  BOTTOM_LEFT_QUARTER_FRAME,
  BOTTOM_RIGHT_QUARTER_FRAME,
  MAXIMIZED_FRAME,
  SNAPPED_LEFT_FRAME,
  SNAPPED_RIGHT_FRAME,
  TOP_HALF_FRAME,
  TOP_LEFT_QUARTER_FRAME,
  TOP_RIGHT_QUARTER_FRAME,
} from "../geometry/frames";
import type { Frame, WindowState } from "../model/records";
import { zoneGlyph } from "./glyphs";

/** The pictogram's box inside its tile. */
const ZONE_GLYPH_SIZE = 22;

export interface WindowSizeActions {
  /** Place the window in a zone one of the window states stands for. */
  readonly setState: (state: WindowState) => void;
  /** Place the window at a fraction of the backdrop, normal and raised. */
  readonly setFrame: (frame: Frame) => void;
}

/** One tile: what it looks like, what it reads, and what it does. */
interface WindowZone {
  readonly key: string;
  readonly label: string;
  readonly frame: Frame;
  /** The state that stands for this zone, or null for a zone placed as a plain frame. */
  readonly state: WindowState | null;
}

/** The zones in the order they are offered: the whole backdrop, the four halves, the four quarters. */
export const WINDOW_ZONES: readonly WindowZone[] = [
  { key: "full", label: "Full window", frame: MAXIMIZED_FRAME, state: "MAXIMIZED" },
  { key: "left-half", label: "Left half", frame: SNAPPED_LEFT_FRAME, state: "SNAPPED_LEFT" },
  { key: "right-half", label: "Right half", frame: SNAPPED_RIGHT_FRAME, state: "SNAPPED_RIGHT" },
  { key: "top-half", label: "Top half", frame: TOP_HALF_FRAME, state: null },
  { key: "bottom-half", label: "Bottom half", frame: BOTTOM_HALF_FRAME, state: null },
  { key: "top-left-quarter", label: "Top left quarter", frame: TOP_LEFT_QUARTER_FRAME, state: null },
  { key: "top-right-quarter", label: "Top right quarter", frame: TOP_RIGHT_QUARTER_FRAME, state: null },
  { key: "bottom-left-quarter", label: "Bottom left quarter", frame: BOTTOM_LEFT_QUARTER_FRAME, state: null },
  { key: "bottom-right-quarter", label: "Bottom right quarter", frame: BOTTOM_RIGHT_QUARTER_FRAME, state: null },
];

function placeIn(zone: WindowZone, actions: WindowSizeActions): void {
  if (zone.state === null) actions.setFrame(zone.frame);
  else actions.setState(zone.state);
}

/** The row: the heading over the tiles, five to a line so the whole-and-halves and the quarters
 *  each read as their own line. */
export function windowSizeRow(actions: WindowSizeActions, onPlaced: () => void): CustomRow {
  return {
    kind: "custom",
    key: "size",
    render: () =>
      // Tighter than a menu row's own slab: the tiles are the content, and the card's edge should
      // sit near their hover boxes rather than at a text row's indent.
      m("div", { class: "mx-1 flex flex-col gap-1 px-1 py-1" }, [
        m("span", { class: "text-(length:--font-size-helper) text-primary" }, "Move and resize"),
        m(
          "div",
          { class: "grid w-max grid-cols-5 gap-1" },
          WINDOW_ZONES.map((zone) =>
            m(
              "button",
              {
                key: zone.key,
                type: "button",
                "data-window-zone": zone.key,
                "aria-label": zone.label,
                title: zone.label,
                class:
                  "flex h-8 w-8 cursor-pointer items-center justify-center rounded-lg text-secondary " +
                  "hover:bg-fill-hover hover:text-primary focus-visible:outline-2 focus-visible:outline-accent",
                onclick: (event: MouseEvent) => {
                  event.stopPropagation();
                  placeIn(zone, actions);
                  onPlaced();
                },
              },
              m.trust(zoneGlyph(zone.frame, ZONE_GLYPH_SIZE)),
            ),
          ),
        ),
      ]),
  };
}
