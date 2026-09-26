/**
 * The ghost of a pulled-out window (the pull-out-window spec, section 4.2): a dashed outline at the window's
 * frame, with the app's glyph, the title, and the two ways to it -- "Show", which raises or reopens the desktop
 * window the chrome shows it in, and "Bring back", which returns it to the desktop here. It never takes focus
 * and no page is drawn in it; the window's page lives in the chrome's own window.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import type { PixelRect } from "../geometry/frames";
import { windowChromeZIndex } from "../geometry/stacking";
import type { AppRecord, WindowRecord } from "../model/records";
import { appGlyph } from "./glyphs";
import { rectStyle } from "./pixelStyle";

const APP_GLYPH_SIZE = 20;

export const DETACHED_WINDOW_ATTRIBUTE = "data-detached-window";

export interface DetachedWindowGhostAttrs {
  readonly window: WindowRecord;
  readonly app: AppRecord | undefined;
  readonly title: string;
  readonly rect: PixelRect;
  readonly stackIndex: number;
  readonly onShow: () => void;
  readonly onBringBack: () => void;
}

export const DetachedWindowGhost: m.Component<DetachedWindowGhostAttrs> = {
  view(vnode) {
    const { window, app, title, rect, stackIndex, onShow, onBringBack } = vnode.attrs;
    return m(
      "div",
      {
        [DETACHED_WINDOW_ATTRIBUTE]: window.id,
        // The ghost takes the window's place in the stacking order: over the pages below it, under the
        // windows above, and it takes the press itself (the layer it sits in is inert).
        class:
          "detached-window-ghost pointer-events-auto absolute flex flex-col items-center justify-center gap-3 " +
          "rounded-(--desk-window-radius) border-2 border-dashed border-accent bg-surface/60 p-4 text-center " +
          "select-none",
        style: { ...rectStyle(rect), zIndex: windowChromeZIndex(stackIndex) },
      },
      [
        m("span", { class: "flex items-center text-secondary" }, m.trust(appGlyph(app, APP_GLYPH_SIZE))),
        m(
          "div",
          { class: "window-title max-w-full truncate text-(length:--font-size-row) font-medium text-primary" },
          title,
        ),
        m("div", { class: "text-(length:--font-size-row) text-faint" }, "Open in its own window"),
        m("div", { class: "flex flex-wrap items-center justify-center gap-2" }, [
          m(Button, { variant: "secondary", sm: true, "data-ghost-action": "show", onclick: onShow }, "Show"),
          m(Button, { sm: true, "data-ghost-action": "bring-back", onclick: onBringBack }, "Bring back"),
        ]),
      ],
    );
  },
};
