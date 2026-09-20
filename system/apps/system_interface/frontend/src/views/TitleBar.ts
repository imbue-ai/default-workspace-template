/**
 * A window's title bar (concepts.md section 2.7), left to right: the app icon, the title, the
 * window menu (three dots, right after the title), then at the right edge minimize,
 * maximize (restore when maximized), and close. It is the drag handle (``data-drag-handle``);
 * a double click toggles maximize. The maximize and restore controls are hidden in compact mode,
 * where every window renders maximized, and the close control is absent from a pinned window,
 * which is never closed.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import type { AppRecord, WindowState } from "../model/records";
import { appGlyph, glyph } from "./glyphs";

const CONTROL_GLYPH_SIZE = 14;
const APP_GLYPH_SIZE = 16;

export type WindowControl = "minimize" | "maximize" | "restore" | "close" | "menu";

export interface TitleBarAttrs {
  readonly title: string;
  readonly app: AppRecord | undefined;
  readonly state: WindowState;
  readonly isFocused: boolean;
  readonly isCompact: boolean;
  readonly isMenuOpen: boolean;
  readonly isPinned: boolean;
  readonly onControl: (control: WindowControl, event: MouseEvent) => void;
  readonly onDoubleClick: () => void;
}

function control(
  name: WindowControl,
  label: string,
  markup: string,
  isOpen: boolean,
  onControl: TitleBarAttrs["onControl"],
): m.Vnode {
  return m(
    Button,
    {
      variant: "ghost",
      icon: true,
      sm: true,
      extra: "window-control shrink-0 min-h-(--desk-touch-target) min-w-(--desk-touch-target)",
      "data-window-control": name,
      "data-no-drag": "",
      "aria-label": label,
      "aria-expanded": name === "menu" ? (isOpen ? "true" : "false") : undefined,
      ...hoverTooltipAttrs(label),
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
      const { title, app, state, isFocused, isCompact, isMenuOpen, isPinned, onControl, onDoubleClick } = vnode.attrs;
      const isMaximized = state === "MAXIMIZED";
      return m(
        "div",
        {
          "data-drag-handle": "",
          class:
            "title-bar pointer-events-auto flex h-(--desk-title-bar-height) shrink-0 items-center gap-1 border-b " +
            "border-default pr-1 pl-2 touch-none select-none " +
            (isFocused ? "bg-surface text-primary" : "bg-surface-secondary text-secondary"),
          ondblclick: (event: MouseEvent) => {
            if ((event.target as Element).closest("[data-window-control]") !== null) return;
            onDoubleClick();
          },
        },
        [
          m("span", { class: "flex shrink-0 items-center text-secondary" }, m.trust(appGlyph(app, APP_GLYPH_SIZE))),
          m("span", { class: "window-title min-w-0 truncate pl-1 text-(length:--font-size-row) font-medium" }, title),
          control("menu", "Window menu", glyph("kebab", CONTROL_GLYPH_SIZE), isMenuOpen, onControl),
          m("span", { class: "flex-1" }),
          control("minimize", "Minimize", glyph("minimize", CONTROL_GLYPH_SIZE), false, onControl),
          isCompact
            ? null
            : isMaximized
              ? control("restore", "Restore", glyph("restore", CONTROL_GLYPH_SIZE), false, onControl)
              : control("maximize", "Maximize", glyph("maximize", CONTROL_GLYPH_SIZE), false, onControl),
          isPinned ? null : control("close", "Close", icon("close", { size: CONTROL_GLYPH_SIZE }), false, onControl),
        ],
      );
    },
  };
}
