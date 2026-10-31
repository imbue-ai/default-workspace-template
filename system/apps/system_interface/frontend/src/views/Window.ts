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
 *
 * A fullscreen window (the Screen spec, section 12) draws no title bar, no handles, and no corner
 * or shadow: it fills the viewport, and a strip at its top edge (``data-fullscreen-strip``) shows on
 * hover with Exit fullscreen and, where the chrome can do it, Immersive. The strip's hot zone is the
 * one part of it that takes the pointer at rest, as thin as a resize edge; the controls hang below
 * it while it or they are hovered.
 */

import m from "mithril";
import { partAttrs } from "@imbue/workspace-ui/src/themes/parts";
import type { PixelRect, ResizeEdge } from "../geometry/frames";
import { RESIZE_EDGES } from "../geometry/frames";
import { windowChromeZIndex } from "../geometry/stacking";
import type { AppRecord, WindowRecord, WindowState } from "../model/records";
import { glyph } from "./glyphs";
import { rectStyle } from "./pixelStyle";
import { TitleBar } from "./TitleBar";
import type { WindowControl } from "./TitleBar";
import { Button } from "@imbue/workspace-ui/src/components/Button";
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

/** What the fullscreen strip says and offers of immersive mode (the Screen integration spec, section 5); null
 *  when the chrome cannot do it, which hides the control. */
export interface ImmersiveControls {
  readonly isOn: boolean;
  readonly isKeyboardLocked: boolean;
  /** Asked straight from the click: the chrome needs the gesture's activation for its fullscreen request. */
  readonly onEnter: () => void;
  readonly onLeave: () => void;
}

/** What the strip says of the keys while the window is fullscreen (the Screen spec, section 12). */
export function fullscreenStripHint(immersive: ImmersiveControls | null): string {
  if (immersive === null) return "";
  if (!immersive.isOn) return "Press Immersive to send every key";
  return immersive.isKeyboardLocked ? "Every key reaches this window" : "Immersive, without the keyboard lock";
}

const STRIP_GLYPH_SIZE = 14;

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
  /** Whether the app behind the window is stopped and nothing brings it back on a request (a row the workspace
   *  cannot start: no supervised program, critical, or inside a critical app's program): its page is replaced by
   *  the stopped placeholder. A stoppable app's window keeps its page, which the shell's parker answers with a
   *  loading page until the app is up. */
  readonly isStopped: boolean;
  /** Immersive mode's state and verbs for the fullscreen strip; null hides its control. */
  readonly immersive: ImmersiveControls | null;
  readonly onRaise: () => void;
  readonly onControl: (control: WindowControl, event: MouseEvent) => void;
  readonly onToggleMaximize: () => void;
  /** The theme's arrangement of the title bar. */
  readonly chrome: ThemeChrome;
}

/** What a window shows in place of its page while the app behind it is stopped and the workspace cannot bring
 *  it back on a request. */
export function stoppedPlaceholder(app: AppRecord | undefined): m.Vnode {
  const label = app?.display_name ?? "This app";
  const detail = app !== undefined && app.program !== "" ? "stopped" : "not running (managed outside the workspace)";
  return m(
    "div",
    { "data-stopped-app": "", class: "flex h-full w-full flex-col items-center justify-center gap-3 bg-surface" },
    [
      m("div", { class: "type-label text-primary" }, label),
      m("div", { class: "text-(length:--font-size-row) text-faint" }, detail),
    ],
  );
}

function stripControl(name: string, label: string, markup: string, onClick: () => void): m.Vnode {
  return m(
    Button,
    {
      variant: "ghost",
      sm: true,
      extra: "fullscreen-control gap-1",
      "data-fullscreen-control": name,
      "aria-label": label,
      onclick: (event: MouseEvent) => {
        event.stopPropagation();
        onClick();
      },
    },
    [m.trust(markup), label],
  );
}

/** The strip a fullscreen window shows on hover at its top edge. */
function fullscreenStrip(attrs: WindowAttrs): m.Vnode {
  const immersive = attrs.immersive;
  return m(
    "div",
    {
      "data-fullscreen-strip": "",
      // The hot zone: as thin as a resize edge, and the one part of the chrome over the page that takes the
      // pointer at rest. The controls below it belong to the same group, so they stay while hovered.
      class: "fullscreen-strip group pointer-events-auto absolute inset-x-0 top-0 h-(--desk-resize-edge)",
    },
    m(
      "div",
      {
        "data-fullscreen-strip-controls": "",
        class:
          "hidden h-(--desk-title-bar-height) items-center gap-2 border-b border-default bg-surface px-2 " +
          "text-primary group-focus-within:flex group-hover:flex",
      },
      [
        m(
          "span",
          { "data-fullscreen-hint": "", class: "min-w-0 truncate text-(length:--font-size-row) text-secondary" },
          fullscreenStripHint(immersive),
        ),
        m("span", { class: "flex-1" }),
        immersive === null
          ? null
          : immersive.isOn
            ? stripControl("leave-immersive", "Leave immersive", glyph("exit-fullscreen", STRIP_GLYPH_SIZE), () =>
                immersive.onLeave(),
              )
            : stripControl("immersive", "Immersive", glyph("fullscreen", STRIP_GLYPH_SIZE), () => immersive.onEnter()),
        stripControl("exit-fullscreen", "Exit fullscreen", glyph("exit-fullscreen", STRIP_GLYPH_SIZE), () =>
          attrs.onControl("fullscreen", new MouseEvent("click")),
        ),
      ],
    ),
  );
}

export function Window(): m.Component<WindowAttrs> {
  return {
    view(vnode) {
      const attrs = vnode.attrs;
      const { window, app, title, rect, state, stackIndex, isFocused, isTouch, isStopped } = attrs;
      const isFullscreen = state === "FULLSCREEN";
      const isResizable = !isTouch && !isFullscreen;
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
              // A fullscreen window has no backdrop around it to be separated from.
              class:
                "window-frame flex h-full w-full flex-col overflow-hidden " +
                (isFullscreen ? "" : "rounded-(--desk-window-radius) shadow-(--desk-window-shadow)"),
            },
            [
              isFullscreen
                ? fullscreenStrip(attrs)
                : m(TitleBar, {
                    title,
                    app,
                    state,
                    isFocused,
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
                  isStopped ? stoppedPlaceholder(app) : null,
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
