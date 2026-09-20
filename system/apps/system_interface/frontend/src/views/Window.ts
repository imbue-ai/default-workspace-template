/**
 * A window's chrome (desktop-interface plan sections 4.2 and 4.3): the title bar, the content
 * box the live page is laid over (``data-window-content``), the transparent shield over the
 * content of every window but the focused one (a press on it raises the window and is consumed,
 * since a click into a cross-origin page cannot reach the shell), and the eight resize edges
 * (``data-resize-edge``). The window is positioned by the pixels the store hands it; it reads no
 * metric from the DOM and attaches no gesture listener.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import type { PixelRect, ResizeEdge } from "../geometry/frames";
import { RESIZE_EDGES } from "../geometry/frames";
import type { AppRecord, WindowRecord, WindowState } from "../model/records";
import { TitleBar } from "./TitleBar";
import type { WindowControl } from "./TitleBar";

const EDGE_CLASS: Readonly<Record<ResizeEdge, string>> = {
  n: "top-0 right-2 left-2 h-1.5 cursor-ns-resize",
  s: "right-2 bottom-0 left-2 h-1.5 cursor-ns-resize",
  e: "top-2 right-0 bottom-2 w-1.5 cursor-ew-resize",
  w: "top-2 bottom-2 left-0 w-1.5 cursor-ew-resize",
  ne: "top-0 right-0 h-3 w-3 cursor-nesw-resize",
  nw: "top-0 left-0 h-3 w-3 cursor-nwse-resize",
  se: "right-0 bottom-0 h-3 w-3 cursor-nwse-resize",
  sw: "bottom-0 left-0 h-3 w-3 cursor-nesw-resize",
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
  /** Whether this client has a page for the window (a window settling on another client's open has none). */
  readonly hasPage: boolean;
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
      const { window, app, title, rect, state, stackIndex, isFocused, isCompact, isTouch, hasPage } = attrs;
      const isStopped = app !== undefined && !app.is_running;
      const isSettlingElsewhere = window.is_settling && !hasPage;
      const isResizable = !isCompact && !isTouch;
      return m(
        "div",
        {
          "data-window-id": window.id,
          "data-window-state": state,
          "data-minimized": "false",
          "data-focused": isFocused ? "true" : "false",
          class:
            "window absolute flex flex-col overflow-hidden rounded-(--desk-window-radius) border bg-surface " +
            "shadow-(--desk-window-shadow) " +
            (isFocused ? "border-default" : "border-subtle"),
          style: {
            left: `${rect.x}px`,
            top: `${rect.y}px`,
            width: `${rect.width}px`,
            height: `${rect.height}px`,
            // Interleaved with the pages: this chrome over its own page (2i+1) and every lower window.
            zIndex: String(2 * stackIndex + 2),
          },
          onpointerdown: () => {
            if (!isFocused) attrs.onRaise();
          },
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
          m("div", { "data-window-content": "", class: "window-content relative min-h-0 flex-1 bg-page" }, [
            isStopped
              ? stoppedPlaceholder(app, attrs.onStartApp)
              : isSettlingElsewhere
                ? m(
                    "div",
                    {
                      "data-settling": "",
                      class: "flex h-full w-full items-center justify-center text-(length:--font-size-row) text-faint",
                    },
                    "Starting on another screen…",
                  )
                : null,
            // The shield: the press that raises the window lands here rather than in the page.
            isFocused
              ? null
              : m("div", {
                  "data-window-shield": "",
                  class: "absolute inset-0 cursor-default",
                  onpointerdown: (event: PointerEvent) => {
                    event.preventDefault();
                    attrs.onRaise();
                  },
                }),
          ]),
          ...(isResizable
            ? RESIZE_EDGES.map((edge) =>
                m("div", {
                  "data-resize-edge": edge,
                  class: `resize-edge absolute touch-none ${EDGE_CLASS[edge]}`,
                }),
              )
            : []),
        ],
      );
    },
  };
}
