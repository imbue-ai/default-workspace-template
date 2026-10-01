/**
 * The phone's home grid (plan-phone-interface.md): every openable app as a tile on the first desktop's
 * wallpaper, in the launcher's order, read-only. A tap shows the app's window or launches it (the store decides
 * which); a long press offers the app's launch rows. An app that is stopped draws faint, as on the desktop.
 */

import m from "mithril";
import { wallpaperBackgroundImage } from "../../model/api";
import type { AppRecord, Desktop } from "../../model/records";
import { appGlyph } from "../glyphs";
import { longPressAttrs } from "./longPress";

/** The size the tile's markup is drawn at; the tile's box (a theme token) scales it to fit. */
const TILE_GLYPH_SIZE = 58;

export interface HomeGridAttrs {
  readonly apps: readonly AppRecord[];
  /** The desktop whose wallpaper the grid sits on: the first one. */
  readonly desktop: Desktop | null;
  readonly isStopped: (app: AppRecord) => boolean;
  readonly onTap: (app: AppRecord) => void;
  readonly onLongPress: (app: AppRecord, target: HTMLElement) => void;
  readonly thresholdPx: number;
}

export const HomeGrid: m.Component<HomeGridAttrs> = {
  view(vnode) {
    const { apps, desktop, isStopped, onTap, onLongPress, thresholdPx } = vnode.attrs;
    const wallpaper = desktop?.wallpaper ?? null;
    return m(
      "div",
      {
        "data-phone-home-grid": "",
        class: "phone-home absolute inset-0 bg-page bg-cover bg-center bg-(image:--desk-default-wallpaper)",
        style: wallpaper === null ? {} : { backgroundImage: wallpaperBackgroundImage(wallpaper) },
      },
      m(
        "div",
        {
          class:
            "phone-home-grid grid h-full grid-cols-[repeat(auto-fill,minmax(var(--desk-phone-cell-width),1fr))] " +
            "content-start gap-y-(--desk-phone-grid-row-gap) overflow-y-auto px-3 pt-(--desk-phone-grid-top) pb-4",
        },
        apps.map((app) =>
          m(
            "button",
            {
              key: app.name,
              type: "button",
              "data-phone-app": app.name,
              "data-stopped": isStopped(app) ? "true" : "false",
              class:
                "phone-app flex flex-col items-center gap-1.5 text-center select-none " +
                (isStopped(app) ? "opacity-45" : ""),
              ...longPressAttrs({
                onTap: () => onTap(app),
                onLongPress: (target) => onLongPress(app, target),
                thresholdPx,
              }),
            },
            [
              m(
                "span",
                {
                  class:
                    "phone-app-tile block size-(--desk-phone-tile-size) overflow-hidden " +
                    "rounded-(--desk-phone-tile-radius) shadow-(--desk-icon-shadow) transition-transform " +
                    "active:scale-95 [&>svg]:size-full",
                },
                m.trust(appGlyph(app, TILE_GLYPH_SIZE)),
              ),
              m(
                "span",
                {
                  class:
                    "phone-app-name line-clamp-2 max-w-(--desk-phone-cell-width) text-xs leading-tight " +
                    "font-semibold wrap-anywhere text-on-accent [filter:var(--desk-shortcut-label-shadow)]",
                },
                app.display_name,
              ),
            ],
          ),
        ),
      ),
    );
  },
};
