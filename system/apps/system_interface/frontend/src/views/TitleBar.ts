/**
 * A window's title bar (concepts.md section 2.7; the multiplayer spec, section 9.1), drawn from the theme's
 * chrome slots (docs/system/blueprint/workspace-themes/, section 4.3): the leading slots from the left edge, the
 * trailing ones ending at the right. The standard look is, while the window is focused, the window menu (three
 * dots), Refresh, "Chat about this app" (a speech balloon, on every window of an app that is not internal), and
 * Share (the app's share settings in the Imbue Studio chrome: for the workspace's owner, under a chrome that can
 * open them, on every window of an app that is not internal); the app icon and the title centred over the bar;
 * then minimize, maximize (fullscreen and restore when maximized: the state control cycles on from maximized to
 * the whole viewport, the Screen spec's L2 layer), and close at the right edge. Share wears its word beside its
 * glyph on a bar wide enough to keep the word clear of the centred title, and its glyph alone on a narrower one
 * (``isShareLabelShown``). A theme may rearrange the slots and align the title its own way, never drop a slot's
 * control; the balloon and Share follow Refresh wherever the theme puts it, and a theme whose controls have no
 * room for a word hides Share, which its window menu still offers. Resting on the maximize control opens the
 * window's size menu, which the window menu opens under Move and resize too. It is the drag handle
 * (``data-drag-handle``); a double click toggles maximize. A pinned window keeps its close control too, so the
 * habit of reaching for it holds; the desktop answers it by minimizing the window.
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

export type WindowControl =
  "minimize" | "maximize" | "fullscreen" | "restore" | "close" | "menu" | "refresh" | "app-chat" | "share";

const APP_CHAT_LABEL = "Chat about this app";
const SHARE_LABEL = "Share";

/** The bar widths from which Share wears its word. The centred title may take three fifths of the bar, so only the
 *  fifth at each end is sure to be clear of it, and the run that ends in the labelled Share (the bar's inset, the
 *  window menu, Refresh, the balloon, then Share and its word, at the standard look's sizes) has to fit in that
 *  fifth with a little air after it. A touch client is asked for the width that run takes with every control as
 *  wide as the touch target. These are breakpoints, as the phone's is, rather than metrics a theme sets. */
const SHARE_LABEL_MIN_BAR_WIDTH = 920;
const SHARE_LABEL_MIN_TOUCH_BAR_WIDTH = 1160;

/** Whether Share wears its word on a bar this wide, or its glyph alone. */
export function isShareLabelShown(barWidth: number, isTouch: boolean): boolean {
  return barWidth >= (isTouch ? SHARE_LABEL_MIN_TOUCH_BAR_WIDTH : SHARE_LABEL_MIN_BAR_WIDTH);
}

export interface TitleBarAttrs {
  readonly title: string;
  readonly app: AppRecord | undefined;
  readonly state: WindowState;
  readonly isFocused: boolean;
  readonly isMenuOpen: boolean;
  /** Whether the bar offers "Chat about this app": an app that is not internal, on a machine with an app-chat launch
   *  path (the multiplayer spec, section 9.1); absent reads as not offered. */
  readonly hasAppChat?: boolean;
  /** Whether the bar offers Share: an app that can be shared on its own (``isAppShareable``), to the workspace's
   *  owner, under an Imbue Studio chrome that opens its share settings; absent reads as not offered. */
  readonly hasShare?: boolean;
  /** The bar's width, which is the window's, and whether the client is a touch one: Share wears its word only
   *  where the two leave it room (``isShareLabelShown``). An absent width reads as too narrow for the word. */
  readonly width?: number;
  readonly isTouch?: boolean;
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
  options: {
    readonly extra?: string;
    readonly hover?: m.Attributes;
    /** A word beside the glyph, which makes the control a labelled button as wide as the two need. */
    readonly text?: string;
  } = {},
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
      icon: options.text === undefined,
      sm: true,
      quiet: options.text !== undefined,
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
    options.text === undefined
      ? m.trust(markup)
      : // In the title's size and a lighter weight than its bold: the word belongs to the bar's type without
        // competing with the window's name.
        [m.trust(markup), m("span", { class: "text-(length:--font-size-row)" }, options.text)],
  );
}

/** The space before each slot in the standard look, where the slot is not the first of its run: the spacing
 *  between the leading items is uneven, so each carries its own leading margin. */
const SLOT_MARGIN: Readonly<Record<ChromeSlot, string>> = {
  icon: "",
  title: "ml-1.5",
  refresh: "ml-0.5",
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
      const {
        title,
        app,
        state,
        isFocused,
        isMenuOpen,
        hasAppChat,
        hasShare,
        width,
        isTouch,
        sizeMenuTrigger,
        onControl,
        onDoubleClick,
        chrome,
      } = vnode.attrs;
      const isMaximized = state === "MAXIMIZED";
      const isTitleCentered = chrome.title_align === "center";

      // The two title actions belong to the window you are in. An unfocused bar drops them rather than fading
      // them -- a greyed pair of buttons is still two things to look at -- and they come back with the click that
      // focuses the window. A centred title is laid over the middle of the bar instead, below. A slot not drawn
      // leaves no gap: the first one drawn in its run takes no margin. It still holds its place in the bar's
      // children, so focusing a window with a press on one of its controls does not shift the controls under the
      // pointer before the release.
      function isSlotShown(name: ChromeSlot): boolean {
        switch (name) {
          case "icon":
          case "title":
            return !isTitleCentered;
          case "refresh":
          case "menu":
            return isFocused;
          case "minimize":
          case "maximize":
          case "close":
            return true;
        }
      }

      function slotRun(names: readonly ChromeSlot[]): m.Children[] {
        const firstShown = names.find(isSlotShown);
        return names.map((name) => (isSlotShown(name) ? slot(name, name === firstShown) : null));
      }

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
            return titleSpan(margin);
          case "refresh": {
            const refresh = control(
              "refresh",
              "Refresh",
              icon("refresh", { size: TITLE_ACTION_GLYPH_SIZE }),
              false,
              onControl,
              {
                extra: margin,
              },
            );
            // The balloon follows Refresh wherever the theme puts it (the multiplayer spec, section 9.1), and Share
            // follows the balloon.
            return [
              refresh,
              hasAppChat
                ? control("app-chat", APP_CHAT_LABEL, glyph("balloon", TITLE_ACTION_GLYPH_SIZE), false, onControl, {
                    extra: "ml-0.5",
                  })
                : null,
              hasShare
                ? control(
                    "share",
                    app === undefined ? SHARE_LABEL : `${SHARE_LABEL} ${app.display_name}`,
                    icon("share", { size: TITLE_ACTION_GLYPH_SIZE }),
                    false,
                    onControl,
                    {
                      extra: "ml-0.5",
                      text:
                        width !== undefined && isShareLabelShown(width, isTouch === true) ? SHARE_LABEL : undefined,
                    },
                  )
                : null,
            ];
          }
          case "menu":
            return control("menu", "Window menu", glyph("kebab", TITLE_ACTION_GLYPH_SIZE), isMenuOpen, onControl, {
              extra: margin,
            });
          case "minimize":
            return control("minimize", "Minimize", glyph("minimize", CONTROL_GLYPH_SIZE), false, onControl, {
              extra: margin,
            });
          case "maximize":
            // Maximized, the slot cycles on: Fullscreen fills the whole viewport, Restore brings the frame back.
            return isMaximized
              ? [
                  control("fullscreen", "Fullscreen", glyph("fullscreen", CONTROL_GLYPH_SIZE), false, onControl, {
                    extra: margin,
                  }),
                  control("restore", "Restore", glyph("restore", CONTROL_GLYPH_SIZE), false, onControl, {
                    hover: sizeMenuTrigger,
                  }),
                ]
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
              (isFocused ? "font-bold" : "font-medium opacity-70"),
          },
          title,
        );
      }

      /** The centred icon and title (the multiplayer spec, section 9.1): laid over the middle of the bar, the icon
       *  only when the theme names its slot, which the runs then leave out. */
      function centeredGroup(): m.Vnode {
        const hasIconSlot = chrome.leading.includes("icon") || chrome.trailing.includes("icon");
        // With no icon slot the title part itself is the positioned element, as it was before the group: a theme
        // styles the part as the one centred patch over the bar (mac-classic's white patch over the stripes).
        if (!hasIconSlot) {
          return m(
            "span",
            {
              ...partAttrs("window-title"),
              class:
                "window-title pointer-events-none absolute left-1/2 max-w-[60%] min-w-0 -translate-x-1/2 truncate " +
                "text-(length:--font-size-row) " +
                (isFocused ? "font-bold" : "font-medium opacity-70"),
            },
            title,
          );
        }
        return m(
          "span",
          { class: "pointer-events-none absolute left-1/2 flex max-w-[60%] min-w-0 -translate-x-1/2 items-center" },
          [
            m(
              "span",
              {
                ...partAttrs("window-icon"),
                class: `flex shrink-0 items-center ${isFocused ? "" : "opacity-50"}`,
              },
              m.trust(appGlyph(app, APP_GLYPH_SIZE)),
            ),
            m(
              "span",
              {
                ...partAttrs("window-title"),
                class:
                  "window-title ml-1.5 min-w-0 truncate text-(length:--font-size-row) " +
                  (isFocused ? "font-bold" : "font-medium opacity-70"),
              },
              title,
            ),
          ],
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
          ...slotRun(chrome.leading),
          m("span", { class: "flex-1" }),
          ...slotRun(chrome.trailing),
          isTitleCentered ? centeredGroup() : null,
        ],
      );
    },
  };
}
