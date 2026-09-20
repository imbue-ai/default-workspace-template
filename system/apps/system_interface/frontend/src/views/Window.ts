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
import { Button } from "@imbue/workspace-ui/src/components/Button";
import type { PixelRect, ResizeEdge } from "../geometry/frames";
import { RESIZE_EDGES } from "../geometry/frames";
import type { AppRecord, WindowRecord, WindowState } from "../model/records";
import { rectStyle } from "./pixelStyle";
import { TitleBar } from "./TitleBar";
import type { WindowControl } from "./TitleBar";

// The handles' sizes are theme tokens (contracts.md section 11): an edge is a strip of
// --desk-resize-edge overhanging the border by --desk-resize-overhang, inset from the corners,
// which are --desk-resize-corner squares over the same overhang. The corners come last in
// RESIZE_EDGES, so they win where an edge would reach them. Spelled out in full: Tailwind
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
  readonly isCompact: boolean;
  readonly isTouch: boolean;
  readonly isMenuOpen: boolean;
  /** Whether the shield covers the content: every unfocused window, and every window while a menu or the
   *  launcher is open. */
  readonly isShielded: boolean;
  /** Whether this client's layout places the window; a window settling on another client's open is not
   *  placed here and shows a placeholder instead of a page. */
  readonly isPlacedHere: boolean;
  /** Offered when the app is stopped and the workspace can start it; null otherwise. */
  readonly onStartApp: (() => void) | null;
  readonly onRaise: () => void;
  readonly onControl: (control: WindowControl, event: MouseEvent) => void;
  readonly onToggleMaximize: () => void;
}

/** What a window shows in place of its page while the app behind it is stopped: that it is
 *  stopped, and a Start where the workspace can start it. */
function stoppedPlaceholder(app: AppRecord | undefined, onStartApp: (() => void) | null): m.Vnode {
  const label = app?.display_name ?? "This app";
  const detail = app !== undefined && app.program !== "" ? "stopped" : "not running (managed outside the workspace)";
  return m(
    "div",
    { "data-stopped-app": "", class: "flex h-full w-full flex-col items-center justify-center gap-3 bg-surface" },
    [
      m("div", { class: "type-label text-primary" }, label),
      m("div", { class: "text-(length:--font-size-row) text-faint" }, detail),
      onStartApp === null ? null : m(Button, { extra: "mt-1", onclick: onStartApp }, `Start ${label}`),
    ],
  );
}

export function Window(): m.Component<WindowAttrs> {
  return {
    view(vnode) {
      const attrs = vnode.attrs;
      const { window, app, title, rect, state, stackIndex, isFocused, isCompact, isTouch, isPlacedHere } = attrs;
      const isStopped = app !== undefined && !app.is_running;
      const isSettlingElsewhere = window.is_settling && !isPlacedHere;
      const isResizable = !isCompact && !isTouch;
      return m(
        "div",
        {
          "data-window-id": window.id,
          "data-window-state": state,
          "data-minimized": "false",
          "data-focused": isFocused ? "true" : "false",
          class: "window absolute",
          style: {
            ...rectStyle(rect),
            // Interleaved with the pages: this chrome over its own page (2i+1) and every lower window.
            zIndex: String(2 * stackIndex + 2),
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
              class:
                "window-frame flex h-full w-full flex-col overflow-hidden rounded-(--desk-window-radius) border " +
                "shadow-(--desk-window-shadow) " +
                (isFocused ? "border-default" : "border-subtle"),
            },
            [
              m(TitleBar, {
                title,
                app,
                state,
                isFocused,
                isCompact,
                isMenuOpen: attrs.isMenuOpen,
                onControl: attrs.onControl,
                onDoubleClick: attrs.onToggleMaximize,
              }),
              // Transparent and inert like the root: the live page sits under this chrome in the stacking
              // order, shows through here, and takes the pointer; only the shield and the placeholders catch
              // a press.
              m(
                "div",
                {
                  "data-window-content": "",
                  class: "window-content relative min-h-0 flex-1 [&>*]:pointer-events-auto",
                },
                [
                  isStopped
                    ? stoppedPlaceholder(app, attrs.onStartApp)
                    : isSettlingElsewhere
                      ? m(
                          "div",
                          {
                            "data-settling": "",
                            class:
                              "flex h-full w-full items-center justify-center bg-page text-(length:--font-size-row) text-faint",
                          },
                          "Starting on another screen…",
                        )
                      : null,
                  // The shield: the press that raises the window (or closes an open menu or the launcher)
                  // lands here rather than in the page, and bubbles to the window's own handler and on to
                  // the document.
                  attrs.isShielded
                    ? m("div", {
                        "data-window-shield": "",
                        class: "absolute inset-0 cursor-default",
                        onpointerdown: (event: PointerEvent) => event.preventDefault(),
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
