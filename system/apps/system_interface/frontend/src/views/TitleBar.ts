/**
 * A window's title bar (concepts.md section 2.7), left to right: the app icon, the title,
 * Refresh, the window menu (three dots, right after it), then at the right edge minimize,
 * maximize (restore when maximized), and close. Resting on the maximize control opens the
 * window's size menu, which the window menu opens under Move and resize too.
 * It is the drag handle (``data-drag-handle``);
 * a double click toggles maximize. The maximize and restore controls are hidden in compact mode,
 * where every window renders maximized. A pinned window keeps its close control too, so the
 * habit of reaching for it holds; the desktop answers it by minimizing the window.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import type { AppRecord, WindowState } from "../model/records";
import { appGlyph, glyph } from "./glyphs";

const CONTROL_GLYPH_SIZE = 14;
/** The two actions that follow the title: a smaller glyph than the window's own controls wear,
 *  in a box the same size as theirs. */
const TITLE_ACTION_GLYPH_SIZE = 12;
/** The app's own tile, taller than any glyph in the bar: it is a picture rather than a mark, and
 *  at a control's size it reads as mud. 20 in a 36 bar leaves 8 above and below. */
const APP_GLYPH_SIZE = 20;

export type WindowControl = "minimize" | "maximize" | "restore" | "close" | "menu" | "refresh";

export interface TitleBarAttrs {
  readonly title: string;
  readonly app: AppRecord | undefined;
  readonly state: WindowState;
  readonly isFocused: boolean;
  readonly isCompact: boolean;
  readonly isMenuOpen: boolean;
  /** Spread onto the maximize control: resting on it opens the window's size menu. Empty in
   *  compact mode, where there is no maximize control to rest on. */
  readonly sizeMenuTrigger: m.Attributes;
  readonly onControl: (control: WindowControl, event: MouseEvent) => void;
  readonly onDoubleClick: () => void;
}

function control(
  name: WindowControl,
  label: string,
  markup: string,
  isOpen: boolean,
  onControl: TitleBarAttrs["onControl"],
  options: { readonly extra?: string; readonly hover?: m.Attributes } = {},
): m.Vnode {
  // One box for every control in the bar, whatever size of glyph it holds: the hover boxes then
  // line up and read as one row of targets, and the pointer crosses between them without aiming.
  const extra = `window-control shrink-0 min-h-(--desk-window-control-size) min-w-(--desk-window-control-size)${
    options.extra === undefined ? "" : ` ${options.extra}`
  }`;
  return m(
    Button,
    {
      variant: "ghost",
      icon: true,
      sm: true,
      extra,
      "data-window-control": name,
      "data-no-drag": "",
      "aria-label": label,
      "aria-expanded": name === "menu" ? (isOpen ? "true" : "false") : undefined,
      // A control that opens a menu on hover says what it does in the menu; a tooltip under it
      // would land in the same place at the same moment.
      ...hoverTooltipAttrs(options.hover === undefined ? label : null),
      ...(options.hover ?? {}),
      onclick: (event: MouseEvent) => {
        event.stopPropagation();
        onControl(name, event);
      },
      ondblclick: (event: MouseEvent) => event.stopPropagation(),
    },
    m.trust(markup),
  );
}

export function TitleBar(): m.Component<TitleBarAttrs> {
  return {
    view(vnode) {
      const { title, app, state, isFocused, isCompact, isMenuOpen, sizeMenuTrigger, onControl, onDoubleClick } =
        vnode.attrs;
      const isMaximized = state === "MAXIMIZED";
      const sizing = { extra: "ml-1", hover: sizeMenuTrigger };
      return m(
        "div",
        {
          "data-drag-handle": "",
          // No gap of its own: the spacing between the leading items is uneven, so each carries
          // its own leading margin.
          class:
            "title-bar pointer-events-auto flex h-(--desk-title-bar-height) shrink-0 items-center border-b " +
            "border-default pr-1 pl-1.5 touch-none select-none " +
            // The open hand says the bar is the handle. Not in compact mode, where a window fills
            // the backdrop and there is nothing to drag it to. The controls carry their own cursor.
            (isCompact ? "" : "cursor-grab ") +
            (isFocused ? "bg-surface text-primary" : "bg-surface-secondary text-secondary"),
          ondblclick: (event: MouseEvent) => {
            if ((event.target as Element).closest("[data-window-control]") !== null) return;
            onDoubleClick();
          },
        },
        [
          // The app's icon paints itself (`docs/system/app-icons.md`), and so does the monogram an
          // app with no icon wears, so unlike every other glyph in the bar it cannot take the
          // bar's colour: an unfocused window fades it by hand instead, and further than it fades
          // the title, since a tile of colour pulls the eye harder than grey text does. The only
          // thing here that still takes the bar's colour is the built-in glyph for a window whose
          // app the registry no longer has.
          m(
            "span",
            { class: "flex shrink-0 items-center " + (isFocused ? "" : "opacity-50") },
            m.trust(appGlyph(app, APP_GLYPH_SIZE)),
          ),
          // Bold while the window is focused: the colour already says which window you are in, and
          // the weight says it from across the screen, where a shade of grey does not read. Faded
          // when it is not, so the name of a background window reads as a label rather than as
          // something to act on.
          m(
            "span",
            {
              class:
                "window-title ml-1.5 min-w-0 truncate text-(length:--font-size-row) " +
                (isFocused ? "font-bold" : "font-medium opacity-70"),
            },
            title,
          ),
          // The two title actions belong to the window you are in. An unfocused bar drops them
          // rather than fading them -- a greyed pair of buttons is still two things to look at,
          // and what is left then is the window's name and the controls every window owes you.
          // They come back with the click that focuses the window, and nothing to their right
          // moves, since the spacer beside them takes up the room.
          !isFocused
            ? null
            : control("refresh", "Refresh", icon("refresh", { size: TITLE_ACTION_GLYPH_SIZE }), false, onControl, {
                extra: "ml-2",
              }),
          !isFocused
            ? null
            : control("menu", "Window menu", glyph("kebab", TITLE_ACTION_GLYPH_SIZE), isMenuOpen, onControl, {
                extra: "ml-0.5",
              }),
          m("span", { class: "flex-1" }),
          control("minimize", "Minimize", glyph("minimize", CONTROL_GLYPH_SIZE), false, onControl),
          isCompact
            ? null
            : isMaximized
              ? control("restore", "Restore", glyph("restore", CONTROL_GLYPH_SIZE), false, onControl, sizing)
              : control("maximize", "Maximize", glyph("maximize", CONTROL_GLYPH_SIZE), false, onControl, sizing),
          control("close", "Close", icon("close", { size: CONTROL_GLYPH_SIZE }), false, onControl, { extra: "ml-1" }),
        ],
      );
    },
  };
}
