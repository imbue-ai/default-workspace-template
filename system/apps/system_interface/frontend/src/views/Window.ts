/**
 * A window's chrome (desktop-interface plan sections 4.2 and 4.3): the title bar, the content
 * box the live page is laid over (``data-window-content``), the transparent shield over the
 * content of every window but the focused one (a press on it raises the window and is consumed,
 * since a click into a cross-origin page cannot reach the shell) and over every window while a
 * menu or the launcher is open (the press that closes them has to reach the shell), and the eight
 * resize handles (``data-resize-edge``). The root (``data-window-id``) is positioned by the
 * pixels the store hands it and clips nothing: it carries the handles, which overhang the
 * window's border so a press just outside the frame still grabs an edge, around an inner frame
 * that clips its rounded corners and holds the title bar and the content. The chrome is stacked
 * over its own live page, so the root, the frame, and the content box are inert
 * (``pointer-events: none``, inherited from the windows layer) and only the title bar, the
 * handles, the shield, and the placeholders take a press. It reads no metric from the DOM and
 * attaches no gesture listener.
 */

import m from "mithril";
import { partAttrs } from "@imbue/workspace-ui/src/themes/parts";
import type { PixelRect, ResizeEdge } from "../geometry/frames";
import { RESIZE_EDGES } from "../geometry/frames";
import { windowChromeZIndex } from "../geometry/stacking";
import type { FailureNotice } from "../model/failures";
import type { AppRecord, WindowRecord, WindowState } from "../model/records";
import { FailureLine } from "./FailureLine";
import { rectStyle } from "./pixelStyle";
import { TitleBar } from "./TitleBar";
import type { WindowControl } from "./TitleBar";
import type { ThemeChrome } from "../model/themes";

// The handles' sizes are theme tokens (contracts.md section 11): an edge is a strip of
// --desk-resize-edge overhanging the border by --desk-resize-overhang, inset from the corners,
// which are --desk-resize-corner squares over the same overhang. Spelled out in full: Tailwind
// generates only the utilities it finds as literal class names.
const EDGE_CLASS: Readonly<Record<ResizeEdge, string>> = {
  n: "-top-(--desk-resize-overhang) inset-x-(--desk-resize-edge-inset) h-(--desk-resize-edge) cursor-ns-resize",
  s: "-bottom-(--desk-resize-overhang) inset-x-(--desk-resize-edge-inset) h-(--desk-resize-edge) cursor-ns-resize",
  e: "-right-(--desk-resize-overhang) inset-y-(--desk-resize-edge-inset) w-(--desk-resize-edge) cursor-ew-resize",
  w: "-left-(--desk-resize-overhang) inset-y-(--desk-resize-edge-inset) w-(--desk-resize-edge) cursor-ew-resize",
  ne: "-top-(--desk-resize-overhang) -right-(--desk-resize-overhang) size-(--desk-resize-corner) cursor-nesw-resize",
  nw: "-top-(--desk-resize-overhang) -left-(--desk-resize-overhang) size-(--desk-resize-corner) cursor-nwse-resize",
  se: "-right-(--desk-resize-overhang) -bottom-(--desk-resize-overhang) size-(--desk-resize-corner) cursor-nwse-resize",
  sw: "-bottom-(--desk-resize-overhang) -left-(--desk-resize-overhang) size-(--desk-resize-corner) cursor-nesw-resize",
};

export interface WindowAttrs {
  readonly window: WindowRecord;
  readonly app: AppRecord | undefined;
  readonly title: string;
  readonly rect: PixelRect;
  readonly state: WindowState;
  readonly stackIndex: number;
  readonly isFocused: boolean;
  readonly isTouch: boolean;
  readonly isMenuOpen: boolean;
  /** Spread onto the maximize control: resting on it opens the window's size menu. */
  readonly sizeMenuTrigger: m.Attributes;
  /** Whether the shield covers the content: every unfocused window, and every window while a menu or the
   *  launcher is open. */
  readonly isShielded: boolean;
  /** What the window says in place of its page while the app behind it is stopped and nothing brings it back on a
   *  request (a row the workspace cannot start: no supervised program, critical, or inside a critical app's
   *  program), with the next step the workspace has; null otherwise. A stoppable app's window keeps its page, which
   *  the shell's parker answers with a loading page until the app is up. */
  readonly stopped: FailureNotice | null;
  /** Whether the window's app is no longer part of the workspace (the app list has landed without it): the window
   *  says so and offers to close, in place of a page it can never show. */
  readonly isAppGone: boolean;
  readonly onRaise: () => void;
  readonly onControl: (control: WindowControl, event: MouseEvent) => void;
  readonly onToggleMaximize: () => void;
  readonly onClose: () => void;
  /** The theme's arrangement of the title bar. */
  readonly chrome: ThemeChrome;
}

/** What a window shows in place of its page: why it has none (its app is stopped, or no longer part of the
 *  workspace), with the buttons for the next step. */
export function windowPlaceholder(marker: string, notice: FailureNotice): m.Vnode {
  return m(
    "div",
    {
      [`data-${marker}`]: "",
      class: "flex h-full w-full flex-col items-center justify-center bg-surface px-6 text-center",
    },
    m(FailureLine, { notice, marker: `${marker}-notice`, messageClass: "type-label text-primary" }),
  );
}

/** What a window of an app that is no longer part of the workspace shows. */
export function appGoneNotice(onClose: () => void): FailureNotice {
  return {
    message: "This app is no longer part of this workspace.",
    details: null,
    actions: [{ label: "Close window", run: onClose }],
    isReportable: false,
  };
}

export function Window(): m.Component<WindowAttrs> {
  return {
    view(vnode) {
      const attrs = vnode.attrs;
      const { window, app, title, rect, state, stackIndex, isFocused, isTouch, stopped, isAppGone } = attrs;
      const isResizable = !isTouch;
      return m(
        "div",
        {
          "data-window-id": window.id,
          "data-window-state": state,
          "data-minimized": "false",
          "data-focused": isFocused ? "true" : "false",
          "data-pinned": window.is_pinned ? "true" : "false",
          ...partAttrs("window"),
          class: "window absolute",
          style: {
            ...rectStyle(rect),
            zIndex: windowChromeZIndex(stackIndex),
          },
          onpointerdown: () => {
            if (!isFocused) attrs.onRaise();
          },
        },
        [
          m(
            "div",
            {
              "data-window-frame": "",
              ...partAttrs("window-frame"),
              // No border: the shadow is what separates a window from the backdrop, and a line
              // around it only competes. Focus is the title bar's, which changes colour with it.
              class:
                "window-frame flex h-full w-full flex-col overflow-hidden rounded-(--desk-window-radius) " +
                "shadow-(--desk-window-shadow)",
            },
            [
              m(TitleBar, {
                title,
                app,
                state,
                isFocused,
                // A window with no page has nothing to refresh.
                canRefresh: !isAppGone,
                isMenuOpen: attrs.isMenuOpen,
                sizeMenuTrigger: attrs.sizeMenuTrigger,
                onControl: attrs.onControl,
                onDoubleClick: attrs.onToggleMaximize,
                chrome: attrs.chrome,
              }),
              // Transparent and inert like the root: the live page sits under this chrome in the stacking
              // order, shows through here, and takes the pointer; only the shield and the placeholders catch
              // a press.
              m(
                "div",
                {
                  "data-window-content": "",
                  ...partAttrs("window-content"),
                  class: "window-content relative min-h-0 flex-1 [&>*]:pointer-events-auto",
                },
                [
                  stopped === null ? null : windowPlaceholder("stopped-app", stopped),
                  isAppGone ? windowPlaceholder("gone-app", appGoneNotice(attrs.onClose)) : null,
                  // The shield: the press that raises the window (or closes an open menu or the launcher)
                  // lands here rather than in the page, and bubbles to the window's own handler and on to
                  // the document. A right-click here is that press and nothing more: handled, so the
                  // desktop's element menu yields to it (element-reference-menu plan section 12).
                  attrs.isShielded
                    ? m("div", {
                        "data-window-shield": "",
                        class: "absolute inset-0 cursor-default",
                        onpointerdown: (event: PointerEvent) => event.preventDefault(),
                        oncontextmenu: (event: MouseEvent) => event.preventDefault(),
                      })
                    : null,
                ],
              ),
            ],
          ),
          ...(isResizable
            ? RESIZE_EDGES.map((edge) =>
                m("div", {
                  "data-resize-edge": edge,
                  class: `resize-edge pointer-events-auto absolute touch-none ${EDGE_CLASS[edge]}`,
                }),
              )
            : []),
        ],
      );
    },
  };
}
