/**
 * A window's title bar (concepts.md section 2.7), drawn from the theme's chrome slots
 * (docs/system/blueprint/workspace-themes/, section 4.3): the leading slots from the left edge, the trailing ones
 * ending at the right. The standard look is the app icon, the title, Refresh, and the window menu (three dots),
 * then minimize, maximize (restore when maximized), and close at the right edge; a theme may rearrange them and
 * centre the title, never drop a control. Resting on the maximize control opens the window's size menu, which the
 * window menu opens under Move and resize too. It is the drag handle (``data-drag-handle``); a double click toggles
 * maximize. A pinned window keeps its close control too, so the habit of reaching for it holds; the desktop answers
 * it by minimizing the window.
 */

import m from "mithril";
import { partAttrs } from "@imbue/workspace-ui/src/themes/parts";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import type { AppRecord, WindowState } from "../model/records";
import type { ChromeSlot, ThemeChrome } from "../model/themes";
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
  readonly isMenuOpen: boolean;
  /** Spread onto the maximize control: resting on it opens the window's size menu. */
  readonly sizeMenuTrigger: m.Attributes;
  readonly onControl: (control: WindowControl, event: MouseEvent) => void;
  readonly onDoubleClick: () => void;
  /** The theme's arrangement of the bar. */
  readonly chrome: ThemeChrome;
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
  // Above the window's resize handles, whose corners reach into the title bar: a press on a control is the control's,
  // however close a theme sets it to the window's corner.
  const extra = `window-control relative z-1 shrink-0 min-h-(--desk-window-control-size) min-w-(--desk-window-control-size)${
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
      ...partAttrs("window-control"),
      "data-control": name,
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

/** The space before each slot in the standard look, where the slot is not the first of its run: the spacing
 *  between the leading items is uneven, so each carries its own leading margin. */
const SLOT_MARGIN: Readonly<Record<ChromeSlot, string>> = {
  icon: "",
  title: "ml-1.5",
  refresh: "ml-2",
  menu: "ml-0.5",
  minimize: "",
  maximize: "ml-1",
  close: "ml-1",
};

function slotMargin(slot: ChromeSlot, isFirst: boolean): string {
  return isFirst ? "" : SLOT_MARGIN[slot];
}

export function TitleBar(): m.Component<TitleBarAttrs> {
  return {
    view(vnode) {
      const { title, app, state, isFocused, isMenuOpen, sizeMenuTrigger, onControl, onDoubleClick, chrome } =
        vnode.attrs;
      const isMaximized = state === "MAXIMIZED";
      const isTitleCentered = chrome.title_align === "center";

      function slot(name: ChromeSlot, isFirst: boolean): m.Children {
        const margin = slotMargin(name, isFirst);
        switch (name) {
          case "icon":
            // The app's icon paints itself (`docs/system/app-icons.md`), and so does the monogram an app with no
            // icon wears, so unlike every other glyph in the bar it cannot take the bar's colour: an unfocused
            // window fades it by hand instead, and further than it fades the title, since a tile of colour pulls
            // the eye harder than grey text does.
            return m(
              "span",
              {
                ...partAttrs("window-icon"),
                class: `flex shrink-0 items-center ${margin} ${isFocused ? "" : "opacity-50"}`,
              },
              m.trust(appGlyph(app, APP_GLYPH_SIZE)),
            );
          case "title":
            // A centred title is laid over the middle of the bar instead, below.
            return isTitleCentered ? null : titleSpan(margin);
          // The two title actions belong to the window you are in. An unfocused bar drops them rather than
          // fading them -- a greyed pair of buttons is still two things to look at -- and they come back with
          // the click that focuses the window.
          case "refresh":
            return isFocused
              ? control("refresh", "Refresh", icon("refresh", { size: TITLE_ACTION_GLYPH_SIZE }), false, onControl, {
                  extra: margin,
                })
              : null;
          case "menu":
            return isFocused
              ? control("menu", "Window menu", glyph("kebab", TITLE_ACTION_GLYPH_SIZE), isMenuOpen, onControl, {
                  extra: margin,
                })
              : null;
          case "minimize":
            return control("minimize", "Minimize", glyph("minimize", CONTROL_GLYPH_SIZE), false, onControl, {
              extra: margin,
            });
          case "maximize":
            return isMaximized
              ? control("restore", "Restore", glyph("restore", CONTROL_GLYPH_SIZE), false, onControl, {
                  extra: margin,
                  hover: sizeMenuTrigger,
                })
              : control("maximize", "Maximize", glyph("maximize", CONTROL_GLYPH_SIZE), false, onControl, {
                  extra: margin,
                  hover: sizeMenuTrigger,
                });
          case "close":
            return control("close", "Close", icon("close", { size: CONTROL_GLYPH_SIZE }), false, onControl, {
              extra: margin,
            });
        }
      }

      // Bold while the window is focused: the colour already says which window you are in, and the weight says
      // it from across the screen, where a shade of grey does not read. Faded when it is not, so the name of a
      // background window reads as a label rather than as something to act on.
      function titleSpan(margin: string): m.Vnode {
        return m(
          "span",
          {
            ...partAttrs("window-title"),
            class:
              `window-title ${margin} min-w-0 truncate text-(length:--font-size-row) ` +
              (isFocused ? "font-bold" : "font-medium opacity-70") +
              (isTitleCentered ? " pointer-events-none absolute left-1/2 max-w-[60%] -translate-x-1/2" : ""),
          },
          title,
        );
      }

      return m(
        "div",
        {
          "data-drag-handle": "",
          ...partAttrs("title-bar"),
          "data-focused": isFocused ? "true" : "false",
          class:
            "title-bar pointer-events-auto relative flex h-(--desk-title-bar-height) shrink-0 items-center border-b " +
            "border-default pr-1 pl-1.5 touch-none select-none " +
            // The open hand says the bar is the handle. The controls carry their own cursor.
            "cursor-grab " +
            (isFocused ? "bg-surface text-primary" : "bg-surface-secondary text-secondary"),
          ondblclick: (event: MouseEvent) => {
            if ((event.target as Element).closest("[data-window-control]") !== null) return;
            onDoubleClick();
          },
        },
        [
          ...chrome.leading.map((name, index) => slot(name, index === 0)),
          m("span", { class: "flex-1" }),
          ...chrome.trailing.map((name, index) => slot(name, index === 0)),
          isTitleCentered ? titleSpan("") : null,
        ],
      );
    },
  };
}
